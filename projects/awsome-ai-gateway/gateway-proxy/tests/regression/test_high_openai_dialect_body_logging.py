# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OpenAI 방언(`/v1/chat/completions`, `/v1/responses`)과 웹서치 경로의 본문 로깅.

왜 이 파일이 따로 있나
----------------------
``test_high_body_logging_gate.py`` 는 게이트(꺼짐이 진짜 꺼짐인지)와 비용을 못 박고,
그 배선은 ``/v1/messages`` 하나만 확인한다. 본문 로깅이 **어느 경로에 배선되지 않았는지**
는 그 파일로 드러나지 않는다 — 배선을 빼먹은 경로는 예외도 경고도 없이 그냥 레코드를
남기지 않는다. 관리자가 토글을 켜고 화면에서 "ON" 을 보는데 Codex 요청만 통째로 빠져
있고, 그 사실은 조사를 시작한 뒤에야 드러난다.

여기서 세 축을 센다:

  1. ``/v1/chat/completions``(=`_handle_openai`) 스트리밍·비스트리밍
  2. ``/v1/responses``(=`_handle_responses`) 스트리밍·비스트리밍
  3. **웹서치 루프의 여섯 개 리턴 경로 전부**

3번이 이 파일의 핵심이다. ``run_web_search_loop`` 는 라우터의 ``if is_stream:`` 블록보다
**먼저** 리턴하므로, 라우터에만 로깅을 배선하면 웹서치를 켠 프로파일은 로깅 코드를 아예
지나지 않는다. 그리고 그 조합이 최악이다 — 웹서치를 쓰는 것은 Codex(Mantle)이고 Mantle 은
AWS invocation log 에도 남지 않으므로, 본문의 정본이 **어디에도** 없게 된다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.body_log_records import wrap_stream_for_body_log

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


def _tree(rel: str) -> tuple[ast.Module, str]:
    src = (_SRC / rel).read_text(encoding="utf-8")
    # 대조군 — 경로 오타를 "일치" 로 오판하지 않는다.
    assert len(src) > 2000, f"{rel} 가 너무 짧다 — 경로가 틀렸을 것이다"
    return ast.parse(src), src


def _func(tree: ast.Module, name: str) -> ast.AST:
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )
    assert fn is not None, f"{name} 를 찾지 못했다 — 이 검사의 전제가 깨졌다"
    return fn


def _calls(node: ast.AST, name: str) -> list[int]:
    """`name(...)` 형태 호출의 줄번호. 속성 호출(`x.name()`)은 세지 않는다."""
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 1. 두 OpenAI 방언 핸들러가 스트리밍·비스트리밍 양쪽을 배선했다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("handler", "stream_fn"),
    [
        ("_handle_openai", "openai_sse_stream"),
        ("_handle_responses", "responses_sse_stream"),
    ],
)
def test_handler_gates_before_starting_the_stream(handler: str, stream_fn: str):
    """게이트가 스트림 생성 **전에** 있어야 한다(사전 게이팅).

    끝나고 물어보면 로깅이 꺼져 있어도 모든 응답 본문을 버릴 목적으로 버퍼링한다.
    """
    tree, _ = _tree("routers/openai_compat.py")
    fn = _func(tree, handler)
    gates = _calls(fn, "resolve_body_logger")
    streams = _calls(fn, stream_fn)
    assert gates, f"{handler} 가 게이트를 부르지 않는다 — 이 경로의 본문이 미기록된다"
    assert streams, f"{stream_fn} 호출을 찾지 못했다 — 전제가 깨졌다"
    assert min(gates) < max(streams), (
        f"{handler}: 게이트(L{gates})가 스트림 생성(L{streams}) 뒤에 있다 — 사전 게이팅이 아니다"
    )


@pytest.mark.parametrize(
    ("handler", "stream_fn"),
    [
        ("_handle_openai", "openai_sse_stream"),
        ("_handle_responses", "responses_sse_stream"),
    ],
)
def test_stream_helper_receives_on_complete(handler: str, stream_fn: str):
    """SSE 헬퍼에 ``on_complete`` 를 실제로 넘겨야 한다.

    게이트만 부르고 넘기지 않으면 Redis 왕복만 하고 아무것도 기록하지 않는다 —
    가장 찾기 어려운 형태의 미기록이다(로그도 오류도 없고 토글은 ON 이다).
    """
    tree, _ = _tree("routers/openai_compat.py")
    fn = _func(tree, handler)
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == stream_fn
        ):
            kw = {k.arg for k in node.keywords}
            assert "on_complete" in kw, (
                f"{handler} L{node.lineno}: {stream_fn} 에 on_complete 를 넘기지 않았다"
            )
            return
    pytest.fail(f"{stream_fn} 호출을 찾지 못했다")


