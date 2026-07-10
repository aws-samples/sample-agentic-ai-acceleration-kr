-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Rate Limit Multi-Scope Sliding Window Check
--
-- 3-scope fast-fail 원자 체크: USER → TEAM → GLOBAL 순서로 검사하고,
-- 첫 번째 violation에서 즉시 거부 반환 (이후 스코프는 검사 생략).
-- 전 스코프 통과 시에만 모든 카운터를 증가 (all-or-nothing).
--
-- KEYS[i]                 = 스코프 i 의 ZSET 키 (예: {{USER:uid:model}}:rpm)
-- ARGV[1]                 = now_ms
-- ARGV[2]                 = request_id (ZSET member)
-- ARGV[3]                 = window_ms (기본 60000)
-- ARGV[4]                 = N (스코프 수)
-- ARGV[5..(4+N)]          = 스코프별 limit (nil/0 이면 체크 스킵)
-- ARGV[(5+N)..(4+2N)]     = 스코프별 이름 ('USER'/'TEAM'/'GLOBAL') — 에러 응답용
--
-- Returns JSON:
--   통과:  {allowed=true, scope=null, remaining=-1, retry_after=null, window_reset=null}
--   거부:  {allowed=false, scope='USER'|'TEAM'|'GLOBAL', limit=N, remaining=0,
--          retry_after=초, window_reset=unix초}

local now_ms     = tonumber(ARGV[1])
local request_id = ARGV[2]
local window_ms  = tonumber(ARGV[3]) or 60000
local num_scopes = tonumber(ARGV[4])

local window_start = now_ms - window_ms

-- Phase 1: 체크 (side-effect 최소화 — ZREMRANGEBYSCORE는 허용, ZADD는 안 함)
for i = 1, num_scopes do
    local key   = KEYS[i]
    local limit = tonumber(ARGV[4 + i])

    if limit and limit > 0 then
        -- 윈도우 밖 항목 제거 (모든 스코프 공통 cleanup)
        redis.call('ZREMRANGEBYSCORE', key, 0, window_start)

        local current = redis.call('ZCARD', key)
        if current >= limit then
            local scope_name = ARGV[4 + num_scopes + i]

            -- retry_after 계산 (가장 오래된 entry + window)
            local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
            local window_reset_ms = now_ms + window_ms
            if #oldest > 0 then
                window_reset_ms = tonumber(oldest[2]) + window_ms
            end
            local retry_after = math.ceil((window_reset_ms - now_ms) / 1000)
            if retry_after < 1 then retry_after = 1 end

            return cjson.encode({
                allowed      = false,
                scope        = scope_name,
                limit_type   = 'rpm',
                limit        = limit,
                remaining    = 0,
                retry_after  = retry_after,
                window_reset = math.ceil(window_reset_ms / 1000)
            })
        end
    end
end

-- Phase 2: 모든 스코프 통과 — 카운터 증가 (atomic)
for i = 1, num_scopes do
    local key   = KEYS[i]
    local limit = tonumber(ARGV[4 + i])

    if limit and limit > 0 then
        redis.call('ZADD', key, now_ms, request_id .. ':' .. tostring(i))
        -- 하드 상한(deepdive Q50): score 트림(Phase1)에 더해 멤버 수를 limit 으로 캡.
        -- 정상 흐름(Phase2 는 ZCARD<limit 일 때만 실행)에선 트리거 안 되나, 시계
        -- 스큐·동시성 레이스·stale 멤버로 인한 비정상 증가를 막아 ZCARD/ZRANGE 비용을
        -- limit 에 독립적으로 bound. 가장 오래된(낮은 rank) 초과분 제거, 최신 limit 유지.
        redis.call('ZREMRANGEBYRANK', key, 0, -(limit + 1))
        -- TTL: window + 60초 버퍼
        redis.call('EXPIRE', key, math.ceil(window_ms / 1000) + 60)
    end
end

return cjson.encode({
    allowed      = true,
    scope        = cjson.null,
    limit_type   = cjson.null,
    limit        = -1,
    remaining    = -1,
    retry_after  = cjson.null,
    window_reset = cjson.null
})
