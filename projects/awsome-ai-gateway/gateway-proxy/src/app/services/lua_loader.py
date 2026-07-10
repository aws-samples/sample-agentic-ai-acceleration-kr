# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class LuaScriptLoader:
    """Lua 스크립트 파일을 로드하고 EVAL 실행을 지원하는 유틸리티."""

    _scripts: dict[str, str] = {}

    @classmethod
    def load_all(cls, script_dir: Path) -> None:
        if not script_dir.exists():
            logger.warning("lua_script_dir_not_found", path=str(script_dir))
            return
        for lua_file in script_dir.glob("*.lua"):
            cls._scripts[lua_file.stem] = lua_file.read_text(encoding="utf-8")
            logger.debug("lua_script_loaded", name=lua_file.stem)

    @classmethod
    def get(cls, name: str) -> str:
        if name not in cls._scripts:
            raise KeyError(f"Lua script '{name}' not loaded")
        return cls._scripts[name]

    @classmethod
    def is_loaded(cls, name: str) -> bool:
        return name in cls._scripts
