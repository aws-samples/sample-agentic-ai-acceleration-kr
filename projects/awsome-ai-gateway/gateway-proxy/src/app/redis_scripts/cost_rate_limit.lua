-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Cost Rate Limit — CPM/CPH 사전 예약 (USER + TEAM 2 스코프)
-- FR-4.6 / BR-RL-03
--
-- KEYS[1] = rl:cost:user:{<user_id>}:cpm:<window_ts>
-- KEYS[2] = rl:cost:user:{<user_id>}:cph:<window_ts>
-- KEYS[3] = rl:cost:team:{<team_id>}:cpm:<window_ts>  (없으면 빈 문자열 슬롯 — team_id=nil)
-- KEYS[4] = rl:cost:team:{<team_id>}:cph:<window_ts>
--
-- ARGV[1] = estimated_cost (USD, string decimal)
-- ARGV[2] = user_cpm_limit  (0 또는 음수 = unlimited)
-- ARGV[3] = user_cph_limit
-- ARGV[4] = team_cpm_limit
-- ARGV[5] = team_cph_limit
-- ARGV[6] = has_team   ("1" = team 키 존재, "0" = team 스킵)
--
-- Returns: JSON {allowed, scope, limit_type, limit, remaining, retry_after, reserved_cost}

local cost = tonumber(ARGV[1])
local user_cpm_limit = tonumber(ARGV[2])
local user_cph_limit = tonumber(ARGV[3])
local team_cpm_limit = tonumber(ARGV[4])
local team_cph_limit = tonumber(ARGV[5])
local has_team = ARGV[6] == "1"

local function check_scope(cpm_key, cph_key, cpm_limit, cph_limit, scope_label)
    local cpm_used = tonumber(redis.call('GET', cpm_key) or '0')
    local cph_used = tonumber(redis.call('GET', cph_key) or '0')

    local cpm_after = cpm_used + cost
    local cph_after = cph_used + cost

    if cpm_limit and cpm_limit > 0 and cpm_after > cpm_limit then
        return {
            allowed = false,
            scope = scope_label,
            limit_type = "cpm",
            limit = cpm_limit,
            remaining = math.max(0, cpm_limit - cpm_used),
            retry_after = 60,
        }
    end
    if cph_limit and cph_limit > 0 and cph_after > cph_limit then
        return {
            allowed = false,
            scope = scope_label,
            limit_type = "cph",
            limit = cph_limit,
            remaining = math.max(0, cph_limit - cph_used),
            retry_after = 3600,
        }
    end
    return { allowed = true }
end

-- 1. 모든 스코프 체크 (예약 전)
local user_check = check_scope(KEYS[1], KEYS[2], user_cpm_limit, user_cph_limit, "USER")
if not user_check.allowed then
    user_check.reserved_cost = 0
    return cjson.encode(user_check)
end

if has_team then
    local team_check = check_scope(KEYS[3], KEYS[4], team_cpm_limit, team_cph_limit, "TEAM")
    if not team_check.allowed then
        team_check.reserved_cost = 0
        return cjson.encode(team_check)
    end
end

-- 2. 모든 스코프 통과 — 예약 커밋 (INCRBYFLOAT + EXPIRE)
if cost > 0 then
    redis.call('INCRBYFLOAT', KEYS[1], cost)
    redis.call('EXPIRE', KEYS[1], 120)
    redis.call('INCRBYFLOAT', KEYS[2], cost)
    redis.call('EXPIRE', KEYS[2], 7200)
    if has_team then
        redis.call('INCRBYFLOAT', KEYS[3], cost)
        redis.call('EXPIRE', KEYS[3], 120)
        redis.call('INCRBYFLOAT', KEYS[4], cost)
        redis.call('EXPIRE', KEYS[4], 7200)
    end
end

return cjson.encode({
    allowed = true,
    reserved_cost = cost,
})
