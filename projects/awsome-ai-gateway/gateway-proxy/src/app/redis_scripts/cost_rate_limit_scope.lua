-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Cost Rate Limit — 단일 스코프 CPM/CPH 사전 예약 (USER 또는 TEAM)
-- FR-4.6 / BR-RL-03
--
-- Redis Cluster CROSSSLOT 대응(deepdive Q50): USER/TEAM 키는 hash tag 가 달라
-- 슬롯이 다르므로 한 eval 에 묶을 수 없다. 이 스크립트는 **한 스코프의 2키만**
-- 받는다(cpm/cph, 동일 {scope_id} hash tag = 단일 슬롯). 호출자가 scope 별로
-- 1회씩 호출하고 결과를 합친다(rate_limit_service.reserve_cost). 과거의
-- 4키(user+team) 단일 eval(cost_rate_limit.lua)은 cluster 에서 CROSSSLOT →
-- fail-open 으로 CPM/CPH 무음 미집행됐다.
--
-- KEYS[1] = rl:cost:<scope>:{<scope_id>}:cpm:<window_ts>
-- KEYS[2] = rl:cost:<scope>:{<scope_id>}:cph:<window_ts>
--
-- ARGV[1] = estimated_cost (USD, string decimal)
-- ARGV[2] = cpm_limit  (0 또는 음수 = unlimited)
-- ARGV[3] = cph_limit
-- ARGV[4] = scope_label ('USER' | 'TEAM')
--
-- Returns: JSON {allowed, scope, limit_type, limit, remaining, retry_after, reserved_cost}
--   통과 시 카운터(INCRBYFLOAT)까지 커밋하고 reserved_cost 반환.

local cost        = tonumber(ARGV[1])
local cpm_limit   = tonumber(ARGV[2])
local cph_limit   = tonumber(ARGV[3])
local scope_label = ARGV[4]

local cpm_used = tonumber(redis.call('GET', KEYS[1]) or '0')
local cph_used = tonumber(redis.call('GET', KEYS[2]) or '0')

local cpm_after = cpm_used + cost
local cph_after = cph_used + cost

if cpm_limit and cpm_limit > 0 and cpm_after > cpm_limit then
    return cjson.encode({
        allowed = false,
        scope = scope_label,
        limit_type = "cpm",
        limit = cpm_limit,
        remaining = math.max(0, cpm_limit - cpm_used),
        retry_after = 60,
        reserved_cost = 0,
    })
end
if cph_limit and cph_limit > 0 and cph_after > cph_limit then
    return cjson.encode({
        allowed = false,
        scope = scope_label,
        limit_type = "cph",
        limit = cph_limit,
        remaining = math.max(0, cph_limit - cph_used),
        retry_after = 3600,
        reserved_cost = 0,
    })
end

-- 통과 — 예약 커밋 (INCRBYFLOAT + EXPIRE)
if cost > 0 then
    redis.call('INCRBYFLOAT', KEYS[1], cost)
    redis.call('EXPIRE', KEYS[1], 120)
    redis.call('INCRBYFLOAT', KEYS[2], cost)
    redis.call('EXPIRE', KEYS[2], 7200)
end

return cjson.encode({
    allowed = true,
    reserved_cost = cost,
})
