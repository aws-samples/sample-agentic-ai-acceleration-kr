# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.lua_loader import LuaScriptLoader


def test_load_all_scripts():
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    LuaScriptLoader.load_all(script_dir)
    assert LuaScriptLoader.is_loaded("rate_limit_check")
    assert LuaScriptLoader.is_loaded("rate_limit_tpm_check")
    assert LuaScriptLoader.is_loaded("cost_rate_limit_scope")
    assert LuaScriptLoader.is_loaded("budget_check")
    assert LuaScriptLoader.is_loaded("budget_deduct")


def test_get_script_not_loaded():
    with pytest.raises(KeyError):
        LuaScriptLoader.get("nonexistent_script")


def test_scripts_contain_keys_argv():
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    LuaScriptLoader.load_all(script_dir)
    # budget_check.lua uses only KEYS (no ARGV); others use both
    for name in ["rate_limit_check", "rate_limit_tpm_check", "budget_deduct"]:
        script = LuaScriptLoader.get(name)
        assert "KEYS" in script
        assert "ARGV" in script


_SCRIPT_DIR = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]


def test_loaded_scripts_drop_comment_and_blank_lines_and_keep_every_code_line():
    """EVAL 마다 본문이 가므로 로드할 때 전체 줄 주석과 빈 줄을 뺀다. 코드 줄(줄 끝 주석 포함)은
    하나도 빠지거나 바뀌지 않고 순서 그대로다 — 실 Redis 테스트들이 이 본문으로 돈다."""
    LuaScriptLoader.load_all(_SCRIPT_DIR)
    files = sorted(_SCRIPT_DIR.glob("*.lua"))
    assert files
    raw_total = sent_total = 0
    for lua_file in files:
        raw = lua_file.read_text(encoding="utf-8")
        sent = LuaScriptLoader.get(lua_file.stem)
        assert sent.splitlines() == _code_lines(raw), lua_file.name
        assert not any(ln.lstrip().startswith("--") or not ln.strip() for ln in sent.splitlines())
        raw_total += len(raw.encode())
        sent_total += len(sent.encode())
    assert sent_total < raw_total * 0.6, (raw_total, sent_total)


def test_a_script_with_a_long_bracket_is_sent_unchanged():
    """``[[ … ]]`` 안의 ``--`` 는 주석이 아니다 — 줄 단위로 지우면 문자열이 바뀐다.

    그대로 보낸다."""
    text = "local s = [[\n-- part of the string\n]]\nreturn s\n"
    assert LuaScriptLoader.strip_comments(text) == text


def test_the_circuit_breaker_sends_its_script_without_comments():
    from app.services.circuit_breaker import CircuitBreakerService

    raw = (_SCRIPT_DIR / "circuit_breaker.lua").read_text(encoding="utf-8")
    assert CircuitBreakerService()._lua.splitlines() == _code_lines(raw)


def _compiles(text: str) -> bool:
    """Redis 와 같은 Lua 5.1 로 컴파일만 해 본다(실행하지 않는다 — KEYS·redis 는 런타임 전역)."""
    from lupa import lua51

    try:
        lua51.LuaRuntime().compile(text)
    except lua51.LuaSyntaxError:
        return False
    return True


@pytest.mark.parametrize(
    "text,naive_breaks",
    [
        pytest.param(
            "local s = [=[\n-- part of the string\n]=]\nreturn s\n", False, id="leveled-long-string"
        ),
        pytest.param("--[==[\nnot code\n]==]\nreturn 1\n", True, id="leveled-block-comment"),
        pytest.param(
            'local s = "first line\\\n-- the rest of the string"\nreturn s\n',
            True,
            id="string-continued-with-a-backslash",
        ),
    ],
)
def test_a_construct_that_line_stripping_would_break_is_sent_unchanged(text, naive_breaks):
    """줄 단위로 지우면 뜻이 바뀌거나(문자열 안의 ``--`` 줄이 사라진다) 컴파일되지 않는 구성은
    그대로 보낸다. 단계가 있는 긴 괄호(``[=[``, ``--[==[``)와 백슬래시로 이어지는 문자열 — 예전
    가드는 ``[[``/``]]`` 만 봤다."""
    naive = "".join(ln + "\n" for ln in _code_lines(text))
    assert naive != text  # 지우면 실제로 달라진다 — 이 테스트가 공허하지 않다
    assert _compiles(naive) is not naive_breaks
    assert LuaScriptLoader.strip_comments(text) == text
    assert _compiles(text)


def test_every_current_script_is_stripped_and_still_compiles():
    """지금 스크립트는 모두 가드에 걸리지 않고(주석이 실제로 빠진다) 지운 뒤에도 Lua 5.1 로
    컴파일된다."""
    for lua_file in sorted(_SCRIPT_DIR.glob("*.lua")):
        raw = lua_file.read_text(encoding="utf-8")
        sent = LuaScriptLoader.strip_comments(raw)
        assert sent != raw, f"{lua_file.name} 의 주석이 빠지지 않았다(가드에 걸렸다)"
        assert _compiles(sent), f"{lua_file.name} 가 지운 뒤 컴파일되지 않는다"
