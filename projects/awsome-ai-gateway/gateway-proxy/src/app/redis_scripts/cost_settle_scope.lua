-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Cost Rate Limit — 단일 스코프 CPM/CPH 사후 정산 (USER 또는 TEAM)
--
-- 예약(cost_rate_limit_scope.lua)이 **실제로 쓴** 두 키를 받는다. 호출자(rate_limit_service.settle_cost)
-- 는 예약할 때 기록해 둔 키 이름을 그대로 넘긴다 — 키 이름에 스코프와 창(window_ts)이 들어 있으므로
-- 환불은 정산 시각으로 창을 다시 계산하지 않는다. 예약하지 않은 스코프는 호출 자체가 없다.
-- 같은 스코프의 **지금** 창 키(KEYS[4], KEYS[5])도 받는다 — 예약한 창이 지난 뒤의 추가 청구가 간다.
--
-- Redis Cluster CROSSSLOT(deepdive Q50): 다섯 키는 모두 같은 hash tag({scope_id}) = 같은 슬롯이다.
-- USER 와 TEAM 은 tag 가 달라 한 EVAL 에 섞을 수 없으므로 호출자가 스코프마다 한 번씩 부른다.
--
-- KEYS[1] = rl:cost:<scope>:{<scope_id>}:cpm:<예약 때의 window_ts>
-- KEYS[2] = rl:cost:<scope>:{<scope_id>}:cph:<예약 때의 window_ts>
-- KEYS[3] = rl:cost-settled:<scope>:{<scope_id>}:<예약 id>   (정산 표지, 같은 hash tag)
-- KEYS[4] = rl:cost:<scope>:{<scope_id>}:cpm:<정산 때의 window_ts>  (예약한 창이 아직이면 KEYS[1] 과 같다)
-- KEYS[5] = rl:cost:<scope>:{<scope_id>}:cph:<정산 때의 window_ts>  (같은 hash tag)
-- ARGV[1] = adjustment (USD, actual - reserved. 음수 = 환불)
-- ARGV[2] = 표지 TTL (초)
--
-- 한 예약은 **한 번만** 정산된다. 표지를 SET NX 로 먼저 세우고, 이미 있으면(같은 예약을 두 번째로 정산 —
-- 응답을 못 받은 명령을 클라이언트가 재시도했거나, 정산 상태의 사본으로 다시 부른 경우) 아무것도 쓰지
-- 않는다. 확인과 표지와 조정이 한 EVAL 이라 사이에 끼어들 틈이 없다.
--
-- 키마다(cpm, cph 따로):
--   * 추가 청구(adjustment > 0)인데 예약한 창이 이미 지났으면(KEYS[i] ~= KEYS[i+3]) 지금 창 키에 더한다.
--     예약보다 더 쓴 비용은 응답이 끝난 지금의 사용량이고, 지난 창의 키에 더하면 어떤 검사도 읽지
--     않아 한도에서 사라진다. 없으면 만들고, TTL 이 없으면 예약과 같은 TTL 을 건다.
--   * 그 밖(환불, 같은 창의 추가 청구)은 예약한 창의 키에서 한다 — 환불은 차감한 그 창에 돌려준다.
--   * 키가 없으면 **아무것도 쓰지 않는다**(키를 만들지 않는다). 예약한 키가 없어지는 경우는 TTL 만료
--     — 예약이 걸 때마다 TTL 을 창 길이의 두 배(cpm 120s / cph 7200s)로 다시 걸므로, 만료됐다면 그
--     창은 이미 끝났고 어떤 검사도 그 키를 다시 읽지 않는다 — 또는 축출·유실이다. 어느 쪽이든 그 창의
--     카운터에 차액만 다시 쓰는 것은 의미 없는 부분값이고, 만들면 TTL 을 지어내야 한다.
--   * 있으면 INCRBYFLOAT. 기존 TTL 은 그대로 남는다. TTL 이 없으면(-1: 이 수정 이전의 정산이 만든 키가
--     예약 창과 겹친 경우) 예약과 같은 TTL 을 건다 — 정산이 TTL 없는 키를 남기지 않는다.
--   * 결과가 음수면 0 으로 둔다(TTL 유지, KEEPTTL 은 Redis 6.0+/Valkey). 한 창의 비용 합계는 음수일 수
--     없다 — 음수는 부동소수 잔차나 축출된 예약을 환불한 결과이고, 남겨 두면 그 창 동안 남의 한도를
--     빌려 준다.
--
-- 증분은 예약과 같은 경로(tonumber → redis.call 의 숫자 인자)로 넘긴다. 예약액 전액을 환불하면 두 증분이
-- 같은 문자열로 직렬화돼 카운터가 정확히 원래 값으로 돌아간다.
--
-- Returns: {cpm_applied, cph_applied, clamped, duplicate}
--   applied 1 = 조정함, 0 = 키 없음 → 건너뜀. clamped 1 = 어느 키가 음수가 될 뻔해 0 으로 뒀다(호출자가
--   로그·카운트한다 — 조용히 넘기지 않는다). duplicate 1 = 이미 정산된 예약이라 아무것도 하지 않았다.

if not redis.call('SET', KEYS[3], '1', 'NX', 'EX', tonumber(ARGV[2])) then
    return {0, 0, 0, 1}
end

local adj = tonumber(ARGV[1])
local ttls = {120, 7200}
local applied = {0, 0}
local clamped = 0

for i = 1, 2 do
    local target = nil
    local now_key = KEYS[i + 3]
    if adj > 0 and now_key and now_key ~= KEYS[i] then
        target = now_key  -- 예약한 창이 지난 추가 청구 → 지금 창(없으면 만든다)
    elseif redis.call('EXISTS', KEYS[i]) == 1 then
        target = KEYS[i]
    end
    if target then
        local v = tonumber(redis.call('INCRBYFLOAT', target, adj))
        if v < 0 then
            redis.call('SET', target, '0', 'KEEPTTL')
            clamped = 1
        end
        if redis.call('TTL', target) < 0 then
            redis.call('EXPIRE', target, ttls[i])
        end
        applied[i] = 1
    end
end

return {applied[1], applied[2], clamped, 0}
