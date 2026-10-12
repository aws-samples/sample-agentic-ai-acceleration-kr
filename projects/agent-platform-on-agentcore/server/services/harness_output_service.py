"""Collects the files a harness turn left in its sandbox.

A harness executes its tools inside AWS, so nothing the agent writes ever passes
through this server — the existing artifact pipeline only sees files because a
*runtime* agent emits them inline. This service closes that gap from the other
side: after the turn it runs two fixed programs inside the agent's own container
(see harness_sandbox_scripts), one to list candidate files and one to PUT them
straight to S3 with presigned URLs the server minted.

Bytes never come back through the command channel; that was measured to be
unusable (60KB of base64 on stdout never completed, 180s read timeout). They also
never pass through this process: the sandbox uploads to S3 itself, 3MB in 0.07s.

The diff is what keeps this cheap and idempotent. `/home` lives for the whole
session, so every turn lists the same files; a file is uploaded only when its
(path, size, mtime) does not match a row already stored for the thread. That also
means a re-run after an interruption cannot duplicate anything, which is what
makes the delayed retry safe.
"""
import asyncio
import base64
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from botocore.exceptions import ClientError

from agents.agentcore_client import AgentCoreClient
from agents.harness_command_client import UPLOAD_BATCH_SIZE, HarnessCommandClient
from core.config import (
    HARNESS_OUTPUT_COMMAND_TIMEOUT,
    HARNESS_OUTPUT_EXTENSIONS,
    HARNESS_OUTPUT_MAX_FILE_BYTES,
    HARNESS_OUTPUT_MAX_FILES,
    HARNESS_OUTPUT_ROOTS,
    HARNESS_OUTPUT_SWEEP,
)
from models.artifact import ArtifactVersion, extension_for
from services.artifact_service import scoped_id
from services.harness_sandbox_scripts import (
    decode_output,
    list_command,
    upload_command,
)

logger = logging.getLogger(__name__)

# When a turn was interrupted the agent is usually still working: on 2026-08-14
# the client disconnected at 04:58:38 while the harness kept running tools until
# 05:00:06. Sweeping immediately finds nothing, so the recovery sweep retries.
# The last attempt lands at 10 minutes, inside the 900s idle session window.
RETRY_DELAYS_SECONDS = (30, 90, 180, 300)


@dataclass(frozen=True)
class SweptFile:
    path: str
    size: int
    mtime: int


@dataclass(frozen=True)
class PlannedUpload:
    artifact_id: str
    version: int
    s3_key: str
    path: str
    filename: str
    size: int
    mtime: int


