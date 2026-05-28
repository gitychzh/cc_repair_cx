# Codex Framework Pipeline Optimization Report

**Date**: 2026-05-28
**Scope**: codex → proxy(40002) → LiteLLM(41001) → ModelScope API → glm5.1/dsv4p

## Architecture Overview

```
Claude Code (codex)
  ↓ Anthropic /v1/messages format
  ↓ ANTHROPIC_BASE_URL=http://127.0.0.1:40001 (or 40002)
Proxy (auth_to_api_40001/40002)
  ↓ Converts Anthropic → OpenAI format
  ↓ Tool description truncation (MAX_TOOL_DESC=2000, MAX_SCHEMA_DESC=600)
  ↓ Proxy-level retry (PROXY_MAX_RETRIES=2)
  ↓ Input token pre-check (safety limit)
LiteLLM glm5.1_uni41001 (or local glm5.1 config)
  ↓ OpenAI /v1/chat/completions format
  ↓ Multi-key routing (7 keys, simple-shuffle)
  ↓ LiteLLM-level retry (num_retries=3)
ModelScope API (api-inference.modelscope.cn/v1)
  ↓ zhipuai/glm-5.1 (7 keys × 200 RPM each)
  ↓ deepseek-ai/deepseek-v4-pro (7 keys × 200 RPM each)
```

## Error Analysis (2026-05-28, 392 requests)

| Category | Count | % | Root Cause |
|----------|-------|---|------------|
| 200 OK | 380 | 96.9% | — |
| 429 insufficient_quota | 11 | 2.8% | TPS/TPM throttle (ModelScope per-key 200 RPM limit) |
| 500 InternalServerError | 1 | 0.3% | Unknown |

### Detailed Error Breakdown

**429 Rate Limit Errors (46 in error_detail log, 11 final after retries):**
- All 429 errors are `insufficient_quota` type with `code: "insufficient_quota"`
- Error message: "You exceeded your current quota, please check your plan and billing details"
- Per阿里云documentation: this is TPS/TPM throttle, NOT real quota exhaustion
- ModelScope rate limit headers show: 200 model RPM, 2000 total RPM per key
- Key evidence: `x-litellm-key-spend: 0.0` on all 429 responses (spend=$0)
- LiteLLM retries 3 times per request, then proxy retries 1 time = up to 4 attempts

**null-choices Errors (8 total, 7 retried successfully):**
- ModelScope returns `choices: null` (or `choices: []`)
- LiteLLM assertion `response_object["choices"] is not None` fails
- Classified as InternalServerError by LiteLLM, NOT retried by LiteLLM
- Proxy correctly detects and retries these (7 of 8 retried successfully)
- Root cause: ModelScope occasionally returns empty response for large input requests

**ConnectionRefused Errors (53 on 2026-05-27, 0 on 2026-05-28):**
- Caused by container restarts/rebuilds on May 27
- Proxy correctly retries these with 2-second delay
- No longer an issue — containers running stable

### Proxy Retry Statistics (2026-05-28)

| Retry Reason | Count | Success After Retry |
|--------------|-------|---------------------|
| 429-rate-limit | 35 | Most succeeded on retry |
| null-choices | 7 | Most succeeded on retry |

## Token Usage Analysis

- Average input tokens per successful request: 45,986
- Average output tokens per successful request: 223
- Total input tokens (successful): 17,474,778
- Total output tokens (successful): 84,881
- Top request: 96,703 input tokens (182 messages, 26 tools)
- Average duration: 13,089ms, p50: 10,027ms, max: 129,446ms

## Configuration Issues Found (Before Optimization)

### 1. Mixed-Case Model Names (CRITICAL)

**Before**: glm5.1 config had 49 deployments:
- 7 × `zhipuai/glm-5.1` (lowercase, Tier-1) — WORKS
- 7 × `zhipuAI/gLM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhipuAI/GlM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZHipuAI/GlM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhIpuAI/GLm-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhiPuAI/gLM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhipUAI/GlM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhipuaI/GLm-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhipuAi/GLM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhipuAI/gLM-5.1` (mixed case) — BROKEN on ModelScope
- 7 × `ZhipuAI/GLm-5.1` (mixed case) — BROKEN on ModelScope

**Impact**: 42 broken deployments waste cooldown slots and retry attempts. When LiteLLM routes to a broken deployment, it gets "Unsupported model" error, wastes a retry, and puts that deployment in cooldown. With cooldown_time=60s, this effectively reduces the working deployment pool.

**Fix**: Removed all mixed-case deployments. Only lowercase `zhipuai/glm-5.1` and `deepseek-ai/deepseek-v4-pro` are kept.

### 2. Over-Conservative RPM (rpm: 1)

**Before**: Each deployment had `rpm: 1`
**Actual ModelScope limit**: 200 RPM per key (verified via rate limit headers)
**Impact**: LiteLLM considers each deployment as having 1 RPM capacity, which causes it to:
  - Put deployments in cooldown after just 1 rate limit error
  - Avoid routing to deployments it thinks are "saturated"
  - Over-concentrate remaining requests on "available" deployments

**Fix**: Changed `rpm: 1` → `rpm: 10` for glm5.1, `rpm: 5` for dsv4p

### 3. latency-based-routing Causes TPS Throttle

