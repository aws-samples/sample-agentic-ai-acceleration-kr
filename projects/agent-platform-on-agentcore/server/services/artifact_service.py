"""Persistence for agent-produced artifacts.

The runtime streams an `artifact` event with the content inline; this service
writes it to S3, records the version in DynamoDB, and hands the enriched event
back so the frontend can render immediately and later refetch by id.
"""
import asyncio
import hashlib
import logging
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import boto3
from botocore.config import Config

from core.config import ARTIFACTS_BUCKET, AWS_REGION
from models.artifact import (
    ArtifactContent,
    ArtifactDetail,
    ArtifactVersion,
    content_type_for,
    extension_for,
    is_binary_kind,
    normalize_kind,
)
from repositories.artifact_repository import ArtifactRepository

logger = logging.getLogger(__name__)

ARTIFACT_PREFIX = "artifacts"


class ArtifactNotText(Exception):
    """Raised when a text read is attempted on a binary artifact."""


def content_disposition(filename: str, fallback_id: str, inline: bool = False) -> str:
    """A `Content-Disposition` value that survives a non-ASCII filename.

    HTTP headers are latin-1: a Korean name in `filename=` makes the ASGI server
    raise UnicodeEncodeError mid-response and the download becomes a 500. RFC
    5987's `filename*` carries the real name percent-encoded, with an ASCII
    `filename` beside it for clients that ignore the extended form. A wholly
    non-ASCII name strips down to just its extension, so the artifact id is used
    rather than serving a file called ".docx".
    """
    stripped = (
        unicodedata.normalize("NFKD", filename or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    stem = stripped.rsplit(".", 1)[0] if "." in stripped else stripped
    ascii_fallback = stripped if stem else f"{fallback_id}{stripped}"
    kind = "inline" if inline else "attachment"
    return (
        f'{kind}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename or fallback_id, safe='')}"
    )


def scoped_id(thread_id: str, raw_id: Optional[str]) -> str:
    """Namespace the agent-chosen artifact id to its thread.

    The agent invents ids like "fib-py", which would collide across threads and
    let one conversation append versions to another's artifact.

    ASCII is enforced rather than assumed: `str.isalnum()` is true for Hangul, and
    a swept filename like `보고서.docx` reaches this function directly, so the old
    slug would have carried Korean into the S3 key. The digest of the raw id is
    appended to the slug when characters were dropped (for disambiguation when
    multiple inputs have the same ASCII skeleton), and slug length is reserved
    for it so the digest is never truncated away.
    """
    if not raw_id:
        return f"art_{uuid.uuid4().hex[:12]}"
    raw = raw_id.strip().lower()
    slug = "".join(
        c if (c.isascii() and c.isalnum()) or c in "-_" else "-" for c in raw
    )
    scope = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:8]
    if raw.isascii():
        slug = slug[:48]
    else:
        # Truncating *after* appending would chop off the very digest that keeps
        # two names apart, and a 48-character slug would lose it completely.
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[:39]}-{digest}"
    return f"{scope}_{slug or uuid.uuid4().hex[:12]}"


class ArtifactsNotConfigured(Exception):
    """Raised when artifact storage is requested but not configured."""


class ArtifactNotFound(Exception):
    """Raised when an artifact or version does not exist."""