def plan_uploads(
    thread_id: str,
    listing: Sequence[SweptFile],
    known: Sequence[ArtifactVersion],
    key_for: Callable[[str, str, int, str], str],
) -> List[PlannedUpload]:
    """Decide which listed files need uploading, and as which version.

    `key_for` is `ArtifactService.key_for`: key construction belongs to the
    artifact service, but the decision of *which* key belongs here, where the
    version number is worked out. Rows without a `source_path` are the runtime
    path's inline artifacts and are ignored — they describe no file in any
    sandbox.
    """
    latest: Dict[str, ArtifactVersion] = {}
    for row in known:
        if not row.source_path:
            continue
        current = latest.get(row.source_path)
        if current is None or row.version > current.version:
            latest[row.source_path] = row

    planned: List[PlannedUpload] = []
    for entry in listing:
        previous = latest.get(entry.path)
        if (
            previous
            and previous.size_bytes == entry.size
            and previous.source_mtime == entry.mtime
        ):
            continue

        filename = entry.path.rsplit("/", 1)[-1]
        artifact_id = previous.artifact_id if previous else scoped_id(thread_id, entry.path)
        version = (previous.version + 1) if previous else 1
        planned.append(
            PlannedUpload(
                artifact_id=artifact_id,
                version=version,
                s3_key=key_for(
                    thread_id,
                    artifact_id,
                    version,
                    extension_for("file", None, filename),
                ),
                path=entry.path,
                filename=filename,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
    return planned


class HarnessOutputService:
    def __init__(
        self,
        command_client: Optional[HarnessCommandClient] = None,
        artifact_service: Any = None,
        *,
        preview_service: Optional[Any] = None,
        sweep_enabled: bool = HARNESS_OUTPUT_SWEEP,
        roots: Optional[List[str]] = None,
        extensions: Optional[List[str]] = None,
        max_file_bytes: int = HARNESS_OUTPUT_MAX_FILE_BYTES,
        max_files: int = HARNESS_OUTPUT_MAX_FILES,
        command_timeout: int = HARNESS_OUTPUT_COMMAND_TIMEOUT,
    ):
        self.command_client = command_client or HarnessCommandClient()
        self.artifact_service = artifact_service
        self.preview_service = preview_service
        self.sweep_enabled = sweep_enabled
        self.roots = roots if roots is not None else HARNESS_OUTPUT_ROOTS
        self.extensions = (
            extensions if extensions is not None else HARNESS_OUTPUT_EXTENSIONS
        )
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.command_timeout = command_timeout
        # A missing IAM grant is a deployment fact, not a per-turn failure: report
        # it once and stop trying, or every turn pays a round trip to be denied.
        self._denied = False

    @property
    def enabled(self) -> bool:
        return bool(
            self.sweep_enabled
            and self.roots
            and self.extensions
            and self.artifact_service is not None
            and getattr(self.artifact_service, "enabled", False)
        )

    # --- entry points -------------------------------------------------------

    async def sweep(
        self, thread_id: str, harness_arn: str, message_id: Optional[str] = None
    ) -> List[ArtifactVersion]:
        """Awaited from the live stream. Off the loop's thread throughout."""
        return await asyncio.to_thread(self.sweep_sync, thread_id, harness_arn, message_id)

    def sweep_sync(
        self, thread_id: str, harness_arn: str, message_id: Optional[str] = None
    ) -> List[ArtifactVersion]:
        """One collection pass. Never raises: this is best-effort recovery."""
        if not self.enabled or self._denied:
            return []
        try:
            listing = self._list(thread_id, harness_arn)
            if not listing:
                return []
            planned = plan_uploads(
                thread_id,
                listing,
                self.artifact_service.list_for_thread(thread_id),
                self.artifact_service.key_for,
            )
            if not planned:
                return []
            return self._upload_and_register(
                thread_id, harness_arn, planned, message_id
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if "AccessDenied" in code:
                self._denied = True
                logger.error(
                    "Harness output sweep disabled: the server role lacks "
                    "bedrock-agentcore:InvokeAgentRuntimeCommand on %s (%s)",
                    harness_arn,
                    exc,
                )
            else:
                logger.warning("Harness output sweep failed on %s: %s", thread_id, exc)
            return []
        except Exception as exc:
            logger.warning(
                "Harness output sweep failed on %s: %s", thread_id, exc, exc_info=True
            )
            return []

    def spawn_delayed_sweep(
        self, thread_id: str, harness_arn: str, message_id: Optional[str] = None
    ) -> None:
        """Recover a turn that ended without a live stream to sweep into.

        Detached because the interrupted path cannot await — a closing generator
        has no loop to await on — and because the agent is usually still working
        when the client disconnects. Nothing is emitted anywhere; the file surfaces
        on the next load, which is why `message_id` matters here as much as on the
        live path: the chat hangs a card off the message the file belongs to.
        """
        if not self.enabled or self._denied:
            return
        threading.Thread(
            target=self._retry_sweep,
            args=(thread_id, harness_arn, message_id),
            name=f"harness-sweep-{thread_id}",
            daemon=True,
        ).start()

    def _retry_sweep(
        self, thread_id: str, harness_arn: str, message_id: Optional[str] = None
    ) -> None:
        empty_rounds = 0
        for delay in RETRY_DELAYS_SECONDS:
            time.sleep(delay)
            if self._denied:
                return
            collected = self.sweep_sync(thread_id, harness_arn, message_id)
            if collected:
                empty_rounds = 0
                logger.info(
                    "Recovered %d file(s) from the interrupted turn on %s",
                    len(collected),
                    thread_id,
                )
                continue
            empty_rounds += 1
            if empty_rounds >= 2:
                # Two quiet rounds: either the agent produced nothing or the
                # session is gone. Either way there is nothing left to wait for.
                return

    # --- steps --------------------------------------------------------------

    def _session_id(self, thread_id: str) -> str:
        # The same derivation the turn used, so the command lands in the session
        # that holds the files.
        return AgentCoreClient._session_id(thread_id)

    def _list(self, thread_id: str, harness_arn: str) -> List[SweptFile]:
        result = self.command_client.run_sync(
            harness_arn,
            self._session_id(thread_id),
            list_command(
                self.roots, self.extensions, self.max_file_bytes, self.max_files
            ),
            timeout=self.command_timeout,
        )
        if not result.ok:
            logger.info(
                "Sandbox listing on %s did not succeed (exit=%s status=%s): %s",
                thread_id,
                result.exit_code,
                result.status,
                (result.stderr or "")[:200],
            )
            return []

        payload = decode_output(result.stdout)
        for entry in payload.get("dropped", []):
            logger.warning(
                "Harness output on %s not collected (%s): %s",
                thread_id,
                entry.get("why"),
                _decode_path(entry.get("p", "")),
            )
        return [
            SweptFile(
                path=_decode_path(entry["p"]),
                size=int(entry["s"]),
                mtime=int(entry["m"]),
            )
            for entry in payload.get("files", [])
        ]

    def _upload_and_register(
        self,
        thread_id: str,
        harness_arn: str,
        planned: List[PlannedUpload],
        message_id: Optional[str],
    ) -> List[ArtifactVersion]:
        uploads = [
            {
                "p": base64.b64encode(item.path.encode("utf-8")).decode("ascii"),
                "url": self.artifact_service.presign_put(item.s3_key),
            }
            for item in planned
        ]

        delivered = set()
        session_id = self._session_id(thread_id)
        # Batched because presigned URLs are ~1KB each and the request body has a
        # limit; UPLOAD_BATCH_SIZE is the measured safe size.
        for start in range(0, len(uploads), UPLOAD_BATCH_SIZE):
            batch = uploads[start : start + UPLOAD_BATCH_SIZE]
            result = self.command_client.run_sync(
                harness_arn,
                session_id,
                upload_command(batch, timeout=self.command_timeout),
                timeout=self.command_timeout,
            )
            if not result.ok:
                logger.warning(
                    "Upload command failed on %s (exit=%s): %s",
                    thread_id,
                    result.exit_code,
                    (result.stderr or "")[:200],
                )
                continue
            for entry in decode_output(result.stdout).get("uploads", []):
                if entry.get("status") == 200:
                    delivered.add(_decode_path(entry["p"]))
                else:
                    logger.warning(
                        "Sandbox could not upload %s: %s",
                        _decode_path(entry.get("p", "")),
                        entry.get("error") or entry.get("status"),
                    )

        registered: List[ArtifactVersion] = []
        for item in planned:
            # Only what actually landed: a row for a missing object would show the
            # user a download that 404s.
            if item.path not in delivered:
                continue
            record = self.artifact_service.register_stored(
                thread_id,
                artifact_id=item.artifact_id,
                version=item.version,
                filename=item.filename,
                s3_key=item.s3_key,
                size_bytes=item.size,
                source_path=item.path,
                source_mtime=item.mtime,
                message_id=message_id,
            )
            registered.append(record)
            if self.preview_service:
                # After registration, never before: the download must work even
                # if no preview ever appears.
                self.preview_service.spawn(record)
        return registered


def _decode_path(encoded: str) -> str:
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return "<undecodable path>"