@pytest.mark.parametrize("handler", ["_handle_openai", "_handle_responses"])
def test_nonstreaming_branch_enqueues_a_record(handler: str):
    """비스트리밍 분기도 배선돼야 한다. 한쪽만 하면 그 경로가 조용히 빠진다."""
    tree, _ = _tree("routers/openai_compat.py")
    fn = _func(tree, handler)
    assert _calls(fn, "build_body_record_for_nonstream"), (
        f"{handler}: 비스트리밍 본문 레코드를 만들지 않는다"
    )


@pytest.mark.parametrize("handler", ["_handle_openai", "_handle_responses"])
def test_nonstreaming_logging_is_not_gated_on_token_count(handler: str):
    """usage 조건 안에서 로깅하면 **실패한 요청**의 본문만 정확히 사라진다.

    비용 기록은 ``usage.total_tokens > 0`` 조건 안에 있는 것이 맞다(0 이면 기록할 비용이
    없다). 본문 로깅에 같은 조건을 물려 쓰면, 조사에 가장 필요한 레코드 — 400/429/500 로
    끝나 토큰이 0 인 요청 — 가 통째로 빠진다.
    """
    tree, _ = _tree("routers/openai_compat.py")
    fn = _func(tree, handler)
    # total_tokens 를 검사하는 If 노드들을 찾아, 그 **안**에 본문 레코드 생성이 없어야 한다.
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_attrs = {
            n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)
        }
        if "total_tokens" not in test_attrs:
            continue
        inside = _calls(node, "build_body_record_for_nonstream")
        assert not inside, (
            f"{handler} L{node.lineno}: 본문 로깅이 usage 조건 안에 있다 — "
            f"토큰 0 인 실패 요청(L{inside})의 본문이 사라진다"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 웹서치 루프 — 여섯 개 리턴 경로 전부
# ─────────────────────────────────────────────────────────────────────────────


def test_web_search_loop_accepts_both_body_log_hooks():
    tree, _ = _tree("services/web_search_loop.py")
    fn = _func(tree, "run_web_search_loop")
    kwonly = {a.arg for a in fn.args.kwonlyargs}
    assert "on_stream_complete" in kwonly
    assert "on_nonstream_complete" in kwonly


def test_every_web_search_return_path_is_wired():
    """``run_web_search_loop`` 의 리턴 경로 개수와 로깅 배선 개수가 맞아야 한다.

    리턴이 여섯이다: 클라이언트가 자기 web_search 툴을 가진 경우(스트림/비스트림),
    MCP 초기화 실패 폴백(스트림/비스트림), 그리고 본 루프(스트림/비스트림).
    하나를 빼먹으면 그 조건에서만 본문이 사라지고, 그 조건은 재현하기 어렵다.
    """
    tree, _ = _tree("services/web_search_loop.py")
    fn = _func(tree, "run_web_search_loop")

    returns = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Return) and n.value is not None
    ]
    # 중첩 헬퍼(_log_stream/_log_nonstream)의 return 은 제외한다.
    nested = {
        id(n)
        for h in ast.walk(fn)
        if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)) and h is not fn
        for n in ast.walk(h)
    }
    returns = [n for n in returns if id(n) not in nested]
    assert len(returns) == 6, (
        f"리턴 경로가 {len(returns)}개다 — 6 을 기대했다. 경로가 늘었다면 그 경로에도 "
        "본문 로깅을 배선하고 이 숫자를 함께 올려야 한다"
    )

    stream_wraps = _calls(fn, "_log_stream")
    nonstream_logs = _calls(fn, "_log_nonstream")
    assert len(stream_wraps) == 3, (
        f"_log_stream 배선이 {len(stream_wraps)}곳 — 스트리밍 리턴 3곳 전부여야 한다"
    )
    assert len(nonstream_logs) == 3, (
        f"_log_nonstream 배선이 {len(nonstream_logs)}곳 — 비스트리밍 리턴 3곳 전부여야 한다"
    )


