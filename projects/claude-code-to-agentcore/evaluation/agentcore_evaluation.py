"""
agentcore_evaluation.py — AgentCore Evaluations(에이전트 품질 채점) 래퍼.

배포만으로는 "프로덕션"이 아닙니다. 리포트가 충분히 정확하고 도움이 되는지
지속적으로 채점하는 품질 게이트가 있어야 합니다. AgentCore Evaluations는
런타임이 CloudWatch에 남긴 gen_ai 트레이스를 빌트인/커스텀 평가자로 채점합니다.

세 가지 사용 패턴을 제공합니다(모두 boto3 직접 호출):
  • list_evaluators()        : 사용 가능한 빌트인 평가자 목록(Helpfulness/Correctness 등)
  • create_online_config(...) : 운영 트래픽을 샘플링해 상시 채점(online)
  • start_batch(...)          : 지난 로그를 한 번에 일괄 채점(batch)
  • run_on_demand(...)        : 특정 세션/트레이스 1건만 즉시 채점

API 호출 형태는 aws-samples agentops-kit 레퍼런스에서 검증된 shape을 따릅니다.

환경 변수:
  EVAL_ROLE_ARN    : 평가 실행 IAM 역할 (setup_eval_role.sh 로 생성)
  LOG_GROUP_NAME   : 런타임 로그 그룹 (예: /aws/bedrock-agentcore/runtimes/<id>-DEFAULT)
  SERVICE_NAME     : 트레이스 service.name (런타임 이름)
"""
import os
from datetime import timedelta

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
EVAL_ROLE_ARN = os.environ.get("EVAL_ROLE_ARN", "")
LOG_GROUP_NAME = os.environ.get("LOG_GROUP_NAME", "")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "anycompany_ecommerce")
AGENT_ID = os.environ.get("AGENT_ID", "")

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"})


def _cp():
    """control plane — 평가자/온라인 설정 CRUD."""
    return boto3.client("bedrock-agentcore-control", region_name=REGION, config=_cfg)


def _dp():
    """data plane — 배치 평가."""
    return boto3.client("bedrock-agentcore", region_name=REGION, config=_cfg)


# ── 빌트인 평가자 목록 ──────────────────────────────────────────────────────
def list_evaluators() -> list[dict]:
    resp = _cp().list_evaluators()
    return resp.get("evaluators", resp.get("evaluatorSummaries", []))


# ── 온라인 평가(운영 트래픽 샘플링 상시 채점) ──────────────────────────────
def create_online_config(name: str, evaluator_ids: list[str],
                         sampling_rate: float = 100.0, description: str = "") -> dict:
    params = {
        "onlineEvaluationConfigName": name,
        "evaluationExecutionRoleArn": EVAL_ROLE_ARN,
        "rule": {"samplingConfig": {"samplingPercentage": sampling_rate}},
        "evaluators": [{"evaluatorId": eid} for eid in evaluator_ids],
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "logGroupNames": [LOG_GROUP_NAME],
                "serviceNames": [SERVICE_NAME],
            }
        },
        "enableOnCreate": True,
    }
    if description:
        params["description"] = description
    return _cp().create_online_evaluation_config(**params)


def list_online_configs() -> list[dict]:
    return _cp().list_online_evaluation_configs().get("onlineEvaluationConfigs", [])


# ── 배치 평가(지난 로그 일괄 채점) ─────────────────────────────────────────
def _sanitize(name: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not s or not s[0].isalpha():
        s = "eval_" + s
    return s[:48]


def start_batch(name: str, evaluator_ids: list[str]) -> dict:
    params = {
        "batchEvaluationName": _sanitize(name),
        "evaluators": [{"evaluatorId": eid} for eid in evaluator_ids],
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "logGroupNames": [LOG_GROUP_NAME],
                "serviceNames": [SERVICE_NAME],
            }
        },
    }
    return _dp().start_batch_evaluation(**params)


def get_batch(batch_id: str) -> dict:
    return _dp().get_batch_evaluation(batchEvaluationId=batch_id)


# ── 온디맨드(세션/트레이스 1건 즉시 채점) ──────────────────────────────────
def run_on_demand(evaluator_ids: list[str], session_id: str,
                  trace_id: str = None, look_back_hours: int = 1):
    from bedrock_agentcore.evaluation.client import EvaluationClient
    client = EvaluationClient(region_name=REGION)
    return client.run(
        evaluator_ids=evaluator_ids,
        session_id=session_id,
        agent_id=AGENT_ID or None,
        log_group_name=LOG_GROUP_NAME if not AGENT_ID else None,
        trace_id=trace_id.replace("-", "") if trace_id else None,
        look_back_time=timedelta(hours=look_back_hours),
    )


if __name__ == "__main__":
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        evs = list_evaluators()
        print("사용 가능한 평가자:")
        for e in evs:
            print(f"  - {e.get('evaluatorId')}  ({e.get('evaluatorName', '')})")
    elif cmd == "online":
        ids = sys.argv[2].split(",") if len(sys.argv) > 2 else []
        print(json.dumps(create_online_config("anycompany-ecommerce-online", ids), default=str, indent=2))
    elif cmd == "batch":
        ids = sys.argv[2].split(",") if len(sys.argv) > 2 else []
        print(json.dumps(start_batch("anycompany_ecommerce_batch", ids), default=str, indent=2))
