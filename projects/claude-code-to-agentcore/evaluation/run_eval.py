"""
run_eval.py — 이커머스 에이전트 품질 평가 러너.

test_cases.json 의 케이스를 배포된 AgentCore Runtime에 돌려, 응답을 빠른
로컬 휴리스틱(기대 키워드 적중률 + 수치 포함 여부 + 지연시간)으로 채점합니다.
프로덕션 트레이스 기반의 AgentCore Evaluations(→ agentcore_evaluation.py)로
넘어가기 전, 회귀 테스트처럼 가볍게 돌릴 수 있는 1차 품질 게이트입니다.

    python run_eval.py                 # 전체 케이스 평가
    python run_eval.py --limit 2       # 앞 2개만
    python run_eval.py --report results.json

구조는 aws-samples agentops-kit 의 run_eval.py 를 이커머스 런타임에 맞춘 것입니다.
"""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_NAME = os.environ.get("SERVICE_NAME", "anycompany_ecommerce")


def load_test_cases() -> list[dict]:
    with open(os.path.join(HERE, "test_cases.json")) as f:
        return json.load(f)


def invoke_runtime(prompt: str) -> str:
    """배포된 Runtime을 호출해 텍스트 응답을 모아 반환."""
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    arn = next(r["agentRuntimeArn"] for r in ctl.list_agent_runtimes()["agentRuntimes"]
               if r["agentRuntimeName"] == AGENT_NAME)
    rt = boto3.client("bedrock-agentcore", region_name=REGION,
                      config=Config(read_timeout=300, retries={"max_attempts": 0}))
    resp = rt.invoke_agent_runtime(
        agentRuntimeArn=arn, runtimeSessionId=uuid.uuid4().hex * 2,
        payload=json.dumps({"prompt": prompt}).encode(),
        contentType="application/json", accept="text/event-stream")
    out = []
    for line in resp["response"].read().decode().splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                out.append(line[6:])
    return "\n".join(str(x) for x in out)


def score(response: str, tc: dict) -> dict:
    """빠른 로컬 채점: 기대 키워드 적중률 + 수치 포함 여부."""
    kws = tc.get("expected_keywords", [])
    hits = sum(1 for k in kws if k.lower() in response.lower())
    keyword_score = hits / max(len(kws), 1)
    has_numbers = any(c.isdigit() for c in response)
    return {"keyword_score": round(keyword_score, 3), "keyword_hits": hits,
            "keyword_total": len(kws), "has_numbers": has_numbers}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    cases = load_test_cases()
    if args.limit:
        cases = cases[:args.limit]

    results = []
    for tc in cases:
        print(f"> {tc['id']} ({tc['category']}) ...", end=" ", flush=True)
        t0 = time.time()
        try:
            resp = invoke_runtime(tc["prompt"])
            sc = score(resp, tc)
            sc.update({"test_id": tc["id"], "category": tc["category"],
                       "latency_s": round(time.time() - t0, 1),
                       "status": "success", "response": resp[:500]})
            print(f"키워드 {sc['keyword_hits']}/{sc['keyword_total']}  "
                  f"숫자 {'O' if sc['has_numbers'] else 'X'}  {sc['latency_s']}s")
        except Exception as e:
            sc = {"test_id": tc["id"], "category": tc["category"],
                  "status": "error", "error": str(e)[:200]}
            print(f"ERROR: {e}")
        results.append(sc)

    ok = [r for r in results if r["status"] == "success"]
    if ok:
        avg_kw = sum(r["keyword_score"] for r in ok) / len(ok)
        num_rate = sum(1 for r in ok if r["has_numbers"]) / len(ok)
        avg_lat = sum(r["latency_s"] for r in ok) / len(ok)
        print("\n── 요약 ──")
        print(f"  케이스 {len(ok)}/{len(results)} 성공")
        print(f"  평균 키워드 적중률 : {avg_kw:.0%}")
        print(f"  수치 포함률        : {num_rate:.0%}")
        print(f"  평균 지연시간      : {avg_lat:.1f}s")

    if args.report:
        payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
                   "agent": AGENT_NAME, "results": results}
        with open(args.report, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n결과 저장: {args.report}")


if __name__ == "__main__":
    main()
