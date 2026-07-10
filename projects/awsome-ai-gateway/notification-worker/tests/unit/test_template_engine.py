# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""TemplateEngine 단위 테스트."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from worker.services.template_engine import TemplateEngine


def _make_template_dir(templates: dict[str, str]) -> Path:
    """임시 디렉토리에 템플릿 파일을 생성하고 경로를 반환한다."""
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    for name, content in templates.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


def test_render_existing_template() -> None:
    tdir = _make_template_dir({
        "budget_threshold.subject.txt": "Budget Alert: {{ payload.threshold_pct }}%",
        "budget_threshold.html": "<p>Hello {{ recipient_name }}</p>",
        "default.subject.txt": "Notification",
        "default.html": "<p>Default</p>",
    })
    engine = TemplateEngine(tdir)

    context = {
        "event": None,
        "payload": {"threshold_pct": 80},
        "recipient_name": "Alice",
        "recipient_email": "alice@example.com",
        "gateway_name": "LLM Gateway",
        "timestamp_kr": "2026-04-10 12:00:00 KST",
    }
    subject, html = engine.render("budget_threshold", context)

    assert "80%" in subject
    assert "Alice" in html


def test_render_falls_back_to_default_template() -> None:
    tdir = _make_template_dir({
        "default.subject.txt": "Default Subject",
        "default.html": "<p>Default Body</p>",
    })
    engine = TemplateEngine(tdir)

    context = {
        "event": None,
        "payload": {},
        "recipient_name": "Bob",
        "recipient_email": "bob@example.com",
        "gateway_name": "LLM Gateway",
        "timestamp_kr": "2026-04-10 12:00:00 KST",
    }
    subject, html = engine.render("nonexistent_event", context)

    assert subject == "Default Subject"
    assert "Default Body" in html


def test_render_subject_strips_newlines() -> None:
    """이메일 헤더 인젝션 방지 — 줄바꿈 제거."""
    tdir = _make_template_dir({
        "bad_event.subject.txt": "Subject\nInjected-Header: value",
        "bad_event.html": "<p>body</p>",
        "default.subject.txt": "Default",
        "default.html": "<p>D</p>",
    })
    engine = TemplateEngine(tdir)

    context = {
        "event": None, "payload": {}, "recipient_name": "X",
        "recipient_email": "x@x.com", "gateway_name": "GW", "timestamp_kr": "now",
    }
    subject, _ = engine.render("bad_event", context)

    assert "\n" not in subject
    assert "\r" not in subject


def test_render_html_autoescape() -> None:
    """HTML body는 autoescape 적용 — XSS 방지."""
    tdir = _make_template_dir({
        "xss_event.subject.txt": "Subject",
        "xss_event.html": "<p>{{ recipient_name }}</p>",
        "default.subject.txt": "Default",
        "default.html": "<p>D</p>",
    })
    engine = TemplateEngine(tdir)

    context = {
        "event": None, "payload": {},
        "recipient_name": "<script>alert(1)</script>",
        "recipient_email": "x@x.com", "gateway_name": "GW", "timestamp_kr": "now",
    }
    _, html = engine.render("xss_event", context)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_context_structure() -> None:
    """build_context는 필수 키를 모두 포함해야 한다."""
    from worker.schemas.events import EventType, NotificationEvent, ServiceSource

    event = NotificationEvent(
        event_id="e1",
        type=EventType.KEY_EXPIRING,
        timestamp="2026-04-10T12:00:00+00:00",  # type: ignore[arg-type]
        source=ServiceSource.ADMIN_API,
        payload={"days_until_expiry": 7},
    )
    ctx = TemplateEngine.build_context(event, "Alice", "alice@example.com")

    assert ctx["event"] is event
    assert ctx["payload"] == {"days_until_expiry": 7}
    assert ctx["recipient_name"] == "Alice"
    assert ctx["recipient_email"] == "alice@example.com"
    assert "timestamp_kr" in ctx
    assert "KST" in ctx["timestamp_kr"]
