# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""seed_dev_data — dev DB 에 합성 BI 데이터를 INSERT (golden Tier B 측정용).

배경 (DEVLOG §31.4 / §32): dev usage_logs 가 60행/2명/4일뿐이라 Tier B
분석(outlier/STL/SARIMAX/heatmap)이 통계적으로 무의미 → orchestrator 가
Code Specialist 를 정당하게 회피 → golden Tier B 0%. 이 Lambda 가 충분한
합성 데이터를 넣어 Tier B 분석이 실제 신호를 갖도록 한다.

설계 — golden Tier B 4기법과 매칭:
  - outlier(09): 다수 사용자 중 소수가 비용/토큰 폭주 → IsolationForest 가 잡음
  - STL(10):     일별 비용에 상승 추세 + 주간 주기(주말 저조) → 분해 가능
  - SARIMAX(11): 90일 시계열 + 추세 → 예측 가능
  - heatmap(12): 팀별 업무시간대(9~18시) 집중 패턴

안전장치:
  - 쓰기 자격증명: /llm-gateway/dev/db/gateway-user (app user, 쓰기 권한).
  - 모든 합성 row 는 request_id prefix 'synthetic-' → 멱등 재실행/삭제 가능.
  - 합성 사용자 sso_subject/email prefix 'synthetic-' → 식별·삭제 가능.
  - event={"action":"purge"} 면 합성 데이터만 삭제하고 종료(되돌리기).
  - read-only 가 아닌 별도 일회용 Lambda (query_db 와 분리).

결정성: 시드 고정(난수 seed=42)이라 재현 가능.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
from typing import Any

import boto3
import psycopg2
import psycopg2.extras

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "gateway")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]  # gateway-user (쓰기)
SYNTH_PREFIX = "synthetic-"

MODELS = [
    ("claude-opus-4-7", 0.015, 0.075),       # (alias, in$/1k, out$/1k 근사)
    ("claude-sonnet-4-6", 0.003, 0.015),
    ("claude-haiku-4-5-20251001", 0.0008, 0.004),
]
PROVIDER = "bedrock"
N_SYNTH_USERS = 22          # outlier 분석에 충분한 사용자 수
N_DAYS = 90                 # SARIMAX/STL 에 충분한 시계열 길이


def _conn():
    sm = boto3.client("secretsmanager")
    sec = json.loads(sm.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"])
    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=sec["username"],
        password=sec["password"],
        connect_timeout=10,
    )


