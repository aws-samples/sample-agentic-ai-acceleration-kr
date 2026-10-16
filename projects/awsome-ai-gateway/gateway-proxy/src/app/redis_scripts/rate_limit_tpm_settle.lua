-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- TPM 사후 정산 — 단일 스코프 (USER, TEAM 또는 GLOBAL)
--
-- 예약(rate_limit_tpm_check.lua)이 토큰을 넣은 **그 버킷**에만 차액을 반영한다. 호출자
-- (rate_limit_service.settle_tpm)는 예약할 때 기록해 둔 세 키와 버킷 번호를 그대로 넘긴다.
-- 예약하지 않은 스코프는 호출 자체가 없다.
--
-- Redis Cluster CROSSSLOT(deepdive Q50): 세 키는 같은 hash tag({scope:id:model}) = 같은 슬롯이다.
-- 다른 스코프의 키와 한 EVAL 에 섞지 않는다 — 호출자가 스코프마다 한 번씩 부른다.
--
-- KEYS[1] = {scope:id:model}:tpm:cur      (현재 버킷 토큰 합)
-- KEYS[2] = {scope:id:model}:tpm:prev     (직전 버킷 토큰 합)
-- KEYS[3] = {scope:id:model}:tpm:window   (cur 가 담고 있는 버킷 번호)
-- KEYS[4] = {scope:id:model}:tpm-settled:<예약 id>   (정산 표지, 같은 hash tag)
-- ARGV[1] = adjustment (tokens, actual - reserved. 음수 = 환불)
-- ARGV[2] = 예약한 버킷 번호 (floor(now / window_sec), 예약 시각)
-- ARGV[3] = TTL (rate_limit_tpm_check.lua 의 window_sec * 2 + 60)
-- ARGV[4] = 표지 TTL (초)
-- ARGV[5] = 지금 버킷 번호 (floor(now / window_sec), 정산 시각)
--
-- 한 예약은 **한 번만** 정산된다 — 표지를 SET NX 로 먼저 세우고, 이미 있으면 아무것도 쓰지 않는다
-- (cost_settle_scope.lua 와 같다: 응답을 못 받은 명령의 재시도, 정산 상태 사본으로 다시 부른 경우).
--
-- 버킷은 분마다 회전한다(cur → prev). 그래서 예약한 버킷이 지금 어디에 있는지를 window 마커로 본다:
--   * 마커 == 예약 버킷     → 아직 cur 다. cur 를 조정한다.
--   * 마커 == 예약 버킷 + 1 → 한 번 회전해 prev 로 옮겨졌다. prev 를 조정한다(가중 추정에 아직 들어간다).
--   * 그 밖(마커 없음·두 번 이상 회전·키 만료) → 예약한 버킷은 어떤 추정에도 더는 들어가지 않는다.
--     아무것도 쓰지 않는다. 지금의 cur 를 조정하면 다른 분의 카운터를 움직이는 것이다.
-- 조정할 키가 없으면 만들지 않는다. 있으면 INCRBY 하고 TTL 은 그대로 둔다(없으면 ARGV[3] 으로 건다 —
-- 이 수정 이전 코드가 남긴 키). 결과가 음수면 0 으로 둔다(TTL 유지): 버킷의 토큰 합은 음수일 수 없고,
-- 음수는 그 분 동안 남의 한도를 빌려 준다.
--
-- 단, 추가분(adjustment > 0 — 예약보다 더 쓴 토큰)이고 예약한 버킷이 지금 버킷이 아니면 위 규칙 대신
-- **지금 버킷(cur)** 에 더한다. 그 토큰은 응답이 끝난 지금 쓴 것이고, prev 에 더하면 가중치만큼 줄어
-- 들며 두 번 이상 회전했으면 아무 데도 남지 않는다. cur 가 아직 지난 버킷이면(그 뒤 검사가 없었다)
-- rate_limit_tpm_check.lua 와 똑같이 회전부터 한다(window 마커가 지금보다 앞일 때만 — 다른 파드의
-- 시계가 앞서 이미 회전한 경우는 그대로 둔다). 키가 없으면 만들고 세 키 모두 TTL 을 건다. 환불은
-- 예약한 그 버킷에서만 한다(위 규칙).
--
-- Returns: {applied, clamped, duplicate}
--   applied 1 = 조정함, 0 = 예약한 버킷이 없어 건너뜀. clamped 1 = 음수가 될 뻔해 0 으로 뒀다(호출자가
--   로그·카운트한다). duplicate 1 = 이미 정산된 예약이라 아무것도 하지 않았다.

if not redis.call('SET', KEYS[4], '1', 'NX', 'EX', tonumber(ARGV[4])) then
    return {0, 0, 1}
end

local adj = tonumber(ARGV[1])
local bucket = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local current = tonumber(ARGV[5])
local marker = tonumber(redis.call('GET', KEYS[3]) or '-1')

if adj > 0 and current and bucket ~= current then
    if marker < current then
        local cur_val = tonumber(redis.call('GET', KEYS[1]) or '0')
        if marker == current - 1 then
            redis.call('SET', KEYS[2], cur_val)
        else
            redis.call('SET', KEYS[2], 0)
        end
        redis.call('SET', KEYS[1], 0)
        redis.call('SET', KEYS[3], current)
        redis.call('EXPIRE', KEYS[2], ttl)
        redis.call('EXPIRE', KEYS[3], ttl)
    end
    redis.call('INCRBY', KEYS[1], adj)
    if redis.call('TTL', KEYS[1]) < 0 then
        redis.call('EXPIRE', KEYS[1], ttl)
    end
    return {1, 0, 0}
end

local target
if marker == bucket then
    target = KEYS[1]
elseif marker == bucket + 1 then
    target = KEYS[2]
else
    return {0, 0, 0}
end

if redis.call('EXISTS', target) == 0 then
    return {0, 0, 0}
end
local clamped = 0
local v = redis.call('INCRBY', target, adj)
if v < 0 then
    redis.call('SET', target, '0', 'KEEPTTL')
    clamped = 1
end
if redis.call('TTL', target) < 0 then
    redis.call('EXPIRE', target, ttl)
end
return {1, clamped, 0}