def test_every_streaming_return_is_wrapped_not_raw():
    """스트리밍 리턴이 감싸지 않은 제너레이터를 그대로 넘기면 안 된다.

    ``StreamingResponse(gen, ...)`` 로 남아 있는 경로가 하나라도 있으면 그 경로만
    미기록이다. ``_log_stream`` 은 훅이 없을 때 인자를 그대로 돌려주므로, 감싸는 것
    자체는 로깅이 꺼져 있을 때 비용이 없다.
    """
    tree, _ = _tree("services/web_search_loop.py")
    fn = _func(tree, "run_web_search_loop")
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "StreamingResponse"
        ):
            continue
        assert node.args, f"L{node.lineno}: StreamingResponse 에 위치 인자가 없다"
        first = node.args[0]
        assert isinstance(first, ast.Call) and getattr(first.func, "id", None) == "_log_stream", (
            f"L{node.lineno}: 제너레이터가 _log_stream 으로 감싸이지 않았다 — "
            "이 경로의 본문만 조용히 미기록된다"
        )


@pytest.mark.parametrize(
    ("router", "func"),
    [("routers/openai_compat.py", "_handle_responses"), ("routers/messages.py", "messages")],
)
def test_routers_pass_the_hooks_to_the_loop(router: str, func: str):
    """양쪽 라우터가 루프에 훅을 넘겨야 한다. 루프가 받을 준비만 돼 있으면 무의미하다."""
    tree, _ = _tree(router)
    fn = _func(tree, func)
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_web_search_loop"
        ):
            kw = {k.arg for k in node.keywords}
            assert "on_stream_complete" in kw, f"{func}: on_stream_complete 미전달"
            assert "on_nonstream_complete" in kw, f"{func}: on_nonstream_complete 미전달"
            return
    pytest.fail(f"{func} 에서 run_web_search_loop 호출을 찾지 못했다")


@pytest.mark.parametrize(
    ("router", "func"),
    [("routers/openai_compat.py", "_handle_responses"), ("routers/messages.py", "messages")],
)
def test_router_hooks_are_pre_gated(router: str, func: str):
    """훅은 게이트 결과에 조건부여야 한다 — 켜져 있지 않으면 ``None``.

    무조건 넘기면 루프가 로깅이 꺼진 상태에서도 SSE 전문을 메모리에 누적한다.
    """
    tree, _ = _tree(router)
    fn = _func(tree, func)
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_web_search_loop"
        ):
            continue
        for kw in node.keywords:
            if kw.arg in ("on_stream_complete", "on_nonstream_complete"):
                assert isinstance(kw.value, ast.IfExp), (
                    f"{func}: {kw.arg} 를 조건 없이 넘긴다 — 꺼져 있어도 누적한다"
                )
        assert _calls(fn, "resolve_body_logger"), f"{func}: 게이트를 부르지 않는다"
        return
    pytest.fail(f"{func} 에서 run_web_search_loop 호출을 찾지 못했다")


# ─────────────────────────────────────────────────────────────────────────────
# 3. 래퍼의 실제 동작
# ─────────────────────────────────────────────────────────────────────────────

_ANTHROPIC_END = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
_RESPONSES_END = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'


async def _agen(chunks):
    for c in chunks:
        yield c


async def _drain(gen):
    return [c async for c in gen]


async def test_wrapper_passes_bytes_through_unchanged():
    """로깅이 켜졌다고 사용자가 받는 바이트가 달라지면 안 된다."""
    chunks = [b"a", b"bb", _ANTHROPIC_END]
    seen: list[tuple[str, str]] = []

    async def hook(text: str, status: str) -> None:
        seen.append((text, status))

    out = await _drain(
        wrap_stream_for_body_log(_agen(chunks), dialect="anthropic", on_complete=hook)
    )
    assert out == chunks
    assert len(seen) == 1
    assert seen[0][0] == "abb" + _ANTHROPIC_END.decode()


