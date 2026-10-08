"""Create demo accounts in the Cognito pool so Insights has more than two people.

The stack's terraform declares exactly one admin and one user. A per-person
dashboard with two rows says nothing, so a demo deployment gets a handful of
plain-user accounts that a driver script then talks through the agents.

Idempotent: an account that exists is left as it is (password re-set so the
driver can sign in), and group membership is added only when missing. Run with
the same AWS credentials the deployment used:

    python scripts/seed_demo_users.py --password '<choose-a-password>'
    python scripts/seed_demo_users.py --list

Not terraform on purpose: the installer form is generated from
`standalone/variables.tf` and renders string/bool/number/list variables only, so
a `map(string)` of demo accounts would break it (installer/tests/test_values.py).
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
GROUP = "user"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", help="Permanent password for every demo account (pool policy: 8+, upper, lower, digit).")
    parser.add_argument("--list", action="store_true", help="Print the pool's users and exit.")
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

    for email, persona in DEMO_USERS:
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
        idp.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=args.password, Permanent=True)
        groups = {g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool_id, Username=email)["Groups"]}
        if GROUP not in groups:
            idp.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=GROUP)
        sub = next(
            a["Value"] for a in idp.admin_get_user(UserPoolId=pool_id, Username=email)["UserAttributes"] if a["Name"] == "sub"
        )
        print(f"{'created' if created else 'exists ':8s} {email:32s} {persona:8s} {sub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