def _stable_uuid(seed: str) -> str:
    """결정적 UUID v4-ish (재실행 시 동일). md5 → uuid 포맷."""
    h = hashlib.md5(seed.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def _purge(cur) -> dict:
    """합성 데이터만 삭제 (usage_logs 먼저, 그 다음 users — FK 순서)."""
    cur.execute(
        "DELETE FROM usage.usage_logs WHERE request_id LIKE %s",
        (SYNTH_PREFIX + "%",),
    )
    logs = cur.rowcount
    cur.execute(
        "DELETE FROM auth.users WHERE sso_subject LIKE %s",
        (SYNTH_PREFIX + "%",),
    )
    users = cur.rowcount
    return {"deleted_logs": logs, "deleted_users": users}


def _fetch_fk(cur) -> dict:
    """기존 팀/부서 (합성 사용자 배치용)."""
    cur.execute("SELECT id, dept_id FROM auth.teams ORDER BY name")
    teams = cur.fetchall()
    if not teams:
        raise RuntimeError("auth.teams 비어있음 — 합성 사용자 배치 불가")
    return {"teams": [(str(t[0]), str(t[1])) for t in teams]}


def _seed_users(cur, teams: list[tuple[str, str]]) -> list[dict]:
    """합성 사용자 N명 생성. 결정적 UUID, synthetic- prefix."""
    users = []
    rows = []
    for i in range(N_SYNTH_USERS):
        uid = _stable_uuid(f"synth-user-{i}")
        team_id, dept_id = teams[i % len(teams)]
        email = f"{SYNTH_PREFIX}user{i:02d}@example.com"
        sso = f"{SYNTH_PREFIX}sso-{i:02d}"
        users.append({"id": uid, "team_id": team_id, "dept_id": dept_id, "idx": i})
        rows.append((uid, team_id, email, f"Synthetic User {i:02d}", "DEVELOPER", sso, "synthetic"))
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject, provider)
           VALUES %s ON CONFLICT (id) DO NOTHING""",
        rows,
    )
    return users


def _gen_logs(users: list[dict], now: dt.datetime) -> list[tuple]:
    """90일치 usage_logs 생성 — outlier/추세/주기/시간대 패턴 내장.

    - 사용자별 base 활동량: 대부분 보통, 소수(idx 0,1)는 outlier(폭주).
    - 일별 추세: 후반으로 갈수록 호출량 증가(선형 추세).
    - 주간 주기: 주말(토/일) 활동 0.3배.
    - 시간대: 업무시간(9~18) 집중(heatmap).
    """
    rng = random.Random(42)
    rows: list[tuple] = []
    seq = 0
    for day_off in range(N_DAYS):
        day = (now - dt.timedelta(days=N_DAYS - 1 - day_off)).date()
        dow = day.weekday()  # 0=월 ... 6=일
        weekend = dow >= 5
        trend = 1.0 + 0.8 * (day_off / N_DAYS)        # 후반 1.8배
        day_factor = (0.3 if weekend else 1.0) * trend
        for u in users:
            # 사용자별 활동 강도: idx 0,1 은 outlier (10~20배)
            if u["idx"] <= 1:
                intensity = rng.uniform(10, 20)
            else:
                intensity = rng.uniform(0.5, 2.0)
            n_calls = int(rng.gauss(3, 1) * intensity * day_factor)
            n_calls = max(0, n_calls)
            for _ in range(n_calls):
                seq += 1
                # 업무시간 집중 (9~18시 80%, 그 외 20%)
                if rng.random() < 0.8:
                    hour = rng.randint(9, 18)
                else:
                    hour = rng.randint(0, 23)
                minute = rng.randint(0, 59)
                # KST 기준 시각 → UTC 저장 (KST = UTC+9)
                kst = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=dt.timezone(dt.timedelta(hours=9)))
                req_at = kst.astimezone(dt.timezone.utc)
                alias, in_rate, out_rate = rng.choice(MODELS)
                in_tok = rng.randint(200, 8000)
                out_tok = rng.randint(100, 4000)
                cache_read = rng.randint(0, in_tok // 2)
                cost = round((in_tok * in_rate + out_tok * out_rate) / 1000.0, 6)
                latency = rng.randint(800, 9000)
                status = "SUCCESS" if rng.random() > 0.05 else rng.choice(["ERROR", "TIMEOUT"])
                completed = req_at + dt.timedelta(milliseconds=latency)
                rows.append((
                    f"{SYNTH_PREFIX}{seq:08d}", u["id"], u["team_id"], u["dept_id"],
                    alias, PROVIDER, in_tok, out_tok, 0, cache_read, cost, latency,
                    status, req_at, completed, rng.random() < 0.3, False,
                ))
    return rows


def _insert_logs(cur, rows: list[tuple]) -> int:
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO usage.usage_logs
           (request_id, user_id, team_id, dept_id, model_alias, provider,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            cost_usd, latency_ms, status, requested_at, completed_at,
            is_streaming, estimated_usage)
           VALUES %s ON CONFLICT (request_id) DO NOTHING""",
        rows,
        page_size=500,
    )
    return len(rows)


def lambda_handler(event: dict, context: Any) -> dict:
    """event:
      {"action":"seed"}  (기본) — 합성 데이터 purge 후 재생성 (멱등)
      {"action":"purge"}        — 합성 데이터만 삭제
      {"action":"stats"}        — 현재 데이터 통계만 반환
    """
    action = (event or {}).get("action", "seed")
    conn = _conn()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            if action == "stats":
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT user_id), MIN(requested_at)::date, MAX(requested_at)::date FROM usage.usage_logs")
                tot, du, mn, mx = cur.fetchone()
                return {"ok": True, "action": "stats", "total_logs": tot,
                        "distinct_users": du, "earliest": str(mn), "latest": str(mx)}

            purged = _purge(cur)
            if action == "purge":
                conn.commit()
                return {"ok": True, "action": "purge", **purged}

            # seed
            fk = _fetch_fk(cur)
            users = _seed_users(cur, fk["teams"])
            now = dt.datetime.now(dt.timezone.utc)
            rows = _gen_logs(users, now)
            inserted = _insert_logs(cur, rows)
            conn.commit()
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM usage.usage_logs WHERE request_id LIKE %s", (SYNTH_PREFIX + "%",))
            sc, su = cur.fetchone()
            return {"ok": True, "action": "seed", "purged": purged,
                    "synth_users": len(users), "logs_generated": inserted,
                    "synth_logs_in_db": sc, "synth_distinct_users": su}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