@pytest.mark.parametrize(
    ("dialect", "terminal"),
    [("anthropic", _ANTHROPIC_END), ("responses", _RESPONSES_END)],
)
async def test_terminal_marker_means_success(dialect: str, terminal: bytes):
    got: list[str] = []

    async def hook(text: str, status: str) -> None:
        got.append(status)

    await _drain(
        wrap_stream_for_body_log(_agen([b"x", terminal]), dialect=dialect, on_complete=hook)
    )
    assert got == ["success"]


@pytest.mark.parametrize("dialect", ["anthropic", "responses"])
async def test_missing_terminal_marker_means_partial(dialect: str):
    """⚠️ 이 단정이 이 래퍼의 존재 이유다.

    SSE 헬퍼들은 클라이언트 연결 끊김을 **return** 으로 처리한다 — 제너레이터는 정상
    종료한다. 그래서 "정상 종료했으니 success" 로 채점하면 **잘린 스트림이 완전한 것으로
    기록된다.** 종료 방식이 아니라 누적된 내용으로 판단해야 한다.
    """
    got: list[str] = []

    async def hook(text: str, status: str) -> None:
        got.append(status)

    await _drain(
        wrap_stream_for_body_log(
            _agen([b"partial output, no terminal frame"]),
            dialect=dialect,
            on_complete=hook,
        )
    )
    assert got == ["partial"]


async def test_cross_dialect_marker_does_not_count():
    """방언을 잘못 넘기면 success 가 되지 않는다(표지 표가 실제로 쓰인다는 대조군)."""
    got: list[str] = []

    async def hook(text: str, status: str) -> None:
        got.append(status)

    await _drain(
        wrap_stream_for_body_log(
            _agen([_RESPONSES_END], ), dialect="anthropic", on_complete=hook
        )
    )
    assert got == ["partial"]


async def test_unknown_dialect_is_partial_not_a_crash():
    """모르는 방언은 partial 로 떨어진다 — 로깅이 요청을 깨뜨리는 것보다 낫다."""
    got: list[str] = []

    async def hook(text: str, status: str) -> None:
        got.append(status)

    out = await _drain(
        wrap_stream_for_body_log(_agen([b"z"]), dialect="klingon", on_complete=hook)
    )
    assert out == [b"z"]
    assert got == ["partial"]


async def test_hook_exception_does_not_break_the_stream():
    """감사 sink 실패로 사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다."""

    async def boom(text: str, status: str) -> None:
        raise RuntimeError("sink down")

    out = await _drain(
        wrap_stream_for_body_log(
            _agen([b"a", _ANTHROPIC_END]), dialect="anthropic", on_complete=boom
        )
    )
    assert out == [b"a", _ANTHROPIC_END]


async def test_upstream_failure_still_records_what_arrived():
    """상류가 중간에 터져도 **그때까지 받은 본문**이 남아야 한다.

    조사에 필요한 것은 성공한 요청보다 실패한 요청의 본문이다. 예외 경로에서 발화를
    빼면 정확히 그 레코드만 사라진다.
    """
    got: list[tuple[str, str]] = []

    async def hook(text: str, status: str) -> None:
        got.append((text, status))

    async def failing():
        yield b"first"
        raise RuntimeError("upstream died")

    with pytest.raises(RuntimeError):
        await _drain(
            wrap_stream_for_body_log(failing(), dialect="anthropic", on_complete=hook)
        )
    assert got == [("first", "partial")]


async def test_hook_fires_exactly_once():
    """두 번 발화하면 같은 요청의 본문 레코드가 중복 적재된다."""
    calls = 0

    async def hook(text: str, status: str) -> None:
        nonlocal calls
        calls += 1

    await _drain(
        wrap_stream_for_body_log(
            _agen([b"a", b"b", _ANTHROPIC_END]), dialect="anthropic", on_complete=hook
        )
    )
    assert calls == 1


async def test_non_utf8_bytes_do_not_raise():
    """프레임이 유효한 UTF-8 이 아니어도 요청을 깨뜨리지 않는다."""
    got: list[str] = []

    async def hook(text: str, status: str) -> None:
        got.append(text)

    out = await _drain(
        wrap_stream_for_body_log(
            _agen([b"\xff\xfe", _ANTHROPIC_END]), dialect="anthropic", on_complete=hook
        )
    )
    assert out[0] == b"\xff\xfe"
    assert len(got) == 1
