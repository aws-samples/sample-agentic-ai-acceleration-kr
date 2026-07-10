-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Deduct — 실제 비용 원자적 차감 + 임계값 교차 체크
-- BR-BDG-05, BR-BDG-06
--
-- KEYS[1] = budget:user:{<user_id>}:<period>   -- hash tag on user_id
-- KEYS[2] = budget:config:user:{<user_id>}     -- same hash tag → same slot
-- ARGV[1] = cost (USD, string decimal)
--
-- Returns: JSON {new_used, remaining, threshold_triggered}

local usage_key = KEYS[1]
local config_key = KEYS[2]
local cost = tonumber(ARGV[1])

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

return cjson.encode({
    new_used = new_used,
    remaining = limit - new_used,
    threshold_triggered = triggered,
    app_clients = app_clients
})
