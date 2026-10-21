-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- TPM Sliding Window Counter Check + Pre-Reserve
--
-- 설계 근거:
--   D1: Sliding Window Counter (이전·현재 1분 버킷 + 가중 추정)
--   D2: Pre-reserve 토큰 (요청 진입 시 input + cache_creation + max_output 예약)
--   D3: TPM 공식 — input + cache_creation + output (cache_read 제외)
--   LiteLLM v3 TTL-preservation 패턴 차용.
--
-- Per-scope 3-key 그룹 (동일 hash tag 공유):
--   KEYS[3*i-2] = {scope:id:model}:tpm:cur      (현재 분 버킷 토큰 합)
--   KEYS[3*i-1] = {scope:id:model}:tpm:prev     (이전 분 버킷 토큰 합)
--   KEYS[3*i]   = {scope:id:model}:tpm:window   (현재 분 버킷 타임스탬프 마커)
--
-- ARGV[1]                 = now_sec
-- ARGV[2]                 = reserved_tokens (input + cache_creation + max_output)
-- ARGV[3]                 = window_sec (기본 60)
-- ARGV[4]                 = N (스코프 수)
-- ARGV[5..(4+N)]          = tpm_limit per scope (nil/0 이면 체크 스킵)
-- ARGV[(5+N)..(4+2N)]     = scope names
--
-- 예약 표지(선택, rate_limit_service.check_multi_scope_tpm 은 늘 준다):
--   KEYS[3N+1]            = {scope:id:model}:tpm-reserved:<예약 id>  (같은 hash tag)
--   ARGV[5+2N]            = 표지 TTL (초)
-- 같은 예약(같은 id)의 EVAL 이 다시 오면 — 응답이 늦어 클라이언트(redis-py, redis_retries)가
-- 다시 보낸 것 — 아무것도 쓰지 않고 처음 결과를 그대로 돌려준다. 처음 결과는 표지에 담아 둔다
-- (통과든 거부든). 예전에는 두 번 실행돼 cur 에 예약이 두 번 들어갔고, 정산은 한 번만 되돌리므로
-- 남은 한 번이 버킷이 지날 때까지 한도를 먹었다.
--
-- Returns JSON (rate_limit_check.lua와 동일 shape):
--   통과:  {allowed=true, scope=null, ...}
--   거부:  {allowed=false, scope='USER'|'TEAM'|'GLOBAL', limit_type='tpm',
--          limit=N, remaining=추정잔여, retry_after=초, window_reset=unix초}

local now            = tonumber(ARGV[1])
local reserved       = tonumber(ARGV[2])
local window_sec     = tonumber(ARGV[3]) or 60
local num_scopes     = tonumber(ARGV[4])
local current_bucket = math.floor(now / window_sec)
local elapsed_ratio  = (now % window_sec) / window_sec
local ttl_sec        = window_sec * 2 + 60

local marker = KEYS[3 * num_scopes + 1]
if marker then
    local first = redis.call('GET', marker)
    if first then
        return first
    end
end

local function finish(result)
    if marker then
        redis.call('SET', marker, result, 'EX', tonumber(ARGV[5 + 2 * num_scopes]))
    end
    return result
end

-- Phase 1: 각 스코프 버킷 회전 + 가중 추정치 체크
for i = 1, num_scopes do
    local cur_key  = KEYS[3 * i - 2]
    local prev_key = KEYS[3 * i - 1]
    local win_key  = KEYS[3 * i]
    local limit    = tonumber(ARGV[4 + i])

    if limit and limit > 0 then
        -- 버킷 회전: 저장된 마커가 오래됐으면 cur → prev 이동
        local stored_bucket = tonumber(redis.call('GET', win_key) or '0')
        if stored_bucket ~= current_bucket then
            local cur_val = tonumber(redis.call('GET', cur_key) or '0')
            if stored_bucket == current_bucket - 1 then
                redis.call('SET', prev_key, cur_val)
            else
                -- 2분 이상 공백 → 이전 버킷도 리셋
                redis.call('SET', prev_key, 0)
            end
            redis.call('SET', cur_key, 0)
            redis.call('SET', win_key, current_bucket)
            -- ⚠️ SET 은 TTL 을 지운다. 세 키 모두 여기서 다시 건다 — 예전에는 아래 Phase 2 에서 cur 와
            --    window 만, 그것도 통과할 때만 걸어서 prev 는 처음 만들어질 때부터 TTL 이 없었고, 회전
            --    직후 거부되면 세 키가 모두 TTL 없이 남았다(volatile-lru 가 축출하지 못한다).
            redis.call('EXPIRE', cur_key, ttl_sec)
            redis.call('EXPIRE', prev_key, ttl_sec)
            redis.call('EXPIRE', win_key, ttl_sec)
        end

        local cur_tokens  = tonumber(redis.call('GET', cur_key) or '0')
        local prev_tokens = tonumber(redis.call('GET', prev_key) or '0')

        -- 가중 추정: prev × (1 - elapsed) + cur + reserved
        local estimated = prev_tokens * (1.0 - elapsed_ratio) + cur_tokens + reserved

        if estimated > limit then
            local scope_name = ARGV[4 + num_scopes + i]
            local retry_after = math.ceil(window_sec - (now % window_sec))
            if retry_after < 1 then retry_after = 1 end

            return finish(cjson.encode({
                allowed      = false,
                scope        = scope_name,
                limit_type   = 'tpm',
                limit        = limit,
                remaining    = math.max(0, limit - math.ceil(estimated)),
                retry_after  = retry_after,
                window_reset = (current_bucket + 1) * window_sec
            }))
        end
    end
end

-- Phase 2: 전 스코프 통과 — 예약 토큰 반영
for i = 1, num_scopes do
    local cur_key = KEYS[3 * i - 2]
    local win_key = KEYS[3 * i]
    local limit   = tonumber(ARGV[4 + i])

    if limit and limit > 0 then
        redis.call('INCRBY', cur_key, reserved)
        -- TTL Preservation (LiteLLM v3 패턴): 이미 TTL 있으면 건드리지 않음
        if redis.call('TTL', cur_key) < 0 then
            redis.call('EXPIRE', cur_key, ttl_sec)
        end
        if redis.call('TTL', win_key) < 0 then
            redis.call('EXPIRE', win_key, ttl_sec)
        end
    end
end

return finish(cjson.encode({
    allowed      = true,
    scope        = cjson.null,
    limit_type   = cjson.null,
    limit        = -1,
    remaining    = -1,
    retry_after  = cjson.null,
    window_reset = cjson.null
}))
