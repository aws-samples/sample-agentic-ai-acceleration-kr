-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Cost settle — 한 스코프(USER 또는 TEAM)의 CPM/CPH 예약 차액 정산
--
-- 왜 Lua 인가, 그리고 왜 EXISTS 가드가 있는가
-- -------------------------------------------
-- 정산은 예약과 실제 비용의 **차액**을 카운터에 더한다. 예약은 max_tokens 기준 과대
-- 추정이므로 이 차액은 거의 항상 **음수**(=환불)다.
--
-- 예전에는 파이프라인으로 그냥 ``INCRBYFLOAT`` 했다. Redis 의 INCRBYFLOAT 는 키가 없으면
-- **키를 만든다**. 그래서 두 가지가 동시에 일어났다:
--
--   1. 예약했던 창이 이미 지났으면(스트리밍이 분 경계를 넘으면 매번 그렇다) 환불이
--      **다음 창**에 음수로 얹혔다. 그 요청이 소비한 적 없는 창에 헤드룸을 만들어 준다.
--   2. 그렇게 만들어진 키에는 **TTL 이 없었다**. ``rl:cost:user:{uid}:cpm:<ts>`` 는
--      사용자·분 단위 키라서, 경계를 넘는 요청마다 불멸의 키가 하나씩 쌓였다.
--
-- 그래서 이 스크립트는 **이미 존재하는 키만** 조정한다. 창이 사라졌다면 조정할 대상이
-- 없는 것이 맞다 — 그 창의 카운터는 이미 만료됐고, 예약분도 함께 사라졌다. 없는 키를
-- 되살려 음수를 넣는 것은 정산이 아니라 다른 창에 대한 할인이다.
--
-- TTL 은 매번 다시 건다. 예약 시점의 TTL(창 길이의 2배 = 유예)과 같은 기준을 쓰되 남은
-- 시간으로 계산하므로 창을 넘겨 연장되지 않고, 과거의 결함으로 만들어진 TTL 없는 키도
-- 이 경로를 한 번 타면 회수된다.
--
-- ⚠️ 키 2개는 **같은 스코프**의 cpm/cph 여야 한다(동일 hash tag = 단일 슬롯).
--    USER/TEAM 을 한 번에 넘기면 Redis Cluster 에서 CROSSSLOT 이다 —
--    cost_rate_limit_scope.lua 와 같은 이유로 스코프별 1회 호출한다.
--
-- KEYS[1] = rl:cost:<scope>:{<scope_id>}:cpm:<window_ts>
-- KEYS[2] = rl:cost:<scope>:{<scope_id>}:cph:<window_ts>
--
-- ARGV[1] = adjustment (USD, 음수 가능)
-- ARGV[2] = cpm 키에 다시 걸 TTL(초). 0 이하면 조정하지 않는다(창이 지났다).
-- ARGV[3] = cph 키에 다시 걸 TTL(초). 0 이하면 조정하지 않는다.
--
-- Returns: 실제로 조정된 키 개수 (0 = 두 창 모두 사라졌거나 차액이 0)

local adj = tonumber(ARGV[1])
if adj == nil or adj == 0 then
    return 0
end

local ttls = { tonumber(ARGV[2]), tonumber(ARGV[3]) }
local applied = 0

for i = 1, 2 do
    local ttl = ttls[i]
    if ttl ~= nil and ttl > 0 and redis.call('EXISTS', KEYS[i]) == 1 then
        redis.call('INCRBYFLOAT', KEYS[i], adj)
        redis.call('EXPIRE', KEYS[i], math.ceil(ttl))
        applied = applied + 1
    end
end

return applied
