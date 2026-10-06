#!/usr/bin/env bash
# Restore agent-platform from backup.sh output.
#
#   ./restore.sh <backup_dir> rehearsal [components]   Restore to temp names, verify, clean up
#   ./restore.sh <backup_dir> live      [components]   Restore to actual resources
#
#   components: comma-separated, default all = ddb,s3,cognito
#   environment: KEEP=1  skip cleanup after rehearsal
#               FORCE=1 skip non-empty target check for live
#
# Supported backup components: DynamoDB, S3, Cognito users
# Not supported: AgentCore Memory events (no official import)
set -uo pipefail

export AWS_REGION="${AWS_REGION:-ap-northeast-1}"
AWS_PAGER=""; export AWS_PAGER

project="${PROJECT:-bap}"
bdir="${1:?backup_dir}"; mode="${2:?rehearsal|live}"; comps="${3:-all}"
[[ "$mode" == rehearsal || "$mode" == live ]] || { echo "mode must be rehearsal|live"; exit 2; }
[[ "$comps" == all ]] && comps="ddb,s3,cognito"
has() { [[ ",$comps," == *",$1,"* ]]; }

acct=$(aws sts get-caller-identity --query Account --output text)
stamp=$(date -u +%H%M%S)
sfx="rt$stamp"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

