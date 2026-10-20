"""The `${project}-slack` table (integrations/slack/repository.py), on the wire.

A real boto3 resource, with every request caught before it leaves: what is
checked is the JSON DynamoDB would actually receive. That is the level the
first deployed save failed at — values serialised twice, so the key went out
as `{"M": {"S": {"S": …}}}` — which a stub of the Python call cannot see.
"""
import json
import os
import sys

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.slack.repository import (  # noqa: E402
    IntegrationExists,
    SlackRepository,
)


class _Raw:
    def __init__(self, body: bytes):
        self._body = body

    def stream(self, *args, **kwargs):
        yield self._body


class Wire:
    """Captures requests; answers each with the next queued (status, body)."""

    def __init__(self):
        self.requests = []
        self.replies = []

    def reply(self, status=200, body=None, error=None):
        if error is not None:
            body = {"__type": f"com.amazonaws.dynamodb.v20120810#{error[0]}", "message": "x", **error[1]}
        self.replies.append((status, body or {}))

    def __call__(self, request, **kwargs):
        target = request.headers.get("X-Amz-Target", b"")
        if isinstance(target, bytes):
            target = target.decode()
        self.requests.append((target.split(".")[-1], json.loads(request.body)))
        status, body = self.replies.pop(0) if self.replies else (200, {})
        return AWSResponse(request.url, status, {"x-amzn-requestid": "r"}, _Raw(json.dumps(body).encode()))


@pytest.fixture
def repo():
    session = boto3.session.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="ap-northeast-2"
    )
    resource = session.resource("dynamodb")
    wire = Wire()
    resource.meta.client.meta.events.register("before-send.dynamodb", wire)
    r = SlackRepository.__new__(SlackRepository)
    r.table_name = "bap-slack"
    r.dynamodb = resource
    r.table = resource.Table("bap-slack")
    r.wire = wire
    return r


INTEGRATION = {
    "integration_id": "i1",
    "harness_id": "h1",
    "app_id": "A1",
    "team_id": "T1",
    "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:1:secret:bap/slack/h1/i1",
    "status": "active",
}


def test_create_sends_plain_typed_values_not_double_serialised(repo):
    repo.create_integration(dict(INTEGRATION))
    op, body = repo.wire.requests[0]
    assert op == "TransactWriteItems"
    put_integration = body["TransactItems"][0]["Put"]
    put_app = body["TransactItems"][1]["Put"]
    assert put_integration["Item"]["pk"] == {"S": "INTEGRATION"}
    assert put_integration["Item"]["sk"] == {"S": "h1"}
    assert put_integration["Item"]["secret_arn"] == {"S": INTEGRATION["secret_arn"]}
    assert put_app["Item"]["pk"] == {"S": "APP#A1"}
    assert put_app["Item"]["harness_id"] == {"S": "h1"}
    for put in (put_integration, put_app):
        assert put["ConditionExpression"] == "attribute_not_exists(pk)"
        assert put["TableName"] == "bap-slack"


def test_no_token_is_written(repo):
    repo.create_integration(dict(INTEGRATION))
    sent = json.dumps(repo.wire.requests)
    assert "xoxb-" not in sent and "xapp-" not in sent


@pytest.mark.parametrize(
    "reasons,which",
    [
        ([{"Code": "ConditionalCheckFailed"}, {"Code": "None"}], "harness"),
        ([{"Code": "None"}, {"Code": "ConditionalCheckFailed"}], "app"),
    ],
)
def test_a_duplicate_harness_or_app_is_reported_as_such(repo, reasons, which):
    repo.wire.reply(400, error=("TransactionCanceledException", {"CancellationReasons": reasons}))
    with pytest.raises(IntegrationExists) as caught:
        repo.create_integration(dict(INTEGRATION))
    assert caught.value.reason == which


def test_a_cancellation_for_another_reason_is_raised_and_logged(repo, caplog):
    repo.wire.reply(
        400,
        error=("TransactionCanceledException", {"CancellationReasons": [{"Code": "ValidationError"}, {"Code": "None"}]}),
    )
    with pytest.raises(ClientError):
        repo.create_integration(dict(INTEGRATION))
    assert "ValidationError" in caplog.text


def test_event_marker_is_conditional_and_a_redelivery_is_false(repo):
    assert repo.mark_event("i1", "D1", "1.5", now=1000) is True
    op, body = repo.wire.requests[0]
    assert op == "PutItem"
    assert body["Item"]["pk"] == {"S": "EVENT#i1#D1#1.5"}
    assert body["Item"]["ttl"] == {"N": str(1000 + 2 * 24 * 3600)}
    assert body["ConditionExpression"] == "attribute_not_exists(pk)"
    repo.wire.reply(400, error=("ConditionalCheckFailedException", {}))
    assert repo.mark_event("i1", "D1", "1.5", now=1000) is False


def test_event_marker_storage_errors_propagate(repo):
    repo.wire.reply(400, error=("AccessDeniedException", {}))
    with pytest.raises(ClientError):
        repo.mark_event("i1", "D1", "1.5")


def test_lock_is_taken_when_absent_expired_or_already_ours(repo):
    assert repo.acquire_lock("t1", "me", 90, now=100.0) is True
    _, body = repo.wire.requests[0]
    assert body["Item"]["holder"] == {"S": "me"}
    assert body["Item"]["expires_at"] == {"N": str(190_000)}
    assert body["ConditionExpression"] == "attribute_not_exists(pk) OR expires_at < :now OR holder = :holder"
    assert body["ExpressionAttributeValues"] == {":now": {"N": "100000"}, ":holder": {"S": "me"}}
    repo.wire.reply(400, error=("ConditionalCheckFailedException", {}))
    assert repo.acquire_lock("t1", "other", 90, now=101.0) is False


def test_lock_renew_and_release_are_holder_conditional(repo):
    repo.wire.reply(400, error=("ConditionalCheckFailedException", {}))
    assert repo.renew_lock("t1", "me", 90, now=0) is False
    _, body = repo.wire.requests[0]
    assert body["ConditionExpression"] == "holder = :holder"
    # Someone else's lock: release leaves it and does not raise.
    repo.wire.reply(400, error=("ConditionalCheckFailedException", {}))
    repo.release_lock("t1", "me")
    op, body = repo.wire.requests[1]
    assert op == "DeleteItem" and body["ConditionExpression"] == "holder = :holder"


def test_update_integration_requires_the_row(repo):
    repo.update_integration("h1", status="deleting")
    _, body = repo.wire.requests[0]
    assert body["ConditionExpression"] == "attribute_exists(pk)"
    assert body["ExpressionAttributeValues"] == {":v0": {"S": "deleting"}}
