# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""커스텀 Code Interpreter 부트스트랩 (control-plane, 1회 실행).

§49: 기본 Code Interpreter 는 boto3 자격증명이 없어 샌드박스가 S3 에 못 쓴다
(report PDF·chart PNG 업로드 전부 실패). execution_role_arn 을 주입한 커스텀
인터프리터를 만들면 샌드박스가 execution role 자격을 받아 직접 S3 쓰기 가능
(실측 확정). 이 인터프리터는 **재사용 리소스** — 한 번 만들고 ID 를 agent env
`CODE_INTERPRETER_ID` 로 주입해 execute_python 이 `code_session(identifier=ID)` 로 쓴다.

사용:
  python scripts/create_code_interpreter.py            # 생성 → ID 출력
  python scripts/create_code_interpreter.py --delete ID  # 삭제

terraform 에 bedrock-agentcore code-interpreter 리소스 타입이 없어(provider 미지원)
boto3 control-plane 으로 부트스트랩한다. dev/prod 각 1회.
"""

from __future__ import annotations

import argparse
import os

from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
# dev execution role. prod 는 해당 role ARN 으로 교체.
DEFAULT_ROLE = "arn:aws:iam::123456789012:role/llm-gateway-dev-chat-agent-execution"
NAME = "chat_agent_interp"  # [a-zA-Z][a-zA-Z0-9_]{0,47}


def create(role_arn: str) -> None:
    ci = CodeInterpreter(REGION)
    resp = ci.create_code_interpreter(
        name=NAME,
        execution_role_arn=role_arn,
        # PUBLIC = agent runtime 과 동일. S3/KMS 는 execution role 로 도달(인터넷
        # 무관 — AWS API 엔드포인트). VPC 가 필요하면 networkMode=VPC + vpcConfig.
        network_configuration={"networkMode": "PUBLIC"},
        description="admin-chat-agent report/code specialist — execution-role S3 write",
    )
    print(f"codeInterpreterId={resp['codeInterpreterId']}")
    print(f"status={resp.get('status')}")
    print(f"\n→ agent env 에 주입: CODE_INTERPRETER_ID={resp['codeInterpreterId']}")


def delete(interp_id: str) -> None:
    ci = CodeInterpreter(REGION)
    resp = ci.delete_code_interpreter(interp_id)
    print(f"deleted {interp_id}: {resp.get('status')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", metavar="ID", help="삭제할 interpreter id")
    ap.add_argument("--role", default=DEFAULT_ROLE, help="execution role ARN")
    args = ap.parse_args()
    if args.delete:
        delete(args.delete)
    else:
        create(args.role)