**Before**: `routing_strategy: latency-based-routing` with `lowest_latency_buffer: 0.1`
**Impact**: Over-concentrates requests on the deployment with lowest latency, causing that key's TPS/TPM to hit the 200 RPM limit faster. This creates a feedback loop:
  1. Key A is fast → most requests go to Key A
  2. Key A hits 200 RPM limit → 429 insufficient_quota
  3. Key A goes into cooldown → requests shift to Key B
  4. Key B hits 200 RPM → repeat

**Fix**: Changed `routing_strategy: simple-shuffle` — distributes load evenly across all 7 keys, preventing any single key from hitting its RPM limit prematurely.

### 4. Over-Aggressive Cooldown (cooldown_time: 60)

**Before**: `cooldown_time: 60` seconds
**Impact**: After a 429 error, the deployment is unavailable for 60 seconds. With 7 deployments, losing 1-2 for 60s reduces capacity by 14-28%. Since the 429 is actually TPS/TPM throttle (resets per-minute), 60s cooldown is longer than needed.

**Fix**: Changed `cooldown_time: 20` seconds — deployment recovers faster, matching the per-minute RPM reset window.

### 5. RateLimitErrorAllowedFails: 0 (glm5.1 local config)

**Before**: `RateLimitErrorAllowedFails: 0` in glm5.1 config (1 in dsv4p)
**Impact**: With 0 allowed fails, a single 429 error immediately puts the deployment in cooldown. This is too aggressive for TPS/TPM throttle which is transient.

**Fix**: Changed `RateLimitErrorAllowedFails: 3` — allows 3 rate limit errors before cooldown, giving temporary throttle spikes a chance to resolve naturally.

### 6. Excessive Retries (num_retries: 5)

**Before**: `num_retries: 5` in glm5.1 config (3 in dsv4p)
**Impact**: 5 LiteLLM retries + 2 proxy retries = up to 7 attempts. Each failed retry adds latency (cooldown check, new deployment selection). For TPS/TPM throttle, retries just add delay without solving the root cause.

**Fix**: Changed `num_retries: 3` — sufficient for routing to different keys, proxy handles the rest.

## Changes Summary

| Parameter | Before (glm5.1 local) | After (Optimized) | Justification |
|-----------|----------------------|-------------------|---------------|
| Deployments | 49 (7+42 broken) | 7 (Tier-1 only) | Mixed-case names don't work on ModelScope |
| rpm | 1 | 10 | ModelScope allows 200 RPM/key; 10 is conservative |
| tpm | (not set) | 500000 | ModelScope TPM limit |
| timeout | 120 | 180 | GLM-5.1 avg response 13s, max 129s observed |
| cooldown_time | 60 | 20 | Faster recovery; TPS/TPM resets per-minute |
| num_retries | 5 | 3 | Sufficient; proxy has own retry layer |
| RateLimitErrorAllowedFails | 0 | 3 | Avoid premature cooldown for transient throttle |
| routing_strategy | latency-based | simple-shuffle | Prevents TPS throttle from over-concentration |
| debug | true | (removed) | Reduces log volume |

## Verification Results (After Optimization)

| Test | Result |
|------|--------|
| Simple text query (40001) | 200 OK, 2615ms |
| Simple text query (40002) | 200 OK, 2246ms |
| Tool calling (Bash) | 200 OK, tool_use stop_reason |
| Multi-turn tool result | 200 OK, correct tool_result handling |
| 5 concurrent requests | All 200 OK, 0 429 errors |
| Streaming request | 200 OK, correct SSE conversion |

## Remaining Issues and Next Steps

1. **Monitor for 429 errors over longer period**: Need 24+ hours of metrics to confirm reduction
2. **null-choices still occurs occasionally**: 8/392 = 2.0%. Consider adding LiteLLM custom callback to handle `choices:null` by retrying automatically
3. **Large context requests**: Top request had 96K input tokens. Consider context compression or summarization for very long conversations
4. **DSv4P model not actively used**: Currently all requests go to glm5.1. Consider enabling fallback model routing for failover
5. **Proxy 40001 vs 40002**: Two proxy instances run the same code. Could consolidate to one with higher availability
6. **Monitor ModelScope key quota exhaustion**: Free keys may have daily/hourly token limits. Need to track cumulative token spend per key

## How to Continue Optimization

1. SSH to the remote machine: `ssh -p 222 opc2_uname@100.109.57.26`
2. Check metrics: `python3 -c "import json; entries=[json.loads(l) for l in open('/opt/cc-infra/logs/proxy/metrics.DATE.jsonl')]; print(f'total={len(entries)} success={sum(1 for e in entries if e.get(\"status\")==200)}')"`
3. Check errors: `python3 -c "import json; entries=[json.loads(l) for l in open('/opt/cc-infra/logs/proxy/error_detail.DATE.jsonl')]; cats={}; for e in entries: cats[e.get('error_subcategory','unknown')] = cats.get(e.get('error_subcategory','unknown'),0)+1; print(cats)"`
4. Restart containers after config change: `docker restart glm5.1_uni41001 dsv4p_uni42001 auth_to_api_40001 auth_to_api_40002`
5. Test endpoint: `curl -s -X POST "http://127.0.0.1:40001/v1/messages" -H "Content-Type: application/json" -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" -d '{"model":"glm5.1","max_tokens":50,"messages":[{"role":"user","content":"hello"}]}'`