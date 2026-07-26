#!/usr/bin/env bash
# seed.sh — base-outputs.json 값으로 Aurora 샘플 데이터 적재 + OpenSearch 스키마 인덱싱.
#
# 멱등: 재실행 안전. Aurora Serverless v2 가 최소용량에서 깨어나며 첫 Data API 호출이
# 간헐 실패할 수 있어 seed-aurora 를 최대 5회 재시도한다.
#
# 전제: scripts/deploy.sh base 로 base-outputs.json 이 생성돼 있어야 함.
#
# 사용:
#   scripts/seed.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"
SAMPLE="$ROOT/sample-data"
OUT="$INFRA/base-outputs.json"

if [[ ! -f "$OUT" ]]; then
  echo "ERROR: $OUT 없음. 먼저 scripts/deploy.sh base 실행." >&2
  exit 2
fi

# CloudFormation outputs(JSON)에서 값 추출. 키는 base-stack.ts 의 CfnOutput 논리 ID.
jq_out() { jq -r ".AgenticT2SqlBaseStack.$1" "$OUT"; }

# 이 솔루션은 us-west-2 고정 배포다. 셸 프로필의 AWS_REGION(예: us-east-1)이
# 새어 들어오면 Data API 가 잘못된 리전 엔드포인트로 가므로 강제로 고정한다.
export AWS_REGION="us-west-2"
export AWS_DEFAULT_REGION="us-west-2"
export AURORA_CLUSTER_ARN="$(jq_out AuroraClusterArn)"
# seed 의 admin 작업(스키마/데이터 적재, 역할 생성)은 master 자격증명으로 수행.
export AURORA_SECRET_ARN="$(jq_out AuroraMasterSecretArn)"
# agent_ro DB 역할 비밀번호를 이 시크릿(= SQL MCP 의 AURORA_SECRET_ARN)과 동기화(통합 버그 방지).
export AGENT_RO_SECRET_ARN="$(jq_out AgentRoSecretArn)"
export DB_NAME="$(jq_out DbName)"
export OPENSEARCH_ENDPOINT="$(jq_out OpenSearchEndpoint)"
export OPENSEARCH_INDEX="$(jq_out OpenSearchIndex)"
export EMBEDDING_MODEL_ID="${EMBEDDING_MODEL_ID:-amazon.titan-embed-text-v2:0}"

echo "[seed] cluster=$AURORA_CLUSTER_ARN db=$DB_NAME"
echo "[seed] opensearch=$OPENSEARCH_ENDPOINT index=$OPENSEARCH_INDEX"

cd "$SAMPLE"
[[ -d .venv ]] || uv sync --extra dev

# --- Aurora seed (재시도: v2 resume 지연 대응) ---
attempt=1; max=5
until uv run seed-aurora; do
  code=$?
  if [[ $attempt -ge $max ]]; then
    echo "ERROR: seed-aurora 가 ${max}회 실패(exit=$code)." >&2
    exit $code
  fi
  wait=$((attempt * 15))
  echo "[retry] seed-aurora 실패(exit=$code). ${wait}s 후 재시도 ($attempt/$max)..."
  sleep "$wait"
  attempt=$((attempt + 1))
done

# --- OpenSearch 인덱싱 ---
uv run index-schema-docs

# --- M2: semantic layer seed (DynamoDB → Streams → OpenSearch/Neptune 동기화) ---
# semantic-outputs.json 이 있으면(Semantic 스택 배포됨) DynamoDB 에 용어/fewshot/스키마
# 엔티티를 적재한다. OSIS·graph-sync Lambda 가 파생 저장소로 전파한다(최종 일관성).
SEMANTIC_OUT="$INFRA/semantic-outputs.json"
if [[ -f "$SEMANTIC_OUT" ]]; then
  export SEMANTIC_TABLE_NAME="$(jq -r '.AgenticT2SqlSemanticStack.SemanticTableName' "$SEMANTIC_OUT")"
  echo "[seed] semantic table=$SEMANTIC_TABLE_NAME"
  cd "$ROOT/semantic-layer"
  [[ -d .venv ]] || uv sync --extra seed
  uv run seed-semantic
  echo "[seed] semantic layer 적재 완료 (동기화는 수 초 내 전파)."
else
  echo "[seed] semantic-outputs.json 없음 — semantic seed skip (M1 호환)."
fi

echo "[seed] 완료: Aurora 데이터 + OpenSearch 인덱스."