class ArtifactService:
    def __init__(
        self,
        repository: Optional[ArtifactRepository] = None,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self.repository = repository
        self.bucket = bucket if bucket is not None else ARTIFACTS_BUCKET
        self.region = region or AWS_REGION
        self._s3 = None

    @property
    def s3(self):
        if self._s3 is None:
            # SigV4 explicitly: boto3's us-east-1 default is the legacy SigV2,
            # which folds the request's Content-Type into the string to sign. The
            # sandbox's uploader sends one and the presigner signs none, so a
            # SigV2 presigned PUT is rejected with SignatureDoesNotMatch —
            # measured against the deployed stack. Under SigV4 only `host` is
            # signed, which is what makes "apply the real content type on read"
            # safe.
            self._s3 = boto3.client(
                "s3", region_name=self.region, config=Config(signature_version="s3v4")
            )
        return self._s3

    @property
    def enabled(self) -> bool:
        return bool(self.bucket and self.repository)

    def _require_configured(self) -> None:
        if not self.enabled:
            raise ArtifactsNotConfigured(
                "Artifact storage is not configured on the server. Set "
                "ARTIFACTS_BUCKET and ARTIFACTS_TABLE from "
                "`terraform output artifacts_bucket` / `artifacts_table`."
            )

    # --- streaming ----------------------------------------------------------

    async def persist_and_enrich(self, thread_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        """Store the artifact carried by a runtime event and annotate the event.

        Never raises: a storage failure degrades to an unstored artifact that
        the panel can still render from the inline content.
        """
        artifact = dict(event.get("event", {}).get("artifact", {}))
        artifact["kind"] = normalize_kind(artifact.get("kind"))
        artifact.setdefault("title", "Untitled")
        artifact["threadId"] = thread_id
        artifact_id = scoped_id(thread_id, artifact.get("artifactId"))
        artifact["artifactId"] = artifact_id

        if not self.enabled:
            artifact["stored"] = False
            artifact.setdefault("version", 1)
            return {"event": {"artifact": artifact}}

        try:
            stored = await asyncio.to_thread(
                self._store,
                thread_id,
                artifact_id,
                artifact.get("title", "Untitled"),
                artifact["kind"],
                artifact.get("language"),
                artifact.get("content", ""),
                artifact.get("toolCallId"),
                artifact.get("messageId"),
            )
        except Exception as exc:
            logger.error("Failed to store artifact %s: %s", artifact_id, exc)
            artifact["stored"] = False
            artifact["storeError"] = str(exc)
            artifact.setdefault("version", 1)
            return {"event": {"artifact": artifact}}

        artifact.update(
            {
                "version": stored.version,
                "s3Key": stored.s3_key,
                "sizeBytes": stored.size_bytes,
                "createdAt": stored.created_at,
                "stored": True,
            }
        )
        return {"event": {"artifact": artifact}}

    def _store(
        self,
        thread_id: str,
        artifact_id: str,
        title: str,
        kind: str,
        language: Optional[str],
        content: str,
        tool_call_id: Optional[str],
        message_id: Optional[str],
    ) -> ArtifactVersion:
        previous = self.repository.latest(artifact_id)
        version = (previous.version + 1) if previous else 1
        body = content.encode("utf-8")
        extension = extension_for(kind, language)
        s3_key = f"{ARTIFACT_PREFIX}/{thread_id}/{artifact_id}/v{version}.{extension}"

        self.s3.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=body,
            ContentType=content_type_for(kind),
        )

        record = ArtifactVersion(
            artifact_id=artifact_id,
            version=version,
            thread_id=thread_id,
            title=title,
            kind=kind,
            language=language,
            s3_key=s3_key,
            size_bytes=len(body),
            created_at=datetime.utcnow().isoformat(),
            tool_call_id=tool_call_id,
            message_id=message_id,
        )
        self.repository.put_version(record)
        logger.info("Stored artifact %s v%s at s3://%s/%s", artifact_id, version, self.bucket, s3_key)
        return record

    # --- sweep support ------------------------------------------------------

    def key_for(
        self, thread_id: str, artifact_id: str, version: int, extension: str
    ) -> str:
        """The S3 key for one version. ASCII by construction.

        The filename never enters the key — only its extension, already stripped
        to ASCII alphanumerics by `extension_for` — so a Korean name cannot reach
        S3 metadata or make the key unprintable, and path traversal is impossible.
        """
        return f"{ARTIFACT_PREFIX}/{thread_id}/{artifact_id}/v{version}.{extension}"

    def presign_put(self, s3_key: str, expires_in: int = 300) -> str:
        """A URL the sandbox can PUT one object to, and nothing else.

        The key is built entirely here, so the agent's container cannot choose
        where its bytes land. Content type is deliberately not signed: the stored
        type is overridden on every read path, which removes any chance of an
        upload failing on a header mismatch.
        """
        self._require_configured()
        return self.s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": s3_key},
            ExpiresIn=expires_in,
        )

    def register_stored(
        self,
        thread_id: str,
        *,
        artifact_id: str,
        version: int,
        filename: str,
        s3_key: str,
        size_bytes: int,
        source_path: str,
        source_mtime: int,
        message_id: Optional[str] = None,
    ) -> ArtifactVersion:
        """Record a version whose object is already in S3.

        The presigned PUT has already delivered the bytes; a second `put_object`
        here would push the same file through the server for nothing.
        """
        self._require_configured()
        record = ArtifactVersion(
            artifact_id=artifact_id,
            version=version,
            thread_id=thread_id,
            title=filename,
            kind="file",
            s3_key=s3_key,
            size_bytes=size_bytes,
            created_at=datetime.utcnow().isoformat(),
            message_id=message_id,
            filename=filename,
            content_type=content_type_for("file", filename),
            source_path=source_path,
            source_mtime=source_mtime,
        )
        self.repository.put_version(record)
        logger.info(
            "Registered swept artifact %s v%s (%s, %d bytes) from %s",
            artifact_id,
            version,
            filename,
            size_bytes,
            source_path,
        )
        return record

    def binary_body(self, artifact_id: str, version: int) -> Tuple[bytes, str, str]:
        """Raw bytes plus the name and type to serve them under."""
        self._require_configured()
        record = self._require_version(artifact_id, version)
        body = self.s3.get_object(Bucket=self.bucket, Key=record.s3_key)["Body"].read()
        filename = record.filename or f"{artifact_id}.{extension_for(record.kind, record.language, record.filename)}"
        return body, filename, record.content_type or "application/octet-stream"

    # --- reads --------------------------------------------------------------

    def list_for_thread(self, thread_id: str) -> List[ArtifactVersion]:
        self._require_configured()
        return self.repository.list_by_thread(thread_id)

    def thread_of(self, artifact_id: str) -> str:
        """The thread that produced this artifact, for the ownership check.

        Routes need the owning thread before they read anything, so this is
        separate from detail(): it answers only "whose is this".
        """
        self._require_configured()
        versions = self.repository.list_versions(artifact_id)
        if not versions:
            raise ArtifactNotFound(f"Artifact {artifact_id} not found")
        return versions[0].thread_id

    def detail(self, artifact_id: str) -> ArtifactDetail:
        self._require_configured()
        versions = self.repository.list_versions(artifact_id)
        if not versions:
            raise ArtifactNotFound(f"Artifact {artifact_id} not found")
        return ArtifactDetail(latest=versions[0], versions=[v.version for v in versions])

    def content(self, artifact_id: str, version: int) -> ArtifactContent:
        self._require_configured()
        record = self._require_version(artifact_id, version)
        if is_binary_kind(record.kind):
            # Not a 500: this path ends in `.decode("utf-8")`, so a .docx would
            # surface as a UnicodeDecodeError instead of "this is not text".
            raise ArtifactNotText(
                f"Artifact {artifact_id} v{version} is a binary file; "
                "use the download route."
            )
        body = self.s3.get_object(Bucket=self.bucket, Key=record.s3_key)["Body"].read()
        return ArtifactContent(
            artifact_id=record.artifact_id,
            version=record.version,
            kind=record.kind,
            language=record.language,
            title=record.title,
            content=body.decode("utf-8"),
        )

    def share_url(
        self,
        artifact_id: str,
        version: int,
        expires_in: int = 604800,
        download: bool = False,
    ) -> Tuple[str, int]:
        """Presigned GET URL for one artifact version.

        S3 caps presigned URL lifetime at 7 days for sigv4, which is also the
        default here.
        """
        self._require_configured()
        record = self._require_version(artifact_id, version)
        expires_in = max(60, min(expires_in, 604800))
        params = {"Bucket": self.bucket, "Key": record.s3_key}
        if record.content_type:
            # The object was stored with a placeholder type (the sandbox's PUT is
            # not signed for one), so the real type is applied on read.
            params["ResponseContentType"] = record.content_type
        if download:
            filename = record.filename or (
                f"{record.title or artifact_id}."
                f"{extension_for(record.kind, record.language, record.filename)}"
            )
            params["ResponseContentDisposition"] = content_disposition(
                filename, artifact_id
            )
        url = self.s3.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_in
        )
        return url, expires_in

    def preview(self, artifact_id: str, version: int) -> Optional[Dict[str, str]]:
        """The stored preview text, or None when extraction produced nothing."""
        self._require_configured()
        record = self._require_version(artifact_id, version)
        if not record.preview_key:
            return None
        body = self.s3.get_object(Bucket=self.bucket, Key=record.preview_key)["Body"].read()
        # The media is the preview key's own extension — the writer chose it.
        return {
            "media": record.preview_key.rsplit(".", 1)[-1],
            "text": body.decode("utf-8", "replace"),
        }

    def _require_version(self, artifact_id: str, version: int) -> ArtifactVersion:
        record = self.repository.get_version(artifact_id, version)
        if not record:
            raise ArtifactNotFound(f"Artifact {artifact_id} v{version} not found")
        return record
