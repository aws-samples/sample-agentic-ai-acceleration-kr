"""The Slack app manifest for one Harness: a DM-only bot over Socket Mode.

The admin pastes it (or opens the prefilled create link) at api.slack.com, so
everything the server relies on is decided here rather than in a setup guide:
Socket Mode on, the Messages tab writable, `message.im` as the only event, and
exactly the bot scopes `api.REQUIRED_BOT_SCOPES` checks. The App-Level Token
(`connections:write`) cannot be declared in a manifest; the admin generates it
under Basic Information.
"""
import json
import re
from typing import Any, Dict
from urllib.parse import quote

import yaml

from integrations.slack.api import REQUIRED_BOT_SCOPES

# Slack's limits on the two names.
APP_NAME_MAX = 35
BOT_NAME_MAX = 80

CREATE_APP_URL = "https://api.slack.com/apps?new_app=1&manifest_json="


def app_name(harness_name: str) -> str:
    name = (harness_name or "").strip() or "Agent"
    return name[:APP_NAME_MAX].strip()


def bot_display_name(harness_name: str) -> str:
    """Lower-case a-z, 0-9, `-`, `_`, `.` only: anything else becomes `-`."""
    name = re.sub(r"[^a-z0-9._-]+", "-", (harness_name or "").lower()).strip("-._")
    return (name or "agent")[:BOT_NAME_MAX]


def build_manifest(harness_name: str) -> Dict[str, Any]:
    return {
        "display_information": {
            "name": app_name(harness_name),
            "description": f"{app_name(harness_name)} 에이전트와 1:1 DM으로 대화합니다.",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": bot_display_name(harness_name),
                "always_online": True,
            },
        },
        "oauth_config": {"scopes": {"bot": list(REQUIRED_BOT_SCOPES)}},
        "settings": {
            "event_subscriptions": {"bot_events": ["message.im"]},
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def manifest_payload(harness_name: str) -> Dict[str, Any]:
    """What GET …/slack/manifest returns: both formats and the create link."""
    manifest = build_manifest(harness_name)
    as_json = json.dumps(manifest, ensure_ascii=False, indent=2)
    return {
        "manifest": manifest,
        "json": as_json,
        "yaml": yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        "create_url": CREATE_APP_URL + quote(json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))),
        "app_scopes": ["connections:write"],
    }
