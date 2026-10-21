# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from botocore.exceptions import ClientError

from app.providers.base import CountTokensResult, ProviderAdapter
from app.schemas.domain import TokenUsage
from app.services.account_credentials import (
    ACCOUNT_UNAVAILABLE_BODY,
    ACCOUNT_UNAVAILABLE_CODE,
    ACCOUNT_UNAVAILABLE_MESSAGE,
    ACCOUNT_UNAVAILABLE_STATUS,
    AccountCredentialsUnavailable,
    redact_account_identifiers,
)

_BEDROCK_POOL_SIZE = int(os.environ.get("BEDROCK_THREAD_POOL_SIZE", "128"))
_bedrock_executor = ThreadPoolExecutor(max_workers=_BEDROCK_POOL_SIZE, thread_name_prefix="bedrock")

logger = structlog.get_logger(__name__)

# boto3 예외 → HTTP 상태 코드 매핑
_BOTO_ERROR_MAP: dict[str, int] = {
    "ValidationException": 400,
    "AccessDeniedException": 403,
    "ResourceNotFoundException": 404,
    "ThrottlingException": 429,
    # 계정의 쿼터(예: 일일 토큰)다. 기본값 502 로 두면 폴백·서킷 브레이커 대상이 되어, 그 계정의
    # 쿼터가 같은 모델 id 를 다른 계정으로 부르는 alias 의 브레이커까지 연다.
    "ServiceQuotaExceededException": 429,
    "ModelTimeoutException": 504,
    "ServiceException": 502,
    "InternalServerException": 502,
}


