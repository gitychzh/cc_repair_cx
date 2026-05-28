# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

This repo optimizes the codex (Claude Code) pipeline running on a remote machine, making tool calls more stable and reducing 429/null-choices errors. The pipeline: `codex → proxy(40002) → LiteLLM(41001) → ModelScope API → glm5.1/dsv4p`.

## Remote Machine Access

```bash
ssh -p 222 opc2_uname@100.109.57.26
```

All infrastructure runs on this machine. Config files are at `/opt/cc-infra/`.

## Architecture

- **proxy.py** (auth_to_api_40001/40002): Anthropic→OpenAI converter with tool truncation, proxy-level retry, and input token safety check. Runs on ports 40001/40002.
- **glm5.1_uni41001**: LiteLLM instance for GLM-5.1, 11 variants × 7 keys = 77 deployments, port 41001. Config: `/opt/cc-infra/litellm-glm51/config.yaml`
- **dsv4p_uni42001**: LiteLLM instance for DSv4P, 11 variants × 7 keys = 77 deployments, port 42001. Config: `/opt/cc-infra/litellm-dsv4p/config.yaml`
- **cc_postgres**: PostgreSQL for LiteLLM spend tracking, port 5432

## Key Commands

```bash
# Restart all containers after config change
docker restart glm5.1_uni41001 dsv4p_uni42001 auth_to_api_40001 auth_to_api_40002

# Quick health check
docker ps --format "{{.Names}} {{.Status}}" | grep -E "litellm|proxy|postgres"

# Test proxy endpoint
curl -s -X POST "http://127.0.0.1:40001/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-litellm-local" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1","max_tokens":50,"messages":[{"role":"user","content":"hello"}]}'

# Analyze today's metrics
python3 -c "
import json
e=[json.loads(l) for l in open('/opt/cc-infra/logs/proxy/metrics.DATE.jsonl')]
total=len(e); ok=sum(1 for x in e if x.get('status')==200)
print(f'total={total} success={ok} pct={round(ok/total*100,1)}')
"

# Analyze today's errors
python3 -c "
import json
e=[json.loads(l) for l in open('/opt/cc-infra/logs/proxy/error_detail.DATE.jsonl')]
cats={}
for x in e: cats[x.get('error_subcategory','?')]=cats.get(x.get('error_subcategory','?'),0)+1
for k,v in sorted(cats.items(),key=lambda x:-x[1]): print(f'{k}: {v}')
"

# Test a model variant against ModelScope API (with rate limit headers)
curl -s -D- --max-time 15 -X POST "https://api-inference.modelscope.cn/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_KEY" \
  -d '{"model":"VARIANT_NAME","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```

## Critical Constraints

- **ALL 11 model variants work correctly**: Every mixed-case variant (`zhipuAI/gLM-5.1`, `ZhipuAI/GlM-5.1`, etc.) returns 200 OK on ModelScope API. They all route to the same backend model `ZhipuAI/GLM-5.1`. **Never remove variants without testing each individually.**
- **Each variant has INDEPENDENT 200 RPM quota**: Verified via `Modelscope-Ratelimit-Model-Requests-Remaining` headers. 11 variants × 7 keys × 200 RPM = 15,400 RPM/min theoretical (capped at 7 × 2000 = 14,000 total RPM). **Removing variants reduces total RPM capacity.**
- **EXCLUDED variant `ZhipuAI/GLm-5.1`**: Only ~50 RPM quota, returns `choices: null`. Do NOT add this variant.
- **rpm must remain = 1**: User requirement. LiteLLM's rpm parameter controls per-deployment rate limiting, not the actual ModelScope quota.
- **429 errors are TPS/TPM throttle** (insufficient_quota), NOT real quota exhaustion. They reset per-minute. Use `cooldown_time: 20` and `RateLimitErrorAllowedFails: 3` to recover quickly.
- **null-choices errors**: ModelScope occasionally returns `choices: null`. Proxy handles retry, but LiteLLM treats as InternalServerError and won't retry internally.
- **GLM-5.1 max input**: 202,745 tokens. Safety limit set to 190,000 in proxy.
- **All changes must be backed by log data or documentation** — never change parameters based on guessing. Test each model variant individually before adding/removing.

## Config File Locations on Remote Machine

- Proxy: `/opt/cc-infra/proxy/proxy.py`
- GLM-5.1 LiteLLM: `/opt/cc-infra/litellm-glm51/config.yaml` (77 deployments, 11 variants × 7 keys)
- DSv4P LiteLLM: `/opt/cc-infra/litellm-dsv4p/config.yaml` (77 deployments, 11 variants × 7 keys)
- Docker Compose: `/opt/cc-infra/docker-compose.yml`
- Proxy logs: `/opt/cc-infra/logs/proxy/` and `/opt/cc-infra/logs/proxy-40002/`
- LiteLLM logs: `/opt/cc-infra/logs/litellm-glm51/` and `/opt/cc-infra/logs/litellm-dsv4p/`
- Codex settings: `~/.claude/settings.json` (uses ANTHROPIC_BASE_URL=http://127.0.0.1:40001)

## Full Analysis Report

See `docs/analysis_report.md` for the detailed error analysis, model variant test report, and change justification.