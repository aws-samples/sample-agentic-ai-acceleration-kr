-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Record one call outcome into a per-second sliding window and decide whether to OPEN.
-- KEYS[1]=fail base (cb:{pmid}:fail) KEYS[2]=total base (cb:{pmid}:total) KEYS[3]=open flag (cb:{pmid}:open)
-- ARGV[1]=window_sec ARGV[2]=min_calls ARGV[3]=error_rate ARGV[4]=open_ms ARGV[5]=is_failure(1/0) ARGV[6]=now_ms
-- Returns 1 if circuit is (now) OPEN, else 0.
local window_sec = tonumber(ARGV[1])
local min_calls  = tonumber(ARGV[2])
local rate_thr   = tonumber(ARGV[3])
local open_ms    = tonumber(ARGV[4])
local is_failure = tonumber(ARGV[5])
local now_ms     = tonumber(ARGV[6])
local now_sec    = math.floor(now_ms / 1000)

if redis.call('EXISTS', KEYS[3]) == 1 then
  return 1
end

-- Window is [now_sec-(window_sec-1), now_sec] inclusive; bucket keys are second-scoped so EXPIRE resets are safe (bucket T only ever written during second T).
local total_key = KEYS[2] .. ':' .. now_sec
local fail_key  = KEYS[1] .. ':' .. now_sec
redis.call('INCR', total_key)
redis.call('EXPIRE', total_key, window_sec)
if is_failure == 1 then
  redis.call('INCR', fail_key)
  redis.call('EXPIRE', fail_key, window_sec)
end

local total = 0
local fails = 0
for i = 0, window_sec - 1 do
  local b = now_sec - i
  local t = redis.call('GET', KEYS[2] .. ':' .. b)
  local f = redis.call('GET', KEYS[1] .. ':' .. b)
  if t then total = total + tonumber(t) end
  if f then fails = fails + tonumber(f) end
end

if total >= min_calls and (fails / total) >= rate_thr then
  redis.call('SET', KEYS[3], '1', 'PX', open_ms)
  return 1
end
return 0
