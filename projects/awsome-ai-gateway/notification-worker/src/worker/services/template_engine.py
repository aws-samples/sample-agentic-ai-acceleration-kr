# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

logger = structlog.get_logger(__name__)

# KST = UTC+9
_KST = timezone(timedelta(hours=9))
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_NEWLINE_RE = re.compile(r"[\r\n]")


class TemplateEngine:
    """Jinja2 기반 이메일 템플릿 렌더링 (SEP-01).

    - autoescape: .html 파일만 HTML 이스케이프 (XSS 방지)
    - StrictUndefined: 정의되지 않은 템플릿 변수 접근 시 즉시 오류 (silent failure 방지)
    - 템플릿 파일: {event_type}.html, {event_type}.subject.txt
    - 템플릿 미존재 시 default.html / default.subject.txt 폴백
    """

    def __init__(self, template_dir: Path | None = None) -> None:
        self._dir = template_dir or _TEMPLATES_DIR
        self._env = Environment(
            loader=FileSystemLoader(str(self._dir)),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
        )

    def render(
        self,
        event_type: str,
        context: dict,
    ) -> tuple[str, str]:
        """이벤트 유형에 맞는 템플릿을 렌더링하여 (subject, html_body) 반환."""
        subject = self._render_subject(event_type, context)
        html_body = self._render_body(event_type, context)
        return subject, html_body

    def _render_subject(self, event_type: str, context: dict) -> str:
        template_name = f"{event_type}.subject.txt"
        try:
            tmpl = self._env.get_template(template_name)
        except TemplateNotFound:
            logger.debug("subject_template_fallback", event_type=event_type)
            tmpl = self._env.get_template("default.subject.txt")

        raw = tmpl.render(**context).strip()
        # 이메일 헤더 인젝션 방지: 줄바꿈/제어문자 제거 (BR-SEC-02)
        return _NEWLINE_RE.sub(" ", raw)

    def _render_body(self, event_type: str, context: dict) -> str:
        template_name = f"{event_type}.html"
        try:
            tmpl = self._env.get_template(template_name)
        except TemplateNotFound:
            logger.debug("body_template_fallback", event_type=event_type)
            tmpl = self._env.get_template("default.html")

        return tmpl.render(**context)

    @staticmethod
    def build_context(event: object, recipient_name: str, recipient_email: str) -> dict:
        """핸들러가 템플릿 렌더링에 필요한 공통 컨텍스트를 구성한다 (BR-EMAIL-03)."""
        now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
        return {
            "event": event,
            "payload": getattr(event, "payload", {}),
            "recipient_name": recipient_name,
            "recipient_email": recipient_email,
            "gateway_name": "LLM Gateway",
            "timestamp_kr": now_kst,
        }


# Module singleton — initialised once in main.py
_engine: TemplateEngine | None = None


def init_template_engine(template_dir: Path | None = None) -> None:
    global _engine
    _engine = TemplateEngine(template_dir)


def get_template_engine() -> TemplateEngine:
    assert _engine is not None, "TemplateEngine not initialised. Call init_template_engine() first."
    return _engine
