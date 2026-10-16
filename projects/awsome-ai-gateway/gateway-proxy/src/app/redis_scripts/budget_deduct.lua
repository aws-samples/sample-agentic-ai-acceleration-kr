-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Deduct — 실제 비용 원자적 차감 + 임계값 교차 체크
-- BR-BDG-05, BR-BDG-06
--
-- KEYS[1] = budget:user:{<user_id>}:<period>   -- hash tag on user_id
-- KEYS[2] = budget:config:user:{<user_id>}     -- same hash tag → same slot
-- KEYS[3] = budget-deducted:user:{<user_id>}:<uuid>  -- 차감 표지(선택), same hash tag
-- ARGV[1] = cost (USD, string decimal)
-- ARGV[2] = 표지 TTL (초, KEYS[3] 이 있을 때)
--
-- 차감 표지: 호출자(cost_recorder._deduct_budget)가 EVAL 마다 새 uuid 로 만든다. 같은 표지의
-- EVAL 이 다시 오면 — 응답이 늦어 클라이언트(redis-py, redis_retries)가 다시 보낸 것 — 아무것도
-- 쓰지 않고 처음 결과를 그대로 돌려준다(처음 결과는 표지에 담아 둔다). 예전에는 두 번 실행돼 그
-- 요청의 비용이 월 예산 카운터에 두 번 들어갔다 — DB 원장은 맞는데 집행만 예산을 일찍 소진했다.
--
-- Returns: JSON {new_used, remaining, threshold_triggered}

local usage_key = KEYS[1]
local config_key = KEYS[2]
local cost = tonumber(ARGV[1])
local marker = KEYS[3]
if marker then
    local first = redis.call('GET', marker)
    if first then
        return first
    end
end

local config_raw = redis.call('GET', config_key)
local limit = 0
local thresholds = {80, 90, 100}
local app_clients = {}
if config_raw then
    local config = cjson.decode(config_raw)
    limit = tonumber(config.limit_usd) or 0
    if config.thresholds then
        thresholds = config.thresholds
    end
    if config.app_clients then
        app_clients = config.app_clients
    end
end

local used = tonumber(redis.call('GET', usage_key) or '0')
local new_used = used + cost

-- 차감 실행
redis.call('INCRBYFLOAT', usage_key, cost)

-- 임계값 교차 체크
local triggered = cjson.null
if limit > 0 then
    local old_pct = (used / limit) * 100
    local new_pct = (new_used / limit) * 100
    for _, t in ipairs(thresholds) do
        if old_pct < t and new_pct >= t then
            triggered = t
            break
        end
    end
end

local result = cjson.encode({
    new_used = new_used,
    remaining = limit - new_used,
    threshold_triggered = triggered,
    app_clients = app_clients
})
if marker then
    redis.call('SET', marker, result, 'EX', tonumber(ARGV[2]))
end
return result
