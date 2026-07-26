#!/usr/bin/env bash
# create-e2e-users.sh — E2E 검증용 Cognito 테스트 사용자를 멱등 생성한다.
#
# 생성 대상 (레벨5·6 E2E 전제):
#   - e2e-user@example.com     : 그룹 없음(일반 인증 사용자) — Cedar 광역 permit 경로 검증
#   - e2e-denied@example.com   : Denied 그룹 — Cedar forbid-wins 검증 (그룹 없으면 생성)
#   - e2e-manager@example.com  : Manager 그룹 — M4 admin 큐레이션·승인 경로 검증
#
# 비밀번호는 Secrets Manager `agentic-t2sql/e2e/user-password` 에 보관한다.
#   - 시크릿이 없으면 랜덤 강력 비밀번호를 생성해 시크릿으로 저장한다.
#   - 평문을 stdout 으로 출력하지 않는다(마스킹). 스크립트 종료 시 임시 파일도 제거.
#   - 각 사용자에게 admin-set-user-password --permanent 로 설정(FORCE_CHANGE_PASSWORD 회피).
#
# 전제: infra/base-outputs.json (Base 스택 배포 산출물 — CognitoUserPoolId 필요).
#
# 사용:
#   scripts/create-e2e-users.sh
#
# 정리: cleanup.sh 가 시크릿을 삭제한다. 사용자는 user pool 삭제와 함께 사라진다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"
# 이 레포 로컬 기본 리전은 us-east-1 — 반드시 강제 고정한다(`:-` 폴백 금지).
export AWS_REGION=us-west-2
REGION="$AWS_REGION"

BASE_OUTPUTS="$INFRA/base-outputs.json"
E2E_SECRET="agentic-t2sql/e2e/user-password"

E2E_USER="e2e-user@example.com"
E2E_DENIED_USER="e2e-denied@example.com"
E2E_MANAGER_USER="e2e-manager@example.com"

if [[ ! -f "$BASE_OUTPUTS" ]]; then
  echo "[오류] $BASE_OUTPUTS 없음 — Base 스택을 먼저 배포하세요(scripts/deploy.sh base)." >&2
  exit 2
fi

POOL_ID="$(jq -r '.AgenticT2SqlBaseStack.CognitoUserPoolId // empty' "$BASE_OUTPUTS")"
if [[ -z "$POOL_ID" ]]; then
  echo "[오류] base-outputs.json 에 CognitoUserPoolId 가 없습니다." >&2
  exit 2
fi

echo "리전: $REGION / user pool: $POOL_ID"

# ── 비밀번호 시크릿 확보(없으면 생성) ──────────────────────────────────────
# 비밀번호는 변수에만 담고 echo 하지 않는다.
if aws secretsmanager describe-secret --secret-id "$E2E_SECRET" --region "$REGION" >/dev/null 2>&1; then
  echo "[secrets] 기존 시크릿 사용: $E2E_SECRET"
  PASSWORD="$(aws secretsmanager get-secret-value --secret-id "$E2E_SECRET" \
    --region "$REGION" --query SecretString --output text)"
else
  echo "[secrets] 시크릿 생성: $E2E_SECRET (랜덤 강력 비밀번호)"
  # Cognito 기본 정책(대/소/숫자/기호 포함, 8자 이상) 충족: 랜덤 24자 + 고정 문자 클래스.
  # `tr </dev/urandom | head` 조합은 SIGPIPE 로 pipefail 을 유발하므로 openssl 을 쓴다.
  RANDOM_PART="$(openssl rand -hex 12)"
  PASSWORD="E2e!${RANDOM_PART}#7"
  aws secretsmanager create-secret --name "$E2E_SECRET" \
    --description "agentic-t2sql E2E 테스트 사용자 공용 비밀번호" \
    --secret-string "$PASSWORD" --region "$REGION" >/dev/null
fi

if [[ -z "${PASSWORD:-}" ]]; then
  echo "[오류] E2E 비밀번호를 확보하지 못했습니다." >&2
  exit 2
fi

# ── 그룹 보장 ─────────────────────────────────────────────────────────────
# Admin/Manager 는 Base 스택(CDK)이 만든다. Denied 는 E2E 전용이라 여기서 보장한다.
ensure_group() {
  local group="$1" desc="$2"
  if aws cognito-idp get-group --user-pool-id "$POOL_ID" --group-name "$group" \
      --region "$REGION" >/dev/null 2>&1; then
    echo "  [skip] 그룹 존재: $group"
    return 0
  fi
  echo "  [create] 그룹 생성: $group"
  aws cognito-idp create-group --user-pool-id "$POOL_ID" --group-name "$group" \
    --description "$desc" --region "$REGION" >/dev/null
}

echo "[groups] 그룹 보장"
ensure_group Manager 'Manager 페르소나 (semantic 큐레이션·승인)'
ensure_group Denied 'E2E Cedar 거부 검증용 그룹 (모든 action forbid)'

# ── 사용자 보장 ───────────────────────────────────────────────────────────
# bash 3.2 호환: 연관 배열 대신 함수 인자로 (username, group) 을 넘긴다.
ensure_user() {
  local username="$1" group="${2:-}"
  if aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$username" \
      --region "$REGION" >/dev/null 2>&1; then
    echo "  [skip] 사용자 존재: $username (비밀번호만 갱신)"
  else
    echo "  [create] 사용자 생성: $username"
    aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$username" \
      --user-attributes "Name=email,Value=$username" Name=email_verified,Value=true \
      --message-action SUPPRESS --region "$REGION" >/dev/null
  fi

  # 멱등: 항상 시크릿의 비밀번호로 permanent 설정(CONFIRMED 상태 보장).
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username "$username" \
    --password "$PASSWORD" --permanent --region "$REGION" >/dev/null

  if [[ -n "$group" ]]; then
    # add-to-group 은 이미 속해 있어도 성공(멱등).
    aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
      --username "$username" --group-name "$group" --region "$REGION" >/dev/null
    echo "    그룹 배정: $group"
  else
    echo "    그룹 없음(일반 인증 사용자)"
  fi
}

echo "[users] 사용자 보장"
ensure_user "$E2E_USER" ""                 # 관례 유지: 일반 사용자는 그룹 없음
ensure_user "$E2E_DENIED_USER" "Denied"
ensure_user "$E2E_MANAGER_USER" "Manager"

unset PASSWORD

cat <<EOF

[완료] E2E 사용자 준비됨:
  - $E2E_USER (그룹 없음)
  - $E2E_DENIED_USER (Denied)
  - $E2E_MANAGER_USER (Manager)
비밀번호는 Secrets Manager 시크릿 '$E2E_SECRET' 에 있습니다(평문 미출력).
E2E 검증기는 이 시크릿을 자동으로 읽습니다: scripts/e2e-smoke.sh
EOF
