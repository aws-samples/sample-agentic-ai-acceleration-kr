"""Disagreements between the pool and the teams table — pure.

The audit ledger only knows what the UI did; a group made in the console or a
role deleted by hand shows up here instead, each with the actions the page may
offer. Probing (does the role / policy still exist) is done by the caller; this
function only reads the probe results, so it can be tested without AWS.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.team import TeamConfig
from services.team_service import PROVISION_STALE_SECONDS, is_stale


def _item(kind: str, team: str, detail: str, actions: List[str]) -> Dict[str, Any]:
    return {"kind": kind, "team": team, "detail": detail, "actions": actions}


def compute_drift(groups: Optional[List[str]], teams: List[TeamConfig],
                  probes: Dict[str, Dict[str, Any]], now: Optional[datetime] = None,
                  stale_after_seconds: int = PROVISION_STALE_SECONDS) -> List[Dict[str, Any]]:
    """`groups` None = the pool is not there to compare with (an Entra-only
    stack): the two group kinds are skipped rather than reported for every team.
    `now` None = no stale check for in-flight rows."""
    out: List[Dict[str, Any]] = []
    rows = {t.name: t for t in teams}
    group_set = set(groups or [])

    for name in (sorted(group_set - set(rows)) if groups is not None else []):
        out.append(_item("unregistered_group", name,
                         f"Cognito 그룹 team:{name} 은 있지만 팀으로 등록되지 않았습니다. 실행 역할과 정책이 없어 하네스를 만들 수 없습니다.",
                         ["register"]))

    for name, team in sorted(rows.items()):
        if team.status == "error":
            step = (team.error or {}).get("step", "?")
            msg = (team.error or {}).get("message", "")
            out.append(_item("provisioning_error", name, f"프로비저닝이 {step} 단계에서 실패했습니다: {msg}", ["retry"]))
            continue
        if team.status in ("provisioning", "deleting"):
            if now is not None and is_stale(team.updated_at, now, stale_after_seconds):
                if team.status == "provisioning":
                    out.append(_item("stale_provisioning", name,
                                     "프로비저닝이 끝나지 않은 채 멈췄습니다 (서버 재시작 등). 다시 시도하면 남은 단계부터 이어서 만듭니다.",
                                     ["retry"]))
                else:
                    out.append(_item("stale_deleting", name,
                                     "삭제가 끝나지 않은 채 멈췄습니다. 다시 시도하면 남은 자원부터 지웁니다.", ["retry_delete"]))
            continue
        if team.status != "ready":
            continue
        if groups is not None and name not in group_set:
            if team.managed_by == "platform":
                out.append(_item("missing_group", name, f"Cognito 그룹 team:{name} 이 없습니다. 토큰에 팀이 실리지 않습니다.",
                                 ["cleanup", "recreate_group"]))
            else:
                # The row came from outside the server (provision is 409 for it);
                # the page can only drop the row.
                out.append(_item("missing_group", name, f"Cognito 그룹 team:{name} 이 없습니다. "
                                 "이 팀은 외부에서 만들어져 서버가 그룹을 다시 만들지 않습니다.", ["cleanup"]))
        probe = probes.get(name)
        if team.managed_by == "platform" and probe:
            if probe.get("role") is False:
                out.append(_item("missing_role", name, "실행 역할이 없습니다.", ["retry"]))
            if any(ok is False for ok in (probe.get("policies") or {}).values()):
                out.append(_item("missing_policy", name, "게이트웨이 Cedar 정책이 없습니다.", ["retry"]))
    return out