def _extract_bedrock_usage(response_body: dict) -> TokenUsage:
    """Bedrock invoke/converse 응답에서 TokenUsage 추출 (cache 토큰 포함)."""
    usage = response_body.get("usage", {})
    input_tokens = usage.get("input_tokens", usage.get("inputTokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("outputTokens", 0))
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _request_id_headers(response: dict) -> dict:
    """``{"x-amzn-requestid": <id>}`` from a boto3 response, or ``{}`` if absent.

    Empty dict rather than ``{"x-amzn-requestid": None}`` on purpose: the caller does
    ``headers.get("x-amzn-requestid")`` and stores the result, and a NULL column means
    "no record to join to". A key present with a None value would be indistinguishable
    downstream but would make any future ``in headers`` check lie.
    """
    rid = (response or {}).get("ResponseMetadata", {}).get("RequestId")
    return {"x-amzn-requestid": rid} if rid else {}


class BedrockAdapter(ProviderAdapter):
    """AWS Bedrock Runtime adapter (boto3 기반).

    두 모드:
    - in-account(기본): bedrock_client 고정. resolver/fallback None → 기존과 동일 동작.
    - cross-account(claude-code→374): client_resolver = async () -> boto3 client
      (STS assume 후 대상 계정 bedrock-runtime). assume/build 실패 시 fallback_client
      (in-account 859)로 **투명 폴백** → claude-code 절대 안 죽음(사용자 결정).

    alias 별 대상 계정(0043): 호출에 ``account=AccountTarget`` 이 오면 위 두 모드보다 우선해
    그 계정의 클라이언트(``account_clients.get_account_client``, 리전 = ``account_region``)로
    호출한다. 그 경로에는 **폴백이 없다** — AssumeRole 실패는 500 + 고정 본문
    (account_credentials 모듈 docstring). ``account`` 가 없으면 위 두 모드가 바이트 단위로 그대로다.
    """

    def __init__(
        self,
        bedrock_client,
        client_resolver=None,
        fallback_client=None,
        account_clients=None,
        account_region: str | None = None,
    ) -> None:
        self._client = bedrock_client
        self._client_resolver = client_resolver
        self._fallback_client = fallback_client
        # BedrockAccountClientProvider. None = alias 계정 미배선 → account 가 온 호출은 파드
        # 자격증명으로 대신하지 않고 거부한다.
        self._account_clients = account_clients
        # alias 계정 클라이언트의 리전 = 그 요청이 원래 갔을 리전(파드 리전, 또는 claude-code 형
        # 프로필이면 profile.region). 계정만 바꾸고 리전은 바꾸지 않는다 — 라우터의 모델 id
        # 리전 재작성(_rewrite_model_id_for_region)도 같은 리전을 쓴다.
        self._account_region = account_region
        #: 마지막 프로필 경로 호출이 in-account 로 폴백했는가. 실제 서빙 계정 기록(0045)용이다.
        #: resolver 가 있는 **요청 단위** 어댑터(routers/messages.py)에서만 바뀐다 — 공유되는
        #: 레지스트리 어댑터는 resolver 가 없으므로 이 값을 건드리지 않는다.
        self.served_by_fallback = False

    async def _get_client(self):
        """resolver 있으면 cross-account 클라이언트, 실패 시 in-account fallback."""
        if self._client_resolver is None:
            return self._client
        try:
            client = await self._client_resolver()
            self.served_by_fallback = False
            return client
        except Exception:
            logger.warning("bedrock_xacct_resolve_failed_fallback_inaccount", exc_info=True)
            if self._fallback_client is not None:
                self.served_by_fallback = True
                return self._fallback_client
            raise

    async def _client_for(self, account):
        """``account`` (alias 계정) 이 있으면 그 계정의 클라이언트, 없으면 위 두 모드.

        Raises:
            AccountCredentialsUnavailable: alias 계정을 쓸 수 없다. 파드 자격증명으로 대신
                하지 않는다 — 그것은 다른 계정에 청구되는 성공 응답이다.
        """
        if account is None:
            return await self._get_client()
        if self._account_clients is None:
            raise AccountCredentialsUnavailable(
                account.role_arn,
                "alias requests a target AWS account but no account client provider is wired "
                "into BedrockAdapter; refusing to call with the pod's own credentials",
            )
        region = self._account_region or getattr(
            getattr(self._client, "meta", None), "region_name", None
        )
        if not region:
            raise AccountCredentialsUnavailable(
                account.role_arn, "no region for the account client"
            )
        try:
            return await self._account_clients.get_account_client(account, region)
        except AccountCredentialsUnavailable:
            raise
        except Exception as exc:
            # 클라이언트 빌드 실패도 같은 타입으로 — 아니면 어댑터 밖으로 새어 나가 500 전역
            # 핸들러로 가고, 라우터의 예약 해제 경로를 건너뛴다.
            logger.warning(
                "bedrock_account_client_build_failed",
                role_arn=account.role_arn,
                error=type(exc).__name__,
            )
            raise AccountCredentialsUnavailable(
                account.role_arn, f"account client build failed: {type(exc).__name__}"
            ) from exc

    async def invoke(
        self,
        request_body: bytes,
        model_id: str,
        path_suffix: str = "invoke",
        account=None,
        **kwargs,
    ) -> tuple[int, bytes, dict, TokenUsage]:
        """Non-streaming Bedrock call.

        The returned headers dict carries ``x-amzn-requestid`` — the join key to this
        call's Bedrock model-invocation log record, persisted as
        ``usage_logs.bedrock_request_id``. ``invoke_stream`` has surfaced it since the
        streaming rewrite (see :meth:`invoke_stream`), but this branch dropped it, so
        every NON-streaming Claude call was unauditable: the record exists in AWS and
        nothing in our data pointed at it. Returned in the headers dict (rather than as
        a 5th tuple element) to keep the 4-tuple contract in ``ProviderAdapter`` and to
        match ``BedrockOpenAIAdapter``, which uses the same convention.

        Safe to put here because no route forwards this dict to the client — every caller
        (``routers/messages.py``, ``routers/bedrock.py``, ``routers/openai_compat.py``)
        constructs its own ``JSONResponse``/``StreamingResponse`` headers rather than
        passing this dict through, so the AWS id is not leaked outward.

        ``account`` — alias 의 대상 계정(0043). 해석 실패는 provider 오류가 아니라 게이트웨이
        설정 오류라서 아래 try 블록 **밖**에서 자기 응답(500)으로 답한다 — 502 로 섞이면 폴백
        루프가 다른 후보(다른 계정일 수 있다)로 넘기고 서킷 브레이커가 올라간다.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        if account is not None:
            try:
                client = await self._client_for(account)
            except AccountCredentialsUnavailable:
                return ACCOUNT_UNAVAILABLE_STATUS, ACCOUNT_UNAVAILABLE_BODY, {}, TokenUsage()
        try:
            if account is None:
                client = await self._get_client()
            if path_suffix in ("invoke", ""):
                response = await loop.run_in_executor(
                    _bedrock_executor,
                    lambda: client.invoke_model(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    ),
                )
                body = response["body"].read()
                try:
                    parsed = json.loads(body)
                    usage = _extract_bedrock_usage(parsed)
                except Exception:
                    usage = TokenUsage()
                return 200, body, _request_id_headers(response), usage

            elif path_suffix == "converse":
                parsed_req = json.loads(request_body)
                response = await loop.run_in_executor(
                    _bedrock_executor,
                    lambda: client.converse(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                )
                usage = TokenUsage(
                    input_tokens=response.get("usage", {}).get("inputTokens", 0),
                    output_tokens=response.get("usage", {}).get("outputTokens", 0),
                )
                usage.total_tokens = usage.input_tokens + usage.output_tokens
                body = json.dumps(response).encode()
                return 200, body, _request_id_headers(response), usage

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            # 원문은 로그에만. 클라이언트 본문에서는 ARN·계정 번호를 지운다 — AccessDenied 문장에
            # 서명한 계정의 assumed-role ARN 이 들어 있다
            # (account_credentials.redact_account_identifiers).
            logger.warning(
                "bedrock_client_error", error_code=code, model_id=model_id, error=str(e)
            )
            error_body = json.dumps(
                {"error": {"type": "provider_error", "message": redact_account_identifiers(str(e))}}
            ).encode()
            return status, error_body, {}, TokenUsage()
        except Exception:
            logger.exception("bedrock_invoke_failed", model_id=model_id)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Bedrock call failed"}}',
                {},
                TokenUsage(),
            )

    async def count_tokens(
        self, request_body: bytes, model_id: str, account=None
    ) -> CountTokensResult:
        """Bedrock CountTokens API — returns (status, input_tokens). No cost, no inference.

        ``account`` 를 받는 이유는 :meth:`invoke` 와 같다: CountTokens 는 모델 액세스를 가진
        계정에서만 허가되므로, 다른 계정으로 서빙되는 alias 를 이 계정에서 세면 추론은 되는데
        토큰 카운트만 403 이 된다(claude-code 의 invoke/count_tokens 계정 스플릿과 같은 부류).

        실패하면 상류 오류 코드·메시지를 결과에 싣는다(계정 번호·ARN 은 지운다). 라우터가
        "이 모델은 셀 수 없다"(``ValidationException``)와 권한·쓰로틀 실패를 구별해야 한다.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        if account is not None:
            try:
                client = await self._client_for(account)
            except AccountCredentialsUnavailable:
                return CountTokensResult(
                    ACCOUNT_UNAVAILABLE_STATUS, 0, ACCOUNT_UNAVAILABLE_CODE,
                    ACCOUNT_UNAVAILABLE_MESSAGE,
                )
        try:
            if account is None:
                client = await self._get_client()
            response = await loop.run_in_executor(
                None,
                lambda: client.count_tokens(
                    modelId=model_id,
                    input={"invokeModel": {"body": request_body}},
                ),
            )
            return CountTokensResult(200, int(response.get("inputTokens", 0)))
        except ClientError as e:
            code = e.response["Error"]["Code"]
            message = redact_account_identifiers(e.response["Error"].get("Message", ""))
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning(
                "bedrock_count_tokens_error", error_code=code, model_id=model_id,
                error_message=message,
            )
            return CountTokensResult(status, 0, code, message)
        except Exception:
            logger.exception("bedrock_count_tokens_failed", model_id=model_id)
            return CountTokensResult(502, 0, None, "Bedrock CountTokens call failed")

    async def invoke_stream(
        self,
        request_body: bytes,
        model_id: str,
        path_suffix: str = "invoke-with-response-stream",
        account=None,
        **kwargs,
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        import asyncio

        loop = asyncio.get_event_loop()
        if account is not None:
            try:
                client = await self._client_for(account)
            except AccountCredentialsUnavailable:

                async def _account_err() -> AsyncIterator[bytes]:
                    yield ACCOUNT_UNAVAILABLE_BODY

                return ACCOUNT_UNAVAILABLE_STATUS, _account_err(), {}, None
        try:
            if account is None:
                client = await self._get_client()
            if path_suffix == "invoke-with-response-stream":
                response = await loop.run_in_executor(
                    _bedrock_executor,
                    lambda: client.invoke_model_with_response_stream(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    ),
                )
                aws_request_id: str | None = response.get("ResponseMetadata", {}).get("RequestId")
                stream = response.get("body")
                return (
                    200,
                    self._bedrock_stream_gen(stream),
                    {"Content-Type": "application/vnd.amazon.eventstream"},
                    aws_request_id,
                )

            elif path_suffix == "converse-stream":
                parsed_req = json.loads(request_body)
                response = await loop.run_in_executor(
                    _bedrock_executor,
                    lambda: client.converse_stream(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                )
                aws_request_id = response.get("ResponseMetadata", {}).get("RequestId")
                stream = response.get("stream")
                return (
                    200,
                    self._converse_stream_gen(stream),
                    {"Content-Type": "application/vnd.amazon.eventstream"},
                    aws_request_id,
                )

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning(
                "bedrock_stream_client_error", error_code=code, model_id=model_id, error=str(e)
            )
            error_msg = redact_account_identifiers(str(e))  # invoke 의 같은 주석

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return status, error_gen(), {}, None
        except Exception as exc:
            error_msg = redact_account_identifiers(str(exc))
            logger.exception("bedrock_stream_failed", model_id=model_id)

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return 502, error_gen(), {}, None

    async def _bedrock_stream_gen(self, stream) -> AsyncIterator[bytes]:
        """Adapt botocore's blocking EventStream iterator to an async generator.

        Each `next()` call can block waiting for the next event to arrive over
        the wire, so we execute it in the default thread pool via
        `run_in_executor` to keep the event loop responsive.

        Errors propagate to the caller so the streaming helper can surface
        them as SSE `event: error` to the client rather than silently cutting
        the connection.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        it = iter(stream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            event = await loop.run_in_executor(_bedrock_executor, _next)
            if event is sentinel:
                return
            chunk = event.get("chunk", {})
            if "bytes" in chunk:
                yield chunk["bytes"]

    async def _converse_stream_gen(self, stream) -> AsyncIterator[bytes]:
        import asyncio

        loop = asyncio.get_event_loop()
        it = iter(stream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            event = await loop.run_in_executor(_bedrock_executor, _next)
            if event is sentinel:
                return
            yield json.dumps(event).encode()
