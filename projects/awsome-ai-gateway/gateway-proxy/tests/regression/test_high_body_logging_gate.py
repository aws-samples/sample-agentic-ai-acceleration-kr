# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""요청/응답 **본문** 로깅의 게이트와 비용.

이 기능이 무엇을 하나
---------------------
켜지면 게이트웨이가 요청 JSON 전문과 응답(스트리밍이면 SSE 프레임 전문)을 Firehose →
S3 로 보낸다. 즉 **사용자가 프롬프트에 넣은 것이 그대로 durable 저장소로 나간다.**

⚠️ 현재 구현은 **마스킹하지 않는다.** 같은 코드베이스의 trace 경로는
   ``trace_mask_pii`` 기본 True 로 마스킹하는데 본문 로거에는 적용되지 않는다.
   알고 있는 격차이고 향후 개선 대상이다. 그래서 이 파일은 "꺼져 있음" 이 진짜로
   꺼져 있는지를 집중적으로 못 박는다 — 두 겹의 잠금이 유일한 통제 수단이다:

     1. ``enabled_effective`` — Firehose 스트림이 실제로 설정됐는지(정적).
        미설정이면 로거가 no-op 이라 로컬/compose 에서 자격증명 없이도 안전하다.
     2. ``body_log_flag`` — 관리자 런타임 토글. **기본 OFF.** 배포만으로는 수집이
        시작되지 않고, 켜는 액션은 ``audit.audit_logs`` 에 불변 행을 남긴다.

두 번째로 중요한 것: **꺼져 있을 때 비용이 0** 이어야 한다. ``on_complete`` 훅을
넘기면 제너레이터가 SSE 프레임 전문을 메모리에 누적하므로, 게이팅이 스트림 시작
**전에** 일어나지 않으면 모든 요청이 버릴 목적으로 응답 본문을 버퍼링한다.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.body_log_records import resolve_body_logger
from app.services.streaming import bedrock_anthropic_sse_stream

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 두 겹 게이트
# ─────────────────────────────────────────────────────────────────────────────


class _Flag:
    def __init__(self, value: bool):
        self.value = value
        self.calls = 0

    async def is_enabled(self, redis, session_factory):
        self.calls += 1
        return self.value


def _logger(effective: bool):
    return SimpleNamespace(enabled_effective=effective)


async def test_no_logger_means_off():
    """app.state 에 로거가 없으면(구 배포 / 부팅 실패) 수집하지 않는다."""
    assert await resolve_body_logger(SimpleNamespace(), None, None) is None


async def test_not_effective_means_off_without_touching_redis():
    """Firehose 스트림 미설정 = 정적으로 꺼짐. **Redis 를 묻지도 않는다.**

    빌드 시점에 끈 기능이 요청마다 Redis 왕복을 하면 안 된다.
    """
    flag = _Flag(True)
    state = SimpleNamespace(body_logger=_logger(False), body_log_flag=flag)
    assert await resolve_body_logger(state, MagicMock(), MagicMock()) is None
    assert flag.calls == 0, "정적으로 꺼졌는데 동적 플래그를 조회했다"


async def test_effective_but_flag_off_means_off():
    """인프라는 준비됐지만 관리자가 켜지 않았다 → 수집하지 않는다.

    ⚠️ 이게 배포 직후의 상태다. 이 단정이 깨지면 아무도 켜지 않은 본문 수집이 돈다.
    """
    flag = _Flag(False)
    state = SimpleNamespace(body_logger=_logger(True), body_log_flag=flag)
    assert await resolve_body_logger(state, MagicMock(), MagicMock()) is None
    assert flag.calls == 1


async def test_both_on_means_on():
    """대조군 — 위 단정들이 "항상 None" 이 아님을 보인다."""
    bl = _logger(True)
    state = SimpleNamespace(body_logger=bl, body_log_flag=_Flag(True))
    assert await resolve_body_logger(state, MagicMock(), MagicMock()) is bl


