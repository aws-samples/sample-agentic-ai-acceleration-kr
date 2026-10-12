"""Persistence for user-attached files.

Bytes go to S3 on upload; the chat message carries only a small reference block.
That keeps the thread record well inside DynamoDB's 400 KB item limit and means
the browser never re-uploads an attachment on later turns, even though it still
sends the whole conversation each time.

Reuses the artifacts bucket under its own prefix — a second bucket would need its
own Terraform, IAM and lifecycle rules for no gain.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import boto3

from core.config import ARTIFACTS_BUCKET, AWS_REGION
from models.attachment import (
    MAX_BYTES,
    Attachment,
    UnsupportedAttachment,
    kind_and_format,
    sanitise_model_name,
)

logger = logging.getLogger(__name__)

ATTACHMENT_PREFIX = "attachments"

_MEDIA_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html",
    "txt": "text/plain",
    "md": "text/markdown",
}


class AttachmentsNotConfigured(Exception):
    """Raised when attachment storage is requested but no bucket is set."""


class AttachmentNotFound(Exception):
    """Raised when an attachment's object is absent."""


class AttachmentService:
    def __init__(
        self,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self.bucket = bucket if bucket is not None else ARTIFACTS_BUCKET
        self.region = region or AWS_REGION
        self._s3 = None

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)

    def _require_configured(self) -> None:
        if not self.enabled:
            raise AttachmentsNotConfigured(
                "Attachment storage is not configured on the server. Set "
                "ARTIFACTS_BUCKET from `terraform output artifacts_bucket`."
            )

    @staticmethod
    def _key(thread_id: str, attachment_id: str, fmt: str) -> str:
        return f"{ATTACHMENT_PREFIX}/{thread_id}/{attachment_id}.{fmt}"

    def store(self, thread_id: str, filename: str, body: bytes) -> Attachment:
        """Validate and write one file, returning what the UI needs to show it."""
        self._require_configured()

        if len(body) > MAX_BYTES:
            raise UnsupportedAttachment(
                f"'{filename}' is too large ({len(body) // 1024} KB). "
                f"The limit is {MAX_BYTES // 1024} KB."
            )

        kind, fmt = kind_and_format(filename, body)
        attachment_id = f"att_{uuid.uuid4().hex[:12]}"
        media_type = _MEDIA_TYPES.get(fmt, "application/octet-stream")

        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(thread_id, attachment_id, fmt),
            Body=body,
            ContentType=media_type,
            # The original filename rides along so a fetch can name the download
            # without a second store to consult. Percent-encoded because S3
            # metadata is ASCII-only: botocore rejects the request outright
            # otherwise, and "보고서.pdf" is an ordinary filename here.
            Metadata={
                "filename": quote(filename, safe=""),
                "kind": kind,
                "format": fmt,
            },
        )

        return Attachment(
            attachment_id=attachment_id,
            filename=filename,
            media_type=media_type,
            size_bytes=len(body),
            kind=kind,
        )

    def _find(self, thread_id: str, attachment_id: str) -> Tuple[bytes, Dict[str, str], str]:
        """Read an object without knowing its extension up front."""
        self._require_configured()

        for fmt in _MEDIA_TYPES:
            try:
                obj = self.s3.get_object(
                    Bucket=self.bucket, Key=self._key(thread_id, attachment_id, fmt)
                )
            except Exception:
                continue
            return obj["Body"].read(), obj.get("Metadata") or {}, fmt

        raise AttachmentNotFound(f"Attachment {attachment_id} not found")

    def fetch(self, thread_id: str, attachment_id: str) -> Tuple[bytes, str, str]:
        body, metadata, fmt = self._find(thread_id, attachment_id)
        return (
            body,
            _MEDIA_TYPES.get(fmt, "application/octet-stream"),
            unquote(metadata.get("filename") or "") or f"{attachment_id}.{fmt}",
        )

    def model_blocks(
        self, thread_id: str, refs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Turn reference blocks into Strands content blocks, bytes included.

        A reference whose object has gone is skipped with a warning rather than
        raising: losing one image is a worse answer, losing the turn is an outage.
        """
        blocks: List[Dict[str, Any]] = []

        for ref in refs:
            attachment_id = ref.get("attachment_id")
            if not attachment_id:
                continue

            try:
                body, metadata, fmt = self._find(thread_id, attachment_id)
            except Exception as exc:
                logger.warning("Skipping attachment %s: %s", attachment_id, exc)
                continue

            kind = ref.get("kind") or metadata.get("kind") or "document"
            if kind == "image":
                blocks.append({"image": {"format": fmt, "source": {"bytes": body}}})
            else:
                filename = (
                    ref.get("filename")
                    or unquote(metadata.get("filename") or "")
                    or "document"
                )
                blocks.append(
                    {
                        "document": {
                            "format": fmt,
                            "name": sanitise_model_name(filename),
                            "source": {"bytes": body},
                        }
                    }
                )

        return blocks
