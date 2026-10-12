"""One-time: turn the rows terraform used to seed (managed_by=terraform) into
platform teams the Organization page owns.

Why a script: terraform no longer declares teams (2026-10-10). Its roles
`<project>-harness-<team>` (path `/`) and Cedar policies `<project>_team_<team>_*`
are destroyed by the apply that removes them; the Cognito groups are kept
(`terraform state rm`) because the members live there. What is left is a row
per team pointing at a role that no longer exists. This script re-provisions
each such row exactly as the Organization page would (group adopted, role
recreated under `/<project>/teams/` with the harness boundary, Cedar policies
from `--tools`), then repoints harnesses that still name the old role ARN.

IAM role names are account-unique, so run this only after the apply removed
the old roles. Dry-run by default; `--apply` writes.

    python scripts/migrate_terraform_teams.py \
        --table bap-teams --pool-id ap-northeast-1_xxx --boundary-arn arn:aws:iam::123:policy/bap-harness-boundary \
        --policy-engine-id bap_gateway_policy-xxxx --gateway-arn arn:aws:bedrock-agentcore:... \
        --skills-bucket bap-skills-123 --shared-actions bap-web-search___WebSearch \
        --tools '{"finance": ["bap-platform-tools___fetch_url", "@builtin"]}' --apply
"""
import argparse
import json
import os
import sys
from typing import Any, Dict, List

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.knowledge import now_iso  # noqa: E402
from models.team import TeamConfig  # noqa: E402
from repositories.team_repository import TeamRepository  # noqa: E402
from services.team_provisioner import ProvisionError, TeamProvisioner, account_id_of, role_name  # noqa: E402
from services.team_service import _from_row  # noqa: E402

LEGACY = "terraform"


def old_role_arn(account: str, project: str, team: str) -> str:
    return f"arn:aws:iam::{account}:role/{role_name(project, team)}"


def harnesses_on(control, role_arns: Dict[str, str]) -> List[Dict[str, Any]]:
    """[{harnessId, name, team}] for every harness whose execution role is one of the old ARNs."""
    by_arn = {arn: team for team, arn in role_arns.items()}
    out: List[Dict[str, Any]] = []
    token = None
    while True:
        kwargs: Dict[str, Any] = {"maxResults": 50}
        if token:
            kwargs["nextToken"] = token
        page = control.list_harnesses(**kwargs)
        for h in page.get("harnesses", []):
            detail = control.get_harness(harnessId=h["harnessId"]).get("harness", {})
            team = by_arn.get(detail.get("executionRoleArn", ""))
            if team:
                out.append({"harnessId": h["harnessId"], "name": detail.get("harnessName") or h.get("harnessName"), "team": team})
        token = page.get("nextToken")
        if not token:
            return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", required=True)
    p.add_argument("--project", default=os.getenv("PLATFORM", "bap"))
    p.add_argument("--region", default=os.getenv("AWS_REGION", "ap-northeast-1"))
    p.add_argument("--pool-id", required=True)
    p.add_argument("--boundary-arn", required=True)
    p.add_argument("--policy-engine-id", default="")
    p.add_argument("--gateway-arn", default="")
    p.add_argument("--skills-bucket", required=True)
    p.add_argument("--shared-actions", default="", help="Comma-separated, as TEAM_SHARED_ACTIONS.")
    p.add_argument("--tools", default="{}", help="JSON: team => allowed_tools list for the new row.")
    p.add_argument("--only", default="", help="Comma-separated team names; default every managed_by=terraform row.")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    tools: Dict[str, List[str]] = json.loads(args.tools)
    only = {t.strip() for t in args.only.split(",") if t.strip()}
    account = account_id_of(args.boundary_arn)

    repo = TeamRepository(table_name=args.table, region_name=args.region)
    control = boto3.client("bedrock-agentcore-control", region_name=args.region)
    prov = TeamProvisioner(
        boto3.client("cognito-idp", region_name=args.region), boto3.client("iam", region_name=args.region), control,
        user_pool_id=args.pool_id, project=args.project, region=args.region, account_id=account,
        boundary_arn=args.boundary_arn, skills_bucket=args.skills_bucket, policy_engine_id=args.policy_engine_id,
        gateway_arn=args.gateway_arn, shared_actions=[a for a in args.shared_actions.split(",") if a.strip()],
    )

    rows = [r for r in repo.list_teams() if r.get("managed_by") == LEGACY and (not only or r.get("name") in only)]
    if not rows:
        print("no managed_by=terraform rows; nothing to do")
        return 0
    old_arns = {r["name"]: old_role_arn(account, args.project, r["name"]) for r in rows}
    affected = harnesses_on(control, old_arns)
    print(f"{len(rows)} row(s): {', '.join(r['name'] for r in rows)}")
    for h in affected:
        print(f"  harness {h['name']} ({h['harnessId']}) runs as the old {h['team']} role")
    if not args.apply:
        print("dry run; pass --apply to migrate")
        return 0

    new_arns: Dict[str, str] = {}
    for row in rows:
        team = _from_row(row)
        assert team is not None
        team = team.model_copy(update={
            "managed_by": "platform", "status": "provisioning", "execution_role_arn": "", "policy_ids": {},
            "allowed_tools": tools.get(team.name, team.allowed_tools), "updated_by": "migration", "updated_at": now_iso(),
        })
        repo.put_team(team.model_dump())
        try:
            done: TeamConfig = prov.provision(team)
        except ProvisionError as exc:
            repo.put_team({**team.model_dump(), "status": "error", "error": {"step": exc.step, "message": exc.message, "at": now_iso()}})
            print(f"  {team.name}: FAILED at {exc.step}: {exc.message} (row left in error; retry from /org)")
            continue
        repo.put_team({**done.model_dump(), "status": "ready", "error": None, "updated_at": now_iso()})
        new_arns[team.name] = done.execution_role_arn
        print(f"  {team.name}: role {done.execution_role_arn}, policies {done.policy_ids}")

    for h in affected:
        arn = new_arns.get(h["team"])
        if not arn:
            print(f"  harness {h['name']}: team {h['team']} not migrated, left alone")
            continue
        control.update_harness(harnessId=h["harnessId"], executionRoleArn=arn)
        print(f"  harness {h['name']}: executionRoleArn -> {arn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