async def test_missing_flag_object_falls_back_to_the_static_answer():
    """플래그 객체가 없으면 "동적 게이트 미설정" → 정적 판정이 선다.

    ⚠️ 이 경우를 켜짐으로 두는 것이 맞는지는 논쟁적이다. 여기서 고정하는 이유는
       동작을 **명시**하는 것이다: 스트림이 설정돼 있고 토글 인프라가 없는 배포는
       정적 설정을 따른다. 토글을 넣은 뒤에는 이 경로가 실무에서 나타나지 않는다.
    """
    bl = _logger(True)
    state = SimpleNamespace(body_logger=bl)
    assert await resolve_body_logger(state, MagicMock(), MagicMock()) is bl


def test_flag_default_is_off():
    """설정 기본값이 OFF 여야 한다 — 배포만으로 수집이 시작되면 안 된다."""
    from app.config import get_settings

    assert get_settings().body_log_flag_default is False


def test_firehose_stream_is_not_configured_by_default():
    """기본 설정에 스트림 이름이 없어야 한다(로컬/compose 안전)."""
    from app.config import get_settings

    assert get_settings().firehose_stream_name is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. 꺼져 있을 때 비용 0 — 프레임을 누적하지 않는다
# ─────────────────────────────────────────────────────────────────────────────


def _frames():
    return [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}).encode(),
        json.dumps(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}
        ).encode(),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 2}}).encode(),
    ]


def _request():
    req = MagicMock()

    async def not_disconnected():
        return False

    req.is_disconnected = not_disconnected
    return req


async def _drain(on_complete=None):
    async def gen():
        for f in _frames():
            yield f

    out = []
    async for chunk in bedrock_anthropic_sse_stream(_request(), gen(), on_complete=on_complete):
        out.append(chunk)
    return out


async def test_hook_fires_exactly_once_on_success():
    calls: list[tuple[str, int]] = []

    async def on_complete(text: str, status: str) -> None:
        calls.append((status, len(text)))

    out = await _drain(on_complete)
    assert len(out) == 3, f"프레임 {len(out)}개 — 3 이어야"
    assert len(calls) == 1, f"훅이 {len(calls)}번 발화 — 정확히 1회여야 한다"
    assert calls[0][0] == "success"
    assert calls[0][1] > 0, "누적 텍스트가 비었다 — 프레임 누적이 배선되지 않았다"


async def test_output_is_identical_with_and_without_the_hook():
    """로깅이 켜졌다고 사용자가 받는 바이트가 달라지면 안 된다."""
    with_hook: list[bytes] = []

    async def on_complete(text: str, status: str) -> None:
        pass

    with_hook = await _drain(on_complete)
    without = await _drain(None)
    assert with_hook == without


async def test_hook_exception_does_not_break_the_stream():
    """감사 실패로 사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다."""

    async def boom(text: str, status: str) -> None:
        raise RuntimeError("sink down")

    out = await _drain(boom)
    assert len(out) == 3, "훅이 터졌는데 스트림도 끊겼다"


def test_generator_does_not_accumulate_when_the_hook_is_absent():
    """AST 로 확인 — 누적이 ``if on_complete is not None`` 가드 안에 있어야 한다.

    가드가 없으면 로깅이 꺼진 상태에서도 모든 응답 본문이 메모리에 쌓인다(동시 스트림
    수 × 응답 크기). 행동 테스트로는 메모리 사용을 관측하기 어려우므로 구조로 못 박는다.
    """
    src = (_SRC / "services" / "streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    appends = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "append"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "accumulated_frames"
    ]
    assert appends, "accumulated_frames.append 를 찾지 못했다 — 누적이 배선되지 않았다"

    # 각 append 가 on_complete 를 검사하는 If 안에 있는지.
    guarded = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "on_complete" not in test_names:
            continue
        for inner in ast.walk(node):
            if inner in appends:
                guarded += 1
    assert guarded == len(appends), (
        f"append {len(appends)}개 중 {guarded}개만 on_complete 가드 안에 있다 — "
        "가드 밖의 것은 로깅이 꺼져 있어도 응답 전문을 메모리에 쌓는다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 라우터 배선 — 사전 게이팅인지
# ─────────────────────────────────────────────────────────────────────────────


def test_router_gates_before_starting_the_stream():
    """``resolve_body_logger`` 를 **스트림 생성 전에** 불러야 한다.

    끝나고 물어보면 모든 요청에 대해 버릴 목적으로 응답을 버퍼링하게 된다.
    """
    src = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    resolve_lines = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "resolve_body_logger"
    ]
    stream_lines = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "bedrock_anthropic_sse_stream"
    ]
    assert resolve_lines, "라우터가 게이트를 호출하지 않는다 — 로거가 죽어 있다"
    assert stream_lines, "스트림 생성 호출을 찾지 못했다 — 이 검사의 전제가 깨졌다"
    assert min(resolve_lines) < max(stream_lines), (
        f"게이트(L{resolve_lines})가 스트림 생성(L{stream_lines}) 뒤에 있다 — "
        "사전 게이팅이 아니다"
    )


