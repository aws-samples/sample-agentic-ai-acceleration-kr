"""The `${project}-slack` table: integrations, app uniqueness, event markers, locks.

    pk                                              sk
    INTEGRATION                                     <harness_id>   the integration
    APP#<api_app_id>                                APP            one app, one Harness
    EVENT#<integration_id>#<dm>#<ts>                E              event seen (TTL)
    LOCK#<thread_id>                                L              turn in flight

No token is ever written here: the integration names its secret by ARN.

Transactions go through the resource's own client, which serialises plain
Python values itself. Handing it pre-serialised `{"S": …}` values serialises
them a second time — the key went out as `{"M": {"S": {"S": …}}}` and DynamoDB
cancelled the transaction (seen on the first deployed save).
"""
import logging
import time
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from repositories.base import DynamoDBRepository

logger = logging.getLogger(__name__)

INTEGRATION_PK = "INTEGRATION"
STATUS_ACTIVE = "active"
STATUS_DELETING = "deleting"

# How long an event marker is kept. Slack retries within minutes; two days
# also covers a redeploy that replays a backlog.
EVENT_MARKER_TTL_SECONDS = 2 * 24 * 3600


class IntegrationExists(Exception):
    """The Harness already has an integration, or the app is connected elsewhere."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "harness" | "app"


def _app_key(app_id: str) -> Dict[str, str]:
    return {"pk": f"APP#{app_id}", "sk": "APP"}


def _lock_key(thread_id: str) -> Dict[str, str]:
    return {"pk": f"LOCK#{thread_id}", "sk": "L"}


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _plain(item: Dict[str, Any]) -> Dict[str, Any]:
    """DynamoDB numbers come back as Decimal; integration rows hold none that
    matter, but keep the dict JSON-friendly for the routes."""
    out = {}
    for key, value in item.items():
        if key in ("pk", "sk"):
            continue
        if hasattr(value, "is_integer"):
            value = int(value) if value == int(value) else float(value)
        out[key] = value
    return out


class SlackRepository(DynamoDBRepository):
    # ── integrations ─────────────────────────────────────────────────────────

    def get_integration(self, harness_id: str) -> Optional[Dict[str, Any]]:
        item = self.table.get_item(
            Key={"pk": INTEGRATION_PK, "sk": harness_id}, ConsistentRead=True
        ).get("Item")
        return _plain(item) if item else None

    def list_integrations(self) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        out: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(INTEGRATION_PK),
            "ConsistentRead": True,
        }
        while True:
            resp = self.table.query(**kwargs)
            out.extend(_plain(i) for i in resp.get("Items") or [])
            key = resp.get("LastEvaluatedKey")
            if not key:
                return out
            kwargs["ExclusiveStartKey"] = key

    def get_app(self, app_id: str) -> Optional[Dict[str, Any]]:
        item = self.table.get_item(Key=_app_key(app_id), ConsistentRead=True).get("Item")
        return _plain(item) if item else None

    def create_integration(self, integration: Dict[str, Any]) -> None:
        """The integration and its app row, both or neither.

        Raises IntegrationExists when either already exists.
        """
        harness_id = integration["harness_id"]
        app_id = integration["app_id"]
        client = self.dynamodb.meta.client
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": {"pk": INTEGRATION_PK, "sk": harness_id, **integration},
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": {
                                **_app_key(app_id),
                                "harness_id": harness_id,
                                "integration_id": integration["integration_id"],
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = [r.get("Code") for r in exc.response.get("CancellationReasons") or []]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                raise IntegrationExists("harness") from None
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                raise IntegrationExists("app") from None
            # Not a duplicate: say why, or the next one is another blind retry.
            logger.error("Slack integration transaction cancelled: %s", reasons)
            raise

    def update_integration(self, harness_id: str, **fields: Any) -> None:
        if not fields:
            return
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":v{i}": value for i, value in enumerate(fields.values())}
        self.table.update_item(
            Key={"pk": INTEGRATION_PK, "sk": harness_id},
            UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def delete_integration(self, harness_id: str, app_id: str) -> None:
        """Both rows. A row already gone counts as deleted."""
        if app_id:
            self.table.delete_item(Key=_app_key(app_id))
        self.table.delete_item(Key={"pk": INTEGRATION_PK, "sk": harness_id})

    # ── event de-duplication ─────────────────────────────────────────────────

    def mark_event(self, integration_id: str, channel: str, ts: str, now: Optional[float] = None) -> bool:
        """True the first time this message is seen, False on a redelivery.

        Raises on a storage error: the caller must not run a turn it could not
        record, or a retry would run it twice.
        """
        now = time.time() if now is None else now
        try:
            self.table.put_item(
                Item={
                    "pk": f"EVENT#{integration_id}#{channel}#{ts}",
                    "sk": "E",
                    "ttl": int(now + EVENT_MARKER_TTL_SECONDS),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    # ── shared per-thread lock ───────────────────────────────────────────────
    # Expiry is judged from `expires_at`, not from the item's presence: TTL
    # deletion lags by up to days, so a dead holder's row can sit there long
    # after its lease ended.

    def acquire_lock(self, thread_id: str, holder: str, ttl_seconds: float, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        try:
            self.table.put_item(
                Item={
                    **_lock_key(thread_id),
                    "holder": holder,
                    "expires_at": int((now + ttl_seconds) * 1000),
                    "ttl": int(now + ttl_seconds + 3600),
                },
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now OR holder = :holder",
                ExpressionAttributeValues={":now": int(now * 1000), ":holder": holder},
            )
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def renew_lock(self, thread_id: str, holder: str, ttl_seconds: float, now: Optional[float] = None) -> bool:
        """Extend the lease. False when it is no longer ours."""
        now = time.time() if now is None else now
        try:
            self.table.update_item(
                Key=_lock_key(thread_id),
                UpdateExpression="SET expires_at = :exp, #ttl = :ttl",
                ConditionExpression="holder = :holder",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":exp": int((now + ttl_seconds) * 1000),
                    ":ttl": int(now + ttl_seconds + 3600),
                    ":holder": holder,
                },
            )
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def release_lock(self, thread_id: str, holder: str) -> None:
        """Delete the lock if it is still ours; someone else's is left alone."""
        try:
            self.table.delete_item(
                Key=_lock_key(thread_id),
                ConditionExpression="holder = :holder",
                ExpressionAttributeValues={":holder": holder},
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
