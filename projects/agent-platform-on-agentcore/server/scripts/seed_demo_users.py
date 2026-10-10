"""Create demo accounts in the Cognito pool so Insights has more than two people.

The stack's terraform declares exactly one admin and one user. A per-person
dashboard with two rows says nothing, so a demo deployment gets a handful of
plain-user accounts that a driver script then talks through the agents.

Idempotent: an account that exists is left as it is (password re-set so the
driver can sign in), and group membership is added only when missing. Run with
the same AWS credentials the deployment used:

    python scripts/seed_demo_users.py --password '<choose-a-password>'
    python scripts/seed_demo_users.py --list

Not terraform on purpose: accounts are data, not infrastructure, and the pool
outlives any one demo.
"""
import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Eight personas, each a different kind of platform user, so the traffic a driver
# sends as them lands on different agents and the per-person table has texture.
# The first five are the daily users; the last three are occasional ones, so the
# per-person distribution has a long tail instead of eight equal rows.
DEMO_USERS = (
    ("jihoon.kim@example.com", "데이터 분석"),
    ("minji.park@example.com", "콘텐츠 마케팅"),
    ("seoyeon.lee@example.com", "리서치"),
    ("donghyun.choi@example.com", "플랫폼 운영"),
    ("yuna.jung@example.com", "기술 문서"),
    ("hyunwoo.kang@example.com", "영업"),
    ("sooyoung.han@example.com", "고객 지원"),
    ("taeyang.oh@example.com", "인턴"),
)

# Workshop personas: one account per team, named after the team so a participant
# can tell at a glance whose seat they are in.
WORKSHOP_USERS = {
    "finance": ("finance-user@example.com", "재무팀"),
    "hr": ("hr-user@example.com", "HR"),
    "support": ("support-user@example.com", "고객지원"),
}

GROUP = "user"


def ensure_user(idp, pool_id: str, email: str, password: str, groups: list[str]) -> tuple[bool, str]:
    """Create or update a user and ensure they are in the specified groups.

    Returns (created, sub) where created is True if the user was newly created.
    Raises SystemExit if a required group does not exist.
    """
    try:
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            MessageAction="SUPPRESS",
        )
        created = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "UsernameExistsException":
            raise
        created = False

    idp.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)

    existing_groups = {g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool_id, Username=email)["Groups"]}
    for group in groups:
        if group not in existing_groups:
            try:
                idp.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                    raise SystemExit(f"Group {group} does not exist; add the team to terraform `teams` and apply first")
                raise

    sub = next(
        a["Value"] for a in idp.admin_get_user(UserPoolId=pool_id, Username=email)["UserAttributes"] if a["Name"] == "sub"
    )
    return created, sub


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", help="Permanent password for every demo account (pool policy: 8+, upper, lower, digit).")
    parser.add_argument("--list", action="store_true", help="Print the pool's users and exit.")
    parser.add_argument("--teams", default="", help="Comma-separated team names. Demo users are spread across them round-robin and one <team>-user@example.com is created per team; groups must exist (terraform `teams`).")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)
    pool_id = os.environ.get("COGNITO_USER_POOL_ID")
    region = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-1"
    if not pool_id:
        print("COGNITO_USER_POOL_ID is not set", file=sys.stderr)
        return 2
    idp = boto3.client("cognito-idp", region_name=region)

    if args.list:
        for user in idp.list_users(UserPoolId=pool_id)["Users"]:
            email = next((a["Value"] for a in user["Attributes"] if a["Name"] == "email"), "")
            groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool_id, Username=user["Username"])["Groups"]]
            print(f"{email:32s} {user['UserStatus']:12s} {','.join(groups):8s} {user['Username']}")
        return 0

    if not args.password:
        parser.error("--password is required unless --list")

    teams = [t.strip() for t in args.teams.split(",") if t.strip()]

    for i, (email, persona) in enumerate(DEMO_USERS):
        groups = [GROUP] + ([f"team:{teams[i % len(teams)]}"] if teams else [])
        created, sub = ensure_user(idp, pool_id, email, args.password, groups)
        print(f"{'created' if created else 'exists ':8s} {email:32s} {persona:8s} {','.join(groups):24s} {sub}")

    for team in teams:
        if team in WORKSHOP_USERS:
            email, persona = WORKSHOP_USERS[team]
            created, sub = ensure_user(idp, pool_id, email, args.password, [GROUP, f"team:{team}"])
            print(f"{'created' if created else 'exists ':8s} {email:32s} {persona:8s} {GROUP},team:{team:16s} {sub}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
