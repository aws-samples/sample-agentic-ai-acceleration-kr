"""Artifact repository — one DynamoDB item per artifact version."""
from typing import List, Optional

from boto3.dynamodb.conditions import Key

from models.artifact import ArtifactVersion
from repositories.base import DynamoDBRepository

THREAD_INDEX = "thread_id-created_at-index"


def _to_model(item: dict) -> ArtifactVersion:
    return ArtifactVersion(
        artifact_id=item["artifact_id"],
        version=int(item["version"]),
        thread_id=item["thread_id"],
        title=item.get("title", ""),
        kind=item.get("kind", "text"),
        language=item.get("language") or None,
        s3_key=item["s3_key"],
        size_bytes=int(item.get("size_bytes", 0)),
        created_at=item.get("created_at", ""),
        tool_call_id=item.get("tool_call_id") or None,
        message_id=item.get("message_id") or None,
        filename=item.get("filename") or None,
        content_type=item.get("content_type") or None,
        source_path=item.get("source_path") or None,
        source_mtime=int(item["source_mtime"]) if item.get("source_mtime") else None,
        preview_key=item.get("preview_key") or None,
    )


class ArtifactRepository(DynamoDBRepository):
    """Artifact version storage in DynamoDB."""

    def put_version(self, artifact: ArtifactVersion) -> ArtifactVersion:
        item = {
            "artifact_id": artifact.artifact_id,
            "version": artifact.version,
            "thread_id": artifact.thread_id,
            "title": artifact.title,
            "kind": artifact.kind,
            "s3_key": artifact.s3_key,
            "size_bytes": artifact.size_bytes,
            "created_at": artifact.created_at,
        }
        for key in (
            "language",
            "tool_call_id",
            "message_id",
            "filename",
            "content_type",
            "source_path",
            "source_mtime",
            "preview_key",
        ):
            value = getattr(artifact, key)
            if value:
                item[key] = value
        self.table.put_item(Item=item)
        return artifact

    def latest(self, artifact_id: str) -> Optional[ArtifactVersion]:
        response = self.table.query(
            KeyConditionExpression=Key("artifact_id").eq(artifact_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return _to_model(items[0]) if items else None

    def get_version(self, artifact_id: str, version: int) -> Optional[ArtifactVersion]:
        response = self.table.get_item(Key={"artifact_id": artifact_id, "version": version})
        item = response.get("Item")
        return _to_model(item) if item else None

    def list_versions(self, artifact_id: str) -> List[ArtifactVersion]:
        response = self.table.query(
            KeyConditionExpression=Key("artifact_id").eq(artifact_id),
            ScanIndexForward=False,
        )
        return [_to_model(item) for item in response.get("Items", [])]

    def list_by_thread(self, thread_id: str) -> List[ArtifactVersion]:
        """Every artifact version created in a thread, newest first."""
        response = self.table.query(
            IndexName=THREAD_INDEX,
            KeyConditionExpression=Key("thread_id").eq(thread_id),
            ScanIndexForward=False,
        )
        return [_to_model(item) for item in response.get("Items", [])]

    def set_preview_key(self, artifact_id: str, version: int, preview_key: str) -> None:
        """Attach a preview to an already-stored version.

        A targeted update rather than a re-put: extraction runs after the row is
        written, and a full put would race a concurrent write of the same row.
        """
        self.table.update_item(
            Key={"artifact_id": artifact_id, "version": version},
            UpdateExpression="SET preview_key = :key",
            ExpressionAttributeValues={":key": preview_key},
        )
