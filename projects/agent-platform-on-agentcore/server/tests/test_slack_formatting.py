"""Slack text in and Slack messages out (integrations/slack/formatting.py, manifest.py)."""
import json
import os
import random
import re
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.slack import formatting as fmt  # noqa: E402
from integrations.slack import manifest  # noqa: E402
from integrations.slack.api import REQUIRED_BOT_SCOPES  # noqa: E402


def _fences(chunk):
    return sum(1 for line in chunk.split("\n") if fmt._fence_marker(line))


def _words(text):
    return re.sub(r"(?m)^(```|~~~).*\n?", "", text).split()


def test_unescape_restores_what_the_user_typed():
    assert fmt.unescape("a &lt;b&gt; &amp; c") == "a <b> & c"
    # `&amp;` last, so an escaped entity stays an entity.
    assert fmt.unescape("&amp;lt;") == "&lt;"


def test_a_short_answer_is_one_message():
    assert fmt.split_markdown("hello") == ["hello"]
    assert fmt.split_markdown("") == []


def test_a_long_answer_splits_on_paragraphs():
    paragraphs = [("p%d " % i) * 30 for i in range(10)]
    chunks = fmt.split_markdown("\n\n".join(paragraphs), limit=400)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 400
        # Never mid-paragraph: every chunk starts at a paragraph's first word.
        assert re.match(r"p\d ", chunk)


def test_a_split_code_block_is_closed_and_reopened_with_its_language():
    code = "```python\n" + "\n".join(f"x = {i}" for i in range(80)) + "\n```"
    chunks = fmt.split_markdown("intro\n\n" + code + "\n\noutro", limit=200)
    assert len(chunks) > 2
    for chunk in chunks:
        assert len(chunk) <= 200
        assert _fences(chunk) % 2 == 0
    # Each continuation reopens the block the way it was opened.
    assert all(c.startswith("```python") for c in chunks[1:-1])


def test_splitting_never_loses_or_reorders_text():
    rng = random.Random(7)
    for limit in (150, 400, 2000):
        for _ in range(150):
            parts = []
            for _ in range(rng.randint(1, 40)):
                roll = rng.random()
                if roll < 0.2:
                    parts.append("```js\n" + "\n".join("y" * rng.randint(0, 90) for _ in range(rng.randint(1, 30))) + "\n```")
                elif roll < 0.25:
                    parts.append("z" * rng.randint(100, 900))
                else:
                    parts.append(" ".join(f"w{rng.randint(0, 99)}" for _ in range(rng.randint(1, 50))))
            text = "\n\n".join(parts)
            chunks = fmt.split_markdown(text, limit)
            assert all(len(c) <= limit for c in chunks)
            assert all(_fences(c) % 2 == 0 for c in chunks)
            assert "".join(_words("\n".join(chunks))) == "".join(_words(text))


def test_mrkdwn_fallback_rewrites_bold_headings_and_links_but_not_code():
    out = fmt.to_mrkdwn("# Title\n**bold** [site](https://x.example)\n```\n**raw** <tag>\n```")
    lines = out.split("\n")
    assert lines[0] == "*Title*"
    assert lines[1] == "*bold* <https://x.example|site>"
    assert lines[3] == "**raw** &lt;tag&gt;"


def test_refusal_phrases_carry_no_placeholders():
    for text in fmt.REFUSALS.values():
        assert "{" not in text and "%" not in text


def test_manifest_is_dm_only_socket_mode_with_exactly_the_checked_scopes():
    m = manifest.build_manifest("Sales Helper")
    assert m["settings"]["socket_mode_enabled"] is True
    assert m["settings"]["event_subscriptions"]["bot_events"] == ["message.im"]
    assert m["features"]["app_home"]["messages_tab_enabled"] is True
    assert m["features"]["app_home"]["messages_tab_read_only_enabled"] is False
    assert sorted(m["oauth_config"]["scopes"]["bot"]) == sorted(REQUIRED_BOT_SCOPES)
    assert m["settings"]["org_deploy_enabled"] is False


def test_manifest_names_respect_slack_limits():
    long_name = "A very long harness name that keeps going and going"
    m = manifest.build_manifest(long_name)
    assert len(m["display_information"]["name"]) <= 35
    bot = m["features"]["bot_user"]["display_name"]
    assert len(bot) <= 80
    assert re.fullmatch(r"[a-z0-9._-]+", bot)
    assert manifest.bot_display_name("한글 에이전트") == "agent"


def test_manifest_payload_offers_both_formats_and_a_prefilled_link():
    payload = manifest.manifest_payload("helper")
    assert json.loads(payload["json"]) == payload["manifest"]
    assert "socket_mode_enabled: true" in payload["yaml"]
    prefix = "https://api.slack.com/apps?new_app=1&manifest_json="
    assert payload["create_url"].startswith(prefix)
    assert json.loads(unquote(payload["create_url"][len(prefix):])) == payload["manifest"]