def test_router_logs_both_streaming_and_nonstreaming():
    """한쪽만 배선하면 그 경로의 본문이 조용히 누락된다."""
    src = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    assert "build_body_record_for_stream" in src, "스트리밍 경로가 배선되지 않았다"
    assert "build_body_record_for_nonstream" in src, "비스트리밍 경로가 배선되지 않았다"


def test_nonstreaming_logs_errors_too():
    """오류 응답의 본문이야말로 조사에 필요하다 — 2xx 만 남기면 쓸모가 없다."""
    src = (_SRC / "services" / "body_log_records.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "build_body_record_for_nonstream"
        ),
        None,
    )
    assert fn is not None
    code = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
    # 상태코드를 보고 error 를 채우는 분기가 있어야 한다(2xx 를 걸러 버리지 않는다).
    assert "status_code" in code
    assert "'error'" in code or '"error"' in code


def test_effective_model_config_is_logged_not_the_requested_one():
    """폴백이 일어났으면 **실제로 응답한** 모델이 남아야 한다.

    요청된 alias 를 남기면 폴백 조사가 불가능해진다 — 로그는 일어나지 않은 일을 말한다.

    ⚠️ 판정을 위치로 한다(허용목록이 아니다). ``effective_model_config`` 는
       ``run_fallback_loop`` 의 결과에서 나오므로 그 줄 **이전**에는 스코프에 없다.
       그 이전의 유일한 로깅 지점은 웹서치 경로인데, 그 경로는 폴백 루프를 타지 않고
       ``model_config`` 로 직접 호출하므로 거기서는 ``model_config`` 가 곧 effective 다.
       허용목록으로 처리하면 나중에 폴백 **뒤**에 추가된 호출이 ``model_config`` 를 써도
       통과해 버린다 — 그래서 "그 줄 뒤의 모든 호출" 로 못 박는다.
    """
    src = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    assign_line = next(
        (
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "effective_model_config"
                for t in n.targets
            )
        ),
        None,
    )
    assert assign_line is not None, (
        "effective_model_config 할당을 찾지 못했다 — 이 검사의 전제가 깨졌다"
    )

    after = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in ("build_body_record_for_stream", "build_body_record_for_nonstream")
        ):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if node.lineno < assign_line:
            # 웹서치 경로 — effective_model_config 가 아직 없다. model_config 여야 한다.
            assert "model_config" in names, (
                f"L{node.lineno}: 폴백 루프 전(웹서치) 경로인데 model_config 를 쓰지 않는다"
            )
            continue
        after += 1
        assert "effective_model_config" in names, (
            f"L{node.lineno}: 요청된 model_config 를 로깅한다 — 폴백 시 틀린 모델이 남는다"
        )
    assert after >= 2, (
        f"폴백 루프 뒤의 로깅 지점이 {after}곳뿐이다 — 스트리밍/비스트리밍 둘 다여야 하고, "
        "이 하한이 없으면 검사가 공허해진다"
    )
