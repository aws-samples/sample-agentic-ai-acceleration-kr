# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

BodyLogStatus = Literal["success", "error", "partial"]


class BodyLogRecord(BaseModel):
    """Raw request/response body record shipped to Firehose -> S3 (JSONL).

    Joins back to usage.usage_logs by ``request_id``. One record per request,
    including errors (non-2xx) and partial (stream disconnect) cases.

    **This is the source of truth for request/response BODIES on every track**, not
    just mantle:

    * ``bedrock-mantle`` (Cowork, and the codex mantle aliases) is not captured by
      ``put-model-invocation-logging-configuration`` at all, so there is no AWS-side
      record to compare against.
    * ``bedrock-runtime`` (the codex runtime aliases) DOES get an AWS-side
      ``ModelInvocationLog`` record per call, **including streamed ones**. So on that
      track the two copies overlap and this record is a second source rather than the
      only one.

      ⚠️ An earlier note here claimed the AWS-side record omitted bodies for streamed
         calls. That was **wrong** and is corrected: the live test
         (``tests/integration/test_invocation_logging_live.py``) records 1 log line for
         runtime/responses streamed, runtime/responses non-streamed, and runtime/chat
         streamed alike — and 0 for both Mantle cases, which is the negative control
         that makes the Mantle claim above trustworthy. The measured table lives in
         ``deployment/terraform/modules/bedrock-invocation-logging/main.tf``.
         Leaving the old claim in place would have asserted an audit gap that does not
         exist, which is the kind of error that gets a compensating control built for
         nothing.

    Where both copies exist, ``request_body`` here is what the router captured and the
    gateway forwards it unmodified on the OpenAI wire, so it should match AWS's
    ``inputBodyJson`` key-for-key. ``bedrock_request_id`` is the join key to the AWS-side
    record.

    ⚠️ **This paragraph used to claim otherwise, and the claim was false.** It said the
    copies differ because ``runtime_openai_adapter._store_disabled_body`` injects
    ``store: false`` on the way upstream. Neither that module nor that function has ever
    existed in this repository, and nothing sets ``store`` anywhere. An auditor reading
    the old text would have concluded that upstream retention was already suppressed.

    ``store`` is therefore left at the provider's default on ``/v1/responses``. Forcing it
    to ``false`` was **not** done here on purpose: it is an untested field injection on the
    highest-volume client's only path, and a provider that rejects unknown fields would turn
    every Codex request into a 400. It also interacts with ``previous_response_id`` chaining,
    which this gateway passes through without inspecting. Deciding it needs one live dev
    probe (does the plane accept and honour ``store``?) plus an operator-visible setting —
    see the PR that corrected this docstring.
    """

    request_id: str
    provider: str
    client: str
    model_alias: str
    status: BodyLogStatus
    is_streaming: bool

    user_id: str | None = None
    team_id: str | None = None
    sso_subject: str | None = None
    bedrock_request_id: str | None = None

    request_body: dict[str, Any]
    response_body: dict[str, Any]
    error: dict[str, Any] | None = None

    requested_at: str
    completed_at: str
    date: str  # YYYY-MM-DD, used for the dt= S3 partition

    schema_version: int = 1

    @classmethod
    def make(
        cls,
        *,
        request_id: str,
        provider: str,
        client: str,
        model_alias: str,
        status: BodyLogStatus,
        is_streaming: bool,
        request_body: dict[str, Any],
        response_body: dict[str, Any],
        user_id: str | None = None,
        team_id: str | None = None,
        sso_subject: str | None = None,
        bedrock_request_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> BodyLogRecord:
        now = datetime.now(tz=UTC)
        return cls(
            request_id=request_id,
            provider=provider,
            client=client,
            model_alias=model_alias,
            status=status,
            is_streaming=is_streaming,
            user_id=user_id,
            team_id=team_id,
            sso_subject=sso_subject,
            bedrock_request_id=bedrock_request_id,
            request_body=request_body,
            response_body=response_body,
            error=error,
            requested_at=now.isoformat(),
            completed_at=now.isoformat(),
            date=now.strftime("%Y-%m-%d"),
        )

    def partition_key(self) -> str:
        return f"provider={self.provider}/client={self.client}/dt={self.date}"

    def to_firehose_bytes(self) -> bytes:
        return (self.model_dump_json() + "\n").encode()
