"""Serves browser screenshots back into a reopened chat.

The `browser_screenshot` tool runs in the built-in-tools gateway Lambda, not here:
it writes the PNG to its own bucket and returns a presigned URL in the tool result
(infra/builtin_tools_gateway/lambda/handler.py). That URL is what makes the image
appear the moment the tool call lands, but it lasts an hour, so a thread reopened
the next day would show a broken image — the same expiry problem the chart events
already document in EventFormatter.chart.

The fix is the key. The Lambda returns it as `s3_key` alongside the URL, the
stored tool call keeps it, and this service reads the object back on request.

That widens the window rather than removing it: the screenshot bucket expires
objects under `screenshots/` after 7 days (infra/builtin_tools_gateway/deploy.py,
"expire-screenshots"), so the honest guarantee is one hour without this and seven
days with it. Older than that is a 404 by design — screenshots are breadcrumbs,
and keeping them forever would be a storage bill for a debugging aid. A thread
that needs its images to outlive the week wants an artifact, not a screenshot.

Nothing here takes a key from the caller. The route addresses a screenshot by
(thread_id, tool_call_id) and the key is looked up in that thread's own stored
messages, so thread ownership is the whole authorisation story — a screenshot
cannot be pulled out of a thread the caller may not open. That matters more than
it looks: the key embeds the gateway session label, which is derived from the
caller's identity, and those labels are short enough to guess.
"""
import json
import logging
from typing import Any, Dict, List, Optional

import boto3

from core.config import AWS_REGION, BROWSER_SCREENSHOT_BUCKET

logger = logging.getLogger(__name__)

# The prefix the gateway Lambda writes under. Enforced on read so a malformed or
# tampered tool result cannot address an unrelated object in the same bucket.
SCREENSHOT_PREFIX = "screenshots/"


class ScreenshotsNotConfigured(Exception):
    """Raised when no screenshot bucket is set on the server."""


class ScreenshotNotFound(Exception):
    """Raised when a thread holds no such screenshot, or its object has gone."""


def screenshot_key(result: Any) -> Optional[str]:
    """The S3 key a `browser_screenshot` tool result points at, if any.

    The result reaches the server as the JSON string the gateway returned — a
    single plain object, not double-encoded, and complete by the time it is
    stored (HarnessEventAdapter accumulates the fragments a long presigned URL is
    split across). It is parsed defensively all the same: every other tool's
    result arrives through the same field, so most calls here are expected to
    return None rather than to fail.
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return None
    if not isinstance(result, dict):
        return None

    key = result.get("s3_key")
    if not isinstance(key, str) or not key.startswith(SCREENSHOT_PREFIX):
        return None
    # A traversal cannot escape an S3 prefix the way it can a filesystem path,
    # but the key is echoed into logs and errors, so refuse the odd shapes.
    if ".." in key:
        return None
    return key


def find_screenshot_key(
    messages: List[Dict[str, Any]], tool_call_id: str
) -> Optional[str]:
    """Walk a thread's stored messages for one tool call's screenshot key.

    Newest first: a tool call id is unique in practice, but a thread reloaded and
    re-run could repeat one, and the recent screenshot is the one being asked for.
    """
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") == tool_call_id:
                key = screenshot_key(call.get("result"))
                if key:
                    return key
    return None


class BrowserScreenshotService:
    def __init__(self, bucket: Optional[str] = None, region: Optional[str] = None):
        self.bucket = bucket if bucket is not None else BROWSER_SCREENSHOT_BUCKET
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

    def fetch(self, messages: List[Dict[str, Any]], tool_call_id: str) -> bytes:
        """The PNG one tool call captured, read out of the screenshot bucket."""
        if not self.enabled:
            raise ScreenshotsNotConfigured(
                "Browser screenshot storage is not configured on the server. Set "
                "BROWSER_SCREENSHOT_BUCKET to the bucket the built-in-tools "
                "gateway writes to."
            )

        key = find_screenshot_key(messages, tool_call_id)
        if not key:
            raise ScreenshotNotFound(
                f"No browser screenshot stored for tool call {tool_call_id}"
            )

        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            # Lifecycle rules and manual cleanups both reach these objects, so a
            # missing one is ordinary rather than exceptional.
            logger.warning("Screenshot %s unreadable: %s", key, exc)
            raise ScreenshotNotFound(f"Screenshot {key} is no longer available")

        return obj["Body"].read()