declare -a RESULTS=(); declare -a CLEANUP=()
log()  { printf '\033[0;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
pass() { RESULTS+=("PASS  $1"); printf '\033[0;32m[PASS]\033[0m %s\n' "$1"; }
fail() { RESULTS+=("FAIL  $1"); printf '\033[0;31m[FAIL]\033[0m %s\n' "$1"; }
py()   { python3 "$@"; }

ddb_target() { [[ $mode == live ]] && echo "$1" || echo "$1-$sfx"; }

ddb_create_like() {
  py - "$1" "$2" > "$work/ct.json" <<'PY'
import json,sys
t=json.load(open(sys.argv[1]))["Table"]
o={"TableName":sys.argv[2],"KeySchema":t["KeySchema"],"AttributeDefinitions":t["AttributeDefinitions"],"BillingMode":"PAY_PER_REQUEST"}
g=[{"IndexName":x["IndexName"],"KeySchema":x["KeySchema"],"Projection":x["Projection"]} for x in t.get("GlobalSecondaryIndexes",[])]
if g: o["GlobalSecondaryIndexes"]=g
print(json.dumps(o))
PY
  aws dynamodb create-table --cli-input-json "file://$work/ct.json" --query TableDescription.TableStatus --output text
  aws dynamodb wait table-exists --table-name "$2"
}

ddb_batch_write() {
  py - "$1" "$2" "$work" <<'PY'
import json,sys
items=json.load(open(sys.argv[1]))["Items"]; tbl=sys.argv[2]; w=sys.argv[3]; n=0
for i in range(0,len(items),25):
    json.dump({tbl:[{"PutRequest":{"Item":x}} for x in items[i:i+25]]},open(f"{w}/bw-{n:04d}.json","w")); n+=1
print(n)
PY
  for bwf in "$work"/bw-*.json; do
    [[ -f "$bwf" ]] || continue
    req="$bwf"
    for _ in 1 2 3 4 5; do
      un=$(aws dynamodb batch-write-item --request-items "file://$req" --query 'UnprocessedItems' --output json)
      [[ -z "$un" || "$un" == "{}" ]] && break
      echo "$un" > "$work/unproc.json"; req="$work/unproc.json"; sleep 2
    done
    rm -f "$bwf"
  done
}

ddb_compare() {
  aws dynamodb scan --table-name "$2" --output json > "$work/after.json"
  py - "$1" "$work/after.json" <<'PY'
import json,sys
a=sorted(json.dumps(x,sort_keys=True) for x in json.load(open(sys.argv[1]))["Items"])
b=sorted(json.dumps(x,sort_keys=True) for x in json.load(open(sys.argv[2]))["Items"])
print(f"backup={len(a)} restored={len(b)} identical={a==b}"); sys.exit(0 if a==b else 1)
PY
}

do_ddb() {
  has ddb || return
  for f in "$bdir"/dynamodb/*.json; do
    [[ "$f" == *.describe.json ]] && continue
    local tbl; tbl=$(basename "$f" .json); local tgt; tgt=$(ddb_target "$tbl")
    local n; n=$(py -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["Items"]))' "$f")
    if [[ $mode == rehearsal ]]; then
      log "ddb $tbl: create temp table $tgt"; ddb_create_like "${f%.json}.describe.json" "$tgt" >/dev/null
      CLEANUP+=("aws dynamodb delete-table --table-name $tgt")
    else
      local cur; cur=$(aws dynamodb scan --table-name "$tgt" --select COUNT --query Count --output text 2>/dev/null || echo 0)
      if [[ "$cur" != 0 && "${FORCE:-0}" != 1 ]]; then fail "ddb $tbl: target not empty (items=$cur), use FORCE=1"; continue; fi
    fi
    log "ddb $tbl: batch-write $n items → $tgt"; ddb_batch_write "$f" "$tgt" >/dev/null
    local r; r=$(ddb_compare "$f" "$tgt") && pass "ddb $tbl ($r)" || fail "ddb $tbl ($r)"
  done
}

do_s3() {
  has s3 || return
  for d in "$bdir"/s3/*/; do
    [[ -d "$d" ]] || continue
    local b; b=$(basename "$d"); local tgt
    if [[ $mode == live ]]; then tgt="$project-$b-$AWS_REGION"
    else tgt="$project-$b-$sfx-$acct"
      log "s3 $b: create temp bucket $tgt"
      aws s3api create-bucket --bucket "$tgt" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null 2>&1 || true
      CLEANUP+=("aws s3 rb s3://$tgt --force")
    fi
    local n; n=$(find "$d" -type f 2>/dev/null | wc -l)
    if [[ $mode == live ]]; then
      local cur; cur=$(aws s3 ls "s3://$tgt" --recursive 2>/dev/null | wc -l || echo 0)
      if [[ "$cur" != 0 && "${FORCE:-0}" != 1 ]]; then fail "s3 $b: target not empty ($cur objects), use FORCE=1"; continue; fi
    fi
    log "s3 $b: sync $n files → s3://$tgt"; aws s3 sync "$d" "s3://$tgt" --quiet 2>/dev/null || true
    local m; m=$(aws s3 ls "s3://$tgt" --recursive 2>/dev/null | wc -l || echo 0)
    [[ "$n" == "$m" ]] && pass "s3 $b (files=$n objects=$m)" || fail "s3 $b (files=$n objects=$m)"
  done
}

do_cognito() {
  has cognito || return
  [[ -f "$bdir/cognito/pool-id.txt" ]] || { log "cognito: backup not found"; return; }
  local pool; pool=$(cat "$bdir/cognito/pool-id.txt" 2>/dev/null)
  [[ -z "$pool" ]] && { fail "cognito: no pool-id"; return; }
  log "cognito: restoring users from pool $pool"
  if [[ $mode == rehearsal ]]; then
    log "cognito: rehearsal restore not implemented"; fail "cognito: not supported in rehearsal mode"
  else
    log "cognito: users must be manually recreated (passwords not exported); see $bdir/cognito/users.json"
    pass "cognito (manual restore required)"
  fi
}

log "Restore from $bdir (project=$project, mode=$mode, components=$comps)"

do_ddb
do_s3
do_cognito

echo ""
echo "=== Results ==="
for r in "${RESULTS[@]}"; do echo "$r"; done

if [[ "$mode" == rehearsal && "${KEEP:-0}" != 1 ]]; then
  log "Cleaning up rehearsal resources..."
  for c in "${CLEANUP[@]}"; do eval "$c" 2>/dev/null || true; done
  ok "Cleanup complete"
fi
