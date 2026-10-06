"""
Thread repository for data access
"""
import json
from typing import Dict, List, Optional, Any
from datetime import datetime
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr

from repositories.base import DynamoDBRepository
from models.thread import Thread
from models.common import ThreadStatus


class ThreadRepository(DynamoDBRepository):
    """Thread repository using DynamoDB"""

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
        self.table.put_item(Item=item)
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

        self.table.put_item(Item=item)
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

        `agent_record_id` narrows to one agent's conversations, which is what the
        insights drill-down asks for. It joins the same FilterExpression rather
        than filtering the returned list: the slice below is applied after the
        sort, so a post-filter would hand back fewer rows than the limit asked
        for and look like the agent had less traffic than it does.

        Still a scan. An `owner_sub` GSI would turn this into a query, but the
        sort and the offset/limit slice below happen in memory over the whole
        result set, so switching to a query changes pagination semantics too —
        that is a latency change, deliberately kept out of this one.
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

        threads = []
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

            # Convert to Thread objects
            for item in threads_data:
                threads.append(self._to_thread(item))

            # Sort (in-memory)
            reverse = sort_order == "desc"
            if sort_by == "updated_at":
                threads.sort(key=lambda t: t.updated_at, reverse=reverse)
            elif sort_by == "created_at":
                threads.sort(key=lambda t: t.created_at, reverse=reverse)

            # Paginate
            return threads[offset : offset + limit]

        except ClientError as e:
            print(f"Error scanning threads: {e}")
            return []










