-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Deduct — 실제 비용 원자적 차감 + 임계값 교차 체크
-- BR-BDG-05, BR-BDG-06
--
-- KEYS[1] = budget:user:{<user_id>}:<period>   -- hash tag on user_id
-- KEYS[2] = budget:config:user:{<user_id>}     -- same hash tag → same slot
-- ARGV[1] = cost (USD, string decimal)
--
-- Returns: JSON {new_used, remaining, limit, threshold_triggered, crossed, app_clients}
--   crossed              = 이 요청이 넘은 **모든** 임계값(오름차순). 없으면 빈 배열이 아니라
--                          cjson 제약 때문에 키가 생략된다(아래 주석 참조).
--   threshold_triggered  = crossed 의 **가장 높은** 값(구버전 소비자 호환).

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
--
-- ⚠️ **넘은 임계값을 전부 모은다.** 예전에는 첫 번째로 맞는 것에서 `break` 했고,
--    thresholds 는 오름차순({80,90,100})이었다. 그래서 한 요청이 70% → 105% 로 뛰면
--    80/90/100 을 모두 넘었는데 **80% 알림만** 나갔다 — 예산이 소진됐다는 사실
--    (100%)은 아무에게도 전달되지 않았다. 큰 요청 한 건이면 재현되고, 알림의 부재는
--    "예산을 안 썼다" 와 구별되지 않아 알아챌 방법이 없다.
--
--    정렬을 신뢰하지 않고 모두 검사한 뒤 오름차순으로 정렬한다 — 운영자가 임계값을
--    임의 순서로 저장할 수 있다(admin-api 는 정렬해서 넣지만 Lua 가 그것에 의존하면
--    한쪽 변경이 조용히 다른 쪽을 깨뜨린다).
local crossed = {}
if limit > 0 then
    local old_pct = (used / limit) * 100
    local new_pct = (new_used / limit) * 100
    for _, t in ipairs(thresholds) do
        local pct = tonumber(t)
        if pct and old_pct < pct and new_pct >= pct then
            crossed[#crossed + 1] = pct
        end
    end
    table.sort(crossed)
end

-- 구버전 소비자를 위한 단일 값 — 넘은 것 중 **가장 높은** 값을 준다. 낮은 값을 주면
-- "80% 도달" 알림 하나로 소진을 대신하게 되어 원래 결함으로 되돌아간다.
local triggered = cjson.null
if #crossed > 0 then
    triggered = crossed[#crossed]
end

local out = {
    new_used = new_used,
    remaining = limit - new_used,
    limit = limit,
    threshold_triggered = triggered,
    app_clients = app_clients
}
-- ⚠️ cjson 은 **빈 Lua 테이블을 JSON 객체 `{}` 로** 인코딩한다(배열이 아니다). 빈 crossed
--    를 그대로 실으면 파이썬 쪽 `isinstance(x, list)` 가 False 가 되어, 같은 함정으로
--    app_clients 게이트가 조용히 닫힌 전례가 있다. 비어 있으면 키를 아예 넣지 않고,
--    읽는 쪽이 부재를 "교차 없음" 으로 다룬다.
if #crossed > 0 then
    out.crossed = crossed
end

return cjson.encode(out)
