"""
Thread repository for data access
"""
import json
from typing import Dict, List, Optional, Any
from datetime import datetime
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key

from repositories.base import DynamoDBRepository
from models.thread import Thread
from models.common import ThreadStatus


# The list indexes (infra/modules/dynamodb). Each is <attr> + updated_at.
OWNER_INDEX = "owner_sub-updated_at-index"
AGENT_INDEX = "agent_record_id-updated_at-index"
SCOPE_INDEX = "scope-updated_at-index"
# Every thread carries the same `scope`, so SCOPE_INDEX is "all threads, newest
# first" — the admin's listing. Thread writes are a few per second at most, far
# under one index partition's write ceiling.
SCOPE_ALL = "all"
# What a list row shows of the transcript: the first question (the title) and
# the first answer (the preview line). The web clips them to 50 and 100 chars.
SUMMARY_CHARS = 300


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def summary_of(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What a list row needs of a transcript: the first human and first ai
    message (text only, clipped) and the turn count (ai messages).

    Stored on the item and projected into the list indexes, so a sidebar page is
    built from the index alone instead of every thread's whole transcript. It is
    also the `values` a listed thread carries — `messages` plus `turns`."""
    messages = [m for m in (values or {}).get("messages") or [] if isinstance(m, dict)]
    out: List[Dict[str, Any]] = []
    for kind in ("human", "ai"):
        first = next((m for m in messages if m.get("type") == kind), None)
        if first is not None:
            out.append({"type": kind, "content": _text_of(first.get("content"))[:SUMMARY_CHARS]})
    return {"messages": out, "turns": sum(1 for m in messages if m.get("type") == "ai")}


# DynamoDB refuses an item over 400 KB. A thread that reached it made every
# later turn fail at its first save with a bare 500 — the conversation could
# never be continued (bst load test, 2026-10-18). The stored transcript is what
# the UI shows; the agent's context comes from AgentCore Memory. So a transcript
# near the cap is compacted on save, leaving room for the other attributes.
VALUES_MAX_BYTES = 380_000
# Messages at the end kept whole however big; older tool results are clipped first.
RECENT_MESSAGES_KEPT = 10
TOOL_RESULT_CLIP = 2_000


def fit_values(values: Optional[Dict[str, Any]], limit: int = VALUES_MAX_BYTES) -> str:
    """The transcript as stored: unchanged when it fits, else compacted.

    First the `result` of tool calls older than the last RECENT_MESSAGES_KEPT
    messages is clipped (those dominate a long tool-heavy thread); then, if it
    still does not fit, the oldest messages are dropped a question-and-answer at
    a time and counted in `trimmed_messages`, so the UI can say so.
    """
    text = json.dumps(values) if values else "{}"
    if len(text.encode("utf-8")) <= limit:
        return text
    out = dict(values or {})
    messages = [dict(m) if isinstance(m, dict) else m for m in out.get("messages") or []]
    for message in messages[:-RECENT_MESSAGES_KEPT]:
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not calls:
            continue
        clipped = []
        for call in calls:
            result = call.get("result") if isinstance(call, dict) else None
            if isinstance(result, str) and len(result) > TOOL_RESULT_CLIP:
                call = {**call, "result": result[:TOOL_RESULT_CLIP] + " …[clipped to fit the 400 KB store]"}
            clipped.append(call)
        message["tool_calls"] = clipped
    out["messages"] = messages
    text = json.dumps(out)
    trimmed = int(out.get("trimmed_messages") or 0)
    while len(text.encode("utf-8")) > limit and len(messages) > 2:
        messages.pop(0)
        trimmed += 1
        # Start on a question so the shown history never opens mid-exchange.
        while messages and isinstance(messages[0], dict) and messages[0].get("type") != "human" and len(messages) > 2:
            messages.pop(0)
            trimmed += 1
        out["trimmed_messages"] = trimmed
        text = json.dumps(out)
    return text


class ThreadRepository(DynamoDBRepository):
    """Thread repository using DynamoDB"""

    @staticmethod
    def _index_fields(thread: Thread, item: Dict[str, Any]) -> Dict[str, Any]:
        """Add the list-index attributes and drop the empty ones.

        DynamoDB rejects an empty string as a secondary-index key, so an
        unowned or not-yet-pinned thread must omit `owner_sub`/`agent_record_id`
        rather than store "" (a read gives "" back either way, see _to_thread)."""
        item["scope"] = SCOPE_ALL
        item["summary"] = json.dumps(summary_of(thread.values), ensure_ascii=False)
        item["values"] = fit_values(thread.values)
        for key in ("owner_sub", "agent_record_id"):
            if not item.get(key):
                item.pop(key, None)
        return item

    def create(self, thread: Thread) -> Thread:
        """Create a new thread"""
        item = {
            "thread_id": thread.thread_id,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            "status": thread.status.value if isinstance(thread.status, ThreadStatus) else thread.status,
            "values": json.dumps(thread.values) if thread.values else "{}",
            "metadata": json.dumps(thread.metadata) if thread.metadata else "{}",
            "owner_sub": thread.owner_sub or "",
            "agent_record_id": thread.agent_record_id or "",
            "agent_name": thread.agent_name or "",
        }
        if thread.basic_chat_model_id:
            item["basic_chat_model_id"] = thread.basic_chat_model_id
        if thread.agent_target:
            item["agent_target"] = {k: v for k, v in thread.agent_target.items() if v}
        self.table.put_item(Item=self._index_fields(thread, item))
        return thread

    def get(self, thread_id: str) -> Optional[Thread]:
        """Get thread by ID"""
        try:
            response = self.table.get_item(Key={"thread_id": thread_id})
            if "Item" not in response:
                return None

            return self._to_thread(response["Item"])
        except ClientError:
            return None

    @staticmethod
    def _to_thread(item: Dict[str, Any]) -> Thread:
        """Build a Thread from a raw DynamoDB item.

        Shared by get() and search() so the owner and agent fields can never be
        read back on one path and dropped on the other.
        """
        return Thread(
            thread_id=item["thread_id"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            status=item.get("status", "idle"),
            values=json.loads(item.get("values", "{}")),
            metadata=json.loads(item.get("metadata", "{}")),
            owner_sub=item.get("owner_sub", ""),
            agent_record_id=item.get("agent_record_id", ""),
            agent_name=item.get("agent_name", ""),
            basic_chat_model_id=item.get("basic_chat_model_id") or None,
            agent_target=item.get("agent_target") or None,
        )

    def update(self, thread_id: str, thread: Thread) -> Thread:
        """Update thread"""
        # This is a whole-item put_item, not an UpdateExpression: any attribute
        # missing from this dict is erased. `owner_sub` therefore has to be
        # carried explicitly, or the first PATCH /state after creation would
        # silently unown the thread and every later read would 403. The agent
        # fields have the same hazard with a quieter symptom: dropping
        # agent_record_id unpins the thread, and the next turn from a different
        # agent would be accepted into it.
        item = {
            "thread_id": thread.thread_id,
            "updated_at": thread.updated_at,
            "status": thread.status.value if isinstance(thread.status, ThreadStatus) else thread.status,
            "values": json.dumps(thread.values) if thread.values else "{}",
            "metadata": json.dumps(thread.metadata) if thread.metadata else "{}",
            "owner_sub": thread.owner_sub or "",
            "agent_record_id": thread.agent_record_id or "",
            "agent_name": thread.agent_name or "",
        }
        if thread.basic_chat_model_id:
            item["basic_chat_model_id"] = thread.basic_chat_model_id
        if thread.agent_target:
            item["agent_target"] = {k: v for k, v in thread.agent_target.items() if v}
        # Preserve created_at - use thread's created_at if available, otherwise try to get from existing
        if thread.created_at:
            item["created_at"] = thread.created_at
        else:
            # Only fetch existing if created_at is not provided
            existing = self.get(thread_id)
            if existing:
                item["created_at"] = existing.created_at
            else:
                item["created_at"] = datetime.utcnow().isoformat()

        self.table.put_item(Item=self._index_fields(thread, item))
        return thread

    def delete(self, thread_id: str) -> None:
        """Delete thread"""
        self.table.delete_item(Key={"thread_id": thread_id})

    def search(
        self,
        owner_sub: Optional[str],
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        status: Optional[ThreadStatus] = None,
        metadata: Optional[Dict[str, Any]] = None,
        agent_record_id: Optional[str] = None,
    ) -> List[Thread]:
        """Threads owned by `owner_sub`, or all of them when it is None.

        `owner_sub` is positional and has no default on purpose: `None` has to
        be passed deliberately, so a caller that simply forgets the argument
        gets a TypeError rather than a listing of everyone's threads. Only the
        admin path passes None.

        `agent_record_id` narrows to one agent's conversations (the insights
        drill-down); with an owner as well, to that owner's.

        A list row, not a thread: `values` holds only the first question and
        answer (`summary_of`), which is all the sidebar shows. Open a thread
        with `get` for its transcript. This is one Query on a list index; it
        used to Scan the whole table and BatchGet every row's transcript.
        """
        if sort_by == "updated_at":
            try:
                return self._index_search(owner_sub, limit, offset, sort_order, status, metadata, agent_record_id)
            except ClientError as exc:
                # A table created before the indexes existed. Slow, but correct.
                if exc.response.get("Error", {}).get("Code") != "ValidationException":
                    print(f"Error listing threads: {exc}")
                    return []
        return self._scan_search(
            owner_sub, limit=limit, offset=offset, sort_by=sort_by, sort_order=sort_order,
            status=status, metadata=metadata, agent_record_id=agent_record_id,
        )

    def _index_search(self, owner_sub, limit, offset, sort_order, status, metadata, agent_record_id):
        if agent_record_id:
            index, key = AGENT_INDEX, Key("agent_record_id").eq(agent_record_id)
        elif owner_sub is not None:
            index, key = OWNER_INDEX, Key("owner_sub").eq(owner_sub)
        else:
            index, key = SCOPE_INDEX, Key("scope").eq(SCOPE_ALL)
        filters = []
        if agent_record_id and owner_sub is not None:
            filters.append(Attr("owner_sub").eq(owner_sub))
        if status:
            filters.append(Attr("status").eq(status.value if isinstance(status, ThreadStatus) else status))
        want = offset + limit
        kwargs: Dict[str, Any] = {
            "IndexName": index,
            "KeyConditionExpression": key,
            "ScanIndexForward": sort_order == "asc",
            # Limit counts items read before the filter; a little headroom keeps
            # an unfiltered first page to a single round trip.
            "Limit": min(want + 20, 1000),
        }
        if filters:
            expression = filters[0]
            for extra in filters[1:]:
                expression = expression & extra
            kwargs["FilterExpression"] = expression
        rows: List[Dict[str, Any]] = []
        while len(rows) < want:
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                if metadata and not self._metadata_matches(item, metadata):
                    continue
                rows.append(item)
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        page = rows[offset:want]
        # Threads written before `summary` existed: read those few in full.
        legacy = [r["thread_id"] for r in page if "summary" not in r]
        full = {i["thread_id"]: i for i in self._get_many(legacy)} if legacy else {}
        out = []
        for row in page:
            if row["thread_id"] in full:
                item = full[row["thread_id"]]
                summary = summary_of(json.loads(item.get("values", "{}")))
            else:
                item = row
                summary = json.loads(row["summary"])
            out.append(self._to_list_thread(item, summary))
        return out

    @staticmethod
    def _metadata_matches(item: Dict[str, Any], metadata: Dict[str, Any]) -> bool:
        item_metadata = json.loads(item.get("metadata", "{}"))
        return all(item_metadata.get(k) == v for k, v in metadata.items())

    @staticmethod
    def _to_list_thread(item: Dict[str, Any], summary: Dict[str, Any]) -> Thread:
        return Thread(
            thread_id=item["thread_id"],
            created_at=item.get("created_at", ""),
            updated_at=item["updated_at"],
            status=item.get("status", "idle"),
            values=summary,
            metadata=json.loads(item.get("metadata", "{}")),
            owner_sub=item.get("owner_sub", ""),
            agent_record_id=item.get("agent_record_id", ""),
            agent_name=item.get("agent_name", ""),
        )

    def _scan_search(
        self,
        owner_sub: Optional[str],
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        status: Optional[ThreadStatus] = None,
        metadata: Optional[Dict[str, Any]] = None,
        agent_record_id: Optional[str] = None,
    ) -> List[Thread]:
        """The pre-index listing: a full-table Scan, sorted and sliced in memory.

        Kept for what the indexes do not cover — sorting by `created_at`, and a
        stack whose table predates them (Query on a missing index raises).
        """
        filters = []
        if owner_sub is not None:
            filters.append(Attr("owner_sub").eq(owner_sub))
        if status:
            status_value = status.value if isinstance(status, ThreadStatus) else status
            filters.append(Attr("status").eq(status_value))
        if agent_record_id:
            filters.append(Attr("agent_record_id").eq(agent_record_id))

        # Filtering server-side rather than after the fact: a thread the caller
        # may not see must never be materialised into a Thread object here.
        scan_kwargs = {}
        if filters:
            expression = filters[0]
            for extra in filters[1:]:
                expression = expression & extra
            scan_kwargs["FilterExpression"] = expression

        # Only what the filter, sort and slice need. `values` is the whole
        # transcript — 7.4MB of the live table's 7.5MB, 343 threads, one a 408KB
        # item — and a listing of 20 used to read and deserialise all of it on
        # every sidebar load. The page's full items are fetched afterwards.
        scan_kwargs["ProjectionExpression"] = "thread_id, created_at, updated_at, metadata"

        try:
            # A single scan() stops at DynamoDB's 1MB page limit, so follow
            # LastEvaluatedKey — otherwise threads past the first page silently
            # vanish from the list. Sorting happens in-memory below, so every
            # page has to be collected before the offset/limit slice is correct.
            threads_data = []
            while True:
                response = self.table.scan(**scan_kwargs)
                threads_data.extend(response.get("Items", []))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key

            # Filter by metadata if provided
            if metadata:
                filtered = []
                for item in threads_data:
                    item_metadata = json.loads(item.get("metadata", "{}"))
                    match = True
                    for key, value in metadata.items():
                        if item_metadata.get(key) != value:
                            match = False
                            break
                    if match:
                        filtered.append(item)
                threads_data = filtered

            # Sort (in-memory)
            reverse = sort_order == "desc"
            if sort_by in ("updated_at", "created_at"):
                threads_data.sort(key=lambda i: i[sort_by], reverse=reverse)

            # Paginate
            page = [i["thread_id"] for i in threads_data[offset : offset + limit]]
            return [self._to_thread(item) for item in self._get_many(page)]

        except ClientError as e:
            print(f"Error scanning threads: {e}")
            return []

    def owners_of(self, thread_ids: List[str]) -> Dict[str, str]:
        """thread id -> owner sub for those of `thread_ids` that exist and are
        owned. 100 ids per BatchGetItem, only the two attributes."""
        found: Dict[str, str] = {}
        ids = list(dict.fromkeys(t for t in thread_ids if t))
        for i in range(0, len(ids), 100):
            request = {self.table_name: {
                "Keys": [{"thread_id": t} for t in ids[i : i + 100]],
                "ProjectionExpression": "thread_id, owner_sub",
            }}
            while request:
                response = self.dynamodb.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    if item.get("owner_sub"):
                        found[item["thread_id"]] = item["owner_sub"]
                request = response.get("UnprocessedKeys") or {}
        return found

    def _get_many(self, thread_ids: List[str]) -> List[Dict[str, Any]]:
        """Full items for `thread_ids`, in that order.

        BatchGetItem takes 100 keys per call (the route caps `limit` at 100)
        and may hand some back as UnprocessedKeys under throttling, which are
        retried. A thread deleted since the scan is simply absent.
        """
        found: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(thread_ids), 100):
            request = {self.table_name: {"Keys": [{"thread_id": t} for t in thread_ids[i : i + 100]]}}
            while request:
                response = self.dynamodb.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    found[item["thread_id"]] = item
                request = response.get("UnprocessedKeys") or {}
        return [found[t] for t in thread_ids if t in found]










