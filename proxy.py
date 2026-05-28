#!/usr/bin/env python3
"""Anthropic → OpenAI Converter Proxy for Claude Code.

Accepts Anthropic Messages API (/v1/messages) and converts to
OpenAI Chat Completions API (/v1/chat/completions), forwarding
to per-model LiteLLM gateways. Handles streaming SSE conversion
and two-layer tool description truncation for third-party models.

Architecture:
  Claude Code → :40001 (this proxy, Anthropic format)
      → :41001 (LiteLLM glm5.1, OpenAI format)
      → :42001 (LiteLLM dsv4p, OpenAI format) [reserved]
      → ModelScope API

Environment variables:
  LITELLM_URL_GLM51  — glm5.1 LiteLLM chat URL (default: http://glm5.1_uni41001:4000/v1/chat/completions)
  LITELLM_URL_DSV4P  — dsv4p LiteLLM chat URL (default: http://dsv4p_uni42001:4000/v1/chat/completions)
  LITELLM_MODELS_URL_GLM51 — glm5.1 models URL
  LITELLM_MODELS_URL_DSV4P — dsv4p models URL
  LITELLM_KEY  — API key for upstream (default: sk-litellm-local)
  LISTEN_PORT  — local listen port (default: 40001)
  PROXY_TIMEOUT — upstream timeout in seconds (default: 300)
  MAX_TOOL_DESC — max chars for tool function descriptions (default: 800)
  MAX_SCHEMA_DESC — max chars for schema parameter descriptions (default: 300)
"""
import http.server
import json
import os
import sys
import time
import datetime
import traceback
import threading
import http.client
import urllib.parse
import socketserver
import re

import uuid

# ─── Configuration ────────────────────────────────────────────────────────
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-local")
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "40001"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))
MAX_TOOL_DESC = int(os.environ.get("MAX_TOOL_DESC", "800"))
MAX_SCHEMA_DESC = int(os.environ.get("MAX_SCHEMA_DESC", "300"))
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# Per-model upstream configuration (Docker DNS resolves container names)
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": os.environ.get("LITELLM_URL_GLM51", "http://glm5.1_uni41001:4000/v1/chat/completions"),
        "models_url": os.environ.get("LITELLM_MODELS_URL_GLM51", "http://glm5.1_uni41001:4000/v1/models"),
    },
    "dsv4p": {
        "chat_url": os.environ.get("LITELLM_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/chat/completions"),
        "models_url": os.environ.get("LITELLM_MODELS_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = "glm5.1"

_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()


def _log_error_detail(detail):
    """Write a detailed error entry to error_detail.{date}.jsonl for root-cause analysis.
    Captures full upstream response body, headers, deployment info, and retry context.
    Only written for errors that need investigation: 429, null-choices, ConnectionRefused, TimeoutError."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = datetime.date.today().isoformat()
        with _error_detail_lock, open(os.path.join(LOG_DIR, f"error_detail.{date}.jsonl"), "a") as f:
            f.write(json.dumps(detail, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

# Known model name mapping: Anthropic model names → LiteLLM unified model IDs
MODEL_MAP = {
    "glm5.1": "glm5.1",
    "glm5.1_uni41001": "glm5.1",
    "zhipuai/glm-5.1": "glm5.1",
    "glm-5.1": "glm5.1",
    "dsv4p": "dsv4p",
    "dsv4p_uni42001": "dsv4p",
    "deepseek-ai/deepseek-v4-pro": "dsv4p",
    "deepseek-v4-pro": "dsv4p",
}

DEFAULT_MODEL = "glm5.1"
# Per-model input token limits (GLM-5.1: from ModelScope error "Range of input length should be [1, 202745]")
# DSv4P: 131072 from LiteLLM config max_tokens (conservative estimate, actual limit TBD)
MODEL_MAX_INPUT_TOKENS = {"glm5.1": 202745, "dsv4p": 131072}
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_GLM51", "170000")),
    "dsv4p": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_DSV4P", "100000")),
}
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "4.0"))
# GLM-5.1 built-in thinking/reasoning mode: ModelScope returns reasoning_content in responses.
# When enabled, proxy converts reasoning_content to Anthropic thinking blocks.
ENABLE_THINKING_MODE = os.environ.get("ENABLE_THINKING_MODE", "true").lower() == "true"
# Placeholder signature for thinking blocks (Anthropic requires signature field for multi-turn)
THINKING_SIGNATURE = os.environ.get("THINKING_SIGNATURE", "ErUB3WY0k2GCM2h+4O0S3Y3W3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f")
# Context compression: when input tokens approach safety threshold, compress older messages
ENABLE_CONTEXT_COMPRESSION = os.environ.get("ENABLE_CONTEXT_COMPRESSION", "true").lower() == "true"
# Compression triggers when estimated tokens exceed this fraction of safety threshold
COMPRESSION_THRESHOLD_FRACTION = float(os.environ.get("COMPRESSION_THRESHOLD_FRACTION", "0.85"))
# Max output tokens for GLM-5.1 on ModelScope (verified from API: max_tokens up to 16384 works)
MAX_OUTPUT_TOKENS_GLM51 = int(os.environ.get("MAX_OUTPUT_TOKENS_GLM51", "16384"))
MAX_OUTPUT_TOKENS_DSV4P = int(os.environ.get("MAX_OUTPUT_TOKENS_DSV4P", "8192"))


def _log(level, msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:10]
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = datetime.date.today().isoformat()
        with _log_lock, open(os.path.join(LOG_DIR, f"proxy.{date}.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _log_metrics(entry):
    """Write a structured JSON metrics entry to metrics.jsonl for long-term optimization data."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = datetime.date.today().isoformat()
        with _metrics_lock, open(os.path.join(LOG_DIR, f"metrics.{date}.jsonl"), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _truncate_desc(text, max_len):
    """Truncate a description string using paragraph/sentence/hard-cut strategy."""
    if not text or len(text) <= max_len:
        return text

    # Strategy 1: cut at first double-newline (paragraph break) within 2x max_len
    double_nl = text.find("\n\n")
    if double_nl > 0 and double_nl <= max_len * 2:
        result = text[:double_nl].strip()
        if len(result) <= max_len:
            return result

    # Strategy 2: find last sentence break (. + space) within max_len
    truncated = text[:max_len]
    last_sentence = truncated.rfind(". ")
    if last_sentence > max_len // 4:
        return text[:last_sentence + 1].strip()

    # Strategy 3: hard cut with ellipsis
    return text[:max_len - 3].rstrip() + "..."


def _truncate_schema_descriptions(schema, max_len=MAX_SCHEMA_DESC):
    """Recursively truncate all 'description' fields in a JSON schema tree."""
    if isinstance(schema, dict):
        for key in schema:
            if key == "description" and isinstance(schema[key], str):
                schema[key] = _truncate_desc(schema[key], max_len)
            else:
                _truncate_schema_descriptions(schema[key], max_len)
    elif isinstance(schema, list):
        for item in schema:
            _truncate_schema_descriptions(item, max_len)
    return schema


# ─── Anthropic → OpenAI Format Conversion ──────────────────────────────────

def _tool_anth_to_oai(anth_tools):
    """Convert Anthropic tool definitions to OpenAI function_calling format,
    with two-layer truncation."""
    oai_tools = []
    total_desc_chars = 0
    total_truncated_chars = 0

    for tool in anth_tools:
        # Anthropic tools have "type": "tool_use" but some clients omit it
        # (default is tool_use). Accept both explicit and implicit tool_use.
        tool_type = tool.get("type", "tool_use")
        if tool_type != "tool_use":
            continue

        name = tool.get("name", "")
        desc = tool.get("description", "")
        original_desc_len = len(desc) if desc else 0
        total_desc_chars += original_desc_len

        # Layer 1: truncate function-level description
        truncated_desc = _truncate_desc(desc, MAX_TOOL_DESC)
        total_truncated_chars += len(truncated_desc) if truncated_desc else 0

        # Layer 2: truncate schema parameter descriptions
        input_schema = tool.get("input_schema", {})
        if input_schema:
            input_schema = _truncate_schema_descriptions(
                json.loads(json.dumps(input_schema)),  # deep copy
                MAX_SCHEMA_DESC
            )
            # Remove title from schema (Anthropic adds these, OpenAI doesn't need them)
            input_schema.pop("title", None)
            for prop in input_schema.get("properties", {}):
                input_schema["properties"][prop].pop("title", None)

        oai_tool = {
            "type": "function",
            "function": {
                "name": name,
                "description": truncated_desc or "",
                "parameters": input_schema or {"type": "object", "properties": {}},
            }
        }
        # Remove empty required array
        if "required" in oai_tool["function"]["parameters"]:
            if not oai_tool["function"]["parameters"]["required"]:
                del oai_tool["function"]["parameters"]["required"]

        oai_tools.append(oai_tool)

    if total_desc_chars > 0:
        _log("TOOL-SIZE", f"{len(oai_tools)} tools, {total_desc_chars} total desc chars")
        _log("TOOL-SIZE-TRUNCATED", f"{len(oai_tools)} tools, {total_truncated_chars} total desc chars (was {total_desc_chars})")

    return oai_tools


def _map_model(model_name):
    """Map Anthropic model names to LiteLLM/OpenAI model names."""
    mapped = MODEL_MAP.get(model_name)
    if mapped:
        return mapped
    # If the model name already matches a known LiteLLM name, keep it
    for v in MODEL_MAP.values():
        if model_name == v:
            return model_name
    # Default fallback
    _log("MODEL", f"Unknown model '{model_name}', using default '{DEFAULT_MODEL}'")
    return DEFAULT_MODEL


def _convert_tool_choice(anth_choice):
    """Convert Anthropic tool_choice to OpenAI tool_choice."""
    if not anth_choice:
        return None
    if isinstance(anth_choice, dict):
        ctype = anth_choice.get("type", "")
        if ctype == "auto":
            return "auto"
        if ctype == "any":
            return "required"
        if ctype == "tool":
            return {"type": "function", "function": {"name": anth_choice.get("name", "")}}
    if isinstance(anth_choice, str):
        if anth_choice == "auto":
            return "auto"
        if anth_choice == "any":
            return "required"
    return "auto"


def anth_to_openai(body):
    """Convert a full Anthropic Messages API request body to OpenAI Chat Completions format."""
    model = _map_model(body.get("model", DEFAULT_MODEL))

    # Extract system prompt (supports string or content-block array)
    system_text = ""
    system_blocks = body.get("system")
    if system_blocks:
        if isinstance(system_blocks, str):
            system_text = system_blocks
        elif isinstance(system_blocks, list):
            parts = []
            for block in system_blocks:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        # System tool_use blocks are unusual but handle them
                        parts.append(f"[Tool: {block.get('name', '')}]")
                elif isinstance(block, str):
                    parts.append(block)
            system_text = "\n".join(parts)

    # Convert messages
    oai_messages = []
    if system_text:
        oai_messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            # Handle content blocks (text + tool_result)
            if isinstance(content, list):
                text_parts = []
                tool_results = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            tool_results.append(block)
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "image":
                            # Skip images for third-party models
                            pass
                    elif isinstance(block, str):
                        text_parts.append(block)
                # Add text content
                if text_parts:
                    oai_messages.append({"role": "user", "content": "\n".join(text_parts)})
                # Add tool results as tool role messages
                for tr in tool_results:
                    tool_use_id = tr.get("tool_use_id", "")
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_texts = []
                        for b in tr_content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                tr_texts.append(b.get("text", ""))
                            elif isinstance(b, str):
                                tr_texts.append(b)
                        tr_content = "\n".join(tr_texts) if tr_texts else ""
                    oai_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_use_id,
                        "content": str(tr_content) if tr_content else "",
                    })
            else:
                oai_messages.append({"role": "user", "content": str(content)})

        elif role == "assistant":
            # Handle content blocks (text + tool_use)
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {})),
                                }
                            })
                        elif block.get("type") == "thinking":
                            # GLM-5.1 has built-in thinking mode. Convert Anthropic thinking
                            # blocks to a special system-like prefix in OpenAI format so
                            # the model understands prior reasoning context.
                            if ENABLE_THINKING_MODE:
                                thinking_text = block.get("thinking", "")
                                if thinking_text:
                                    # Include as a reasoning prefix in the assistant message
                                    text_parts.append(f"[Previous reasoning: {thinking_text}]")
                            else:
                                pass
                msg_dict = {"role": "assistant"}
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                elif tool_calls:
                    msg_dict["content"] = None
                else:
                    msg_dict["content"] = ""
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                oai_messages.append(msg_dict)
            else:
                oai_messages.append({"role": "assistant", "content": str(content) if content else ""})

    # Convert tools
    oai_tools = _tool_anth_to_oai(body.get("tools", []))

    # Build OpenAI request
    oai_body = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": min(body.get("max_tokens", 4096), MAX_OUTPUT_TOKENS_GLM51),
        "stream": body.get("stream", False),
    }

    # GLM-5.1 has built-in thinking/reasoning mode. When codex sends thinking parameters,
    # we pass them through so GLM-5.1 activates its reasoning_content output.
    # This is NOT "unsupported by third-party models" — GLM-5.1 natively supports it.
    thinking_config = body.get("thinking")
    if thinking_config and ENABLE_THINKING_MODE:
        # GLM-5.1 uses do_search or similar parameters, but its reasoning is always active
        # when the model decides to think. We don't need to explicitly enable it —
        # just ensure reasoning_content in responses is converted to thinking blocks.
        _log("THINKING", f"thinking config received: {thinking_config}, GLM-5.1 will use built-in reasoning")

    if oai_tools:
        oai_body["tools"] = oai_tools
        tool_choice = _convert_tool_choice(body.get("tool_choice"))
        if tool_choice:
            oai_body["tool_choice"] = tool_choice

    # Add stream_options for streaming requests to get usage data
    if oai_body["stream"]:
        oai_body["stream_options"] = {"include_usage": True}

    # Pass through temperature and top_p if present
    if "temperature" in body:
        oai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        oai_body["top_p"] = body["top_p"]
    if "stop" in body:
        oai_body["stop"] = body["stop"]

    return oai_body


# ─── OpenAI → Anthropic Format Conversion ────────────────────────────────

def openai_to_anth(oai_response, request_model):
    """Convert an OpenAI Chat Completions response to Anthropic Messages format.
    Handles GLM-5.1 reasoning_content by converting to Anthropic thinking blocks."""
    choice = oai_response.get("choices", [{}])[0]
    message = choice.get("message", {})

    content_blocks = []

    # GLM-5.1 reasoning_content → Anthropic thinking block (before text content)
    reasoning_content = message.get("reasoning_content")
    if not reasoning_content:
        # LiteLLM may put reasoning in provider_specific_fields
        psf = message.get("provider_specific_fields", {})
        reasoning_content = psf.get("reasoning_content")
    if reasoning_content and ENABLE_THINKING_MODE:
        _log("THINKING", f"reasoning_content found: {len(reasoning_content)} chars")
        content_blocks.append({
            "type": "thinking",
            "thinking": reasoning_content,
            "signature": THINKING_SIGNATURE,
        })
    elif ENABLE_THINKING_MODE:
        _log("THINKING", f"no reasoning_content in message keys: {list(message.keys())[:10]}")

    # Text content
    text_content = message.get("content")
    if text_content:
        content_blocks.append({"type": "text", "text": text_content})

    # Tool calls
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc.get("function", {}).get("name", ""),
            "input": args,
        })

    # Stop reason mapping
    finish_reason = choice.get("finish_reason", "stop")
    stop_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }
    stop_reason = stop_map.get(finish_reason, "end_turn")

    # If there are tool calls and text, Claude Code expects tool_use stop reason
    if tool_calls:
        stop_reason = "tool_use"

    usage = oai_response.get("usage", {})

    result = {
        "id": oai_response.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "model": request_model,
        "content": content_blocks if content_blocks else [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }

    return result


# ─── Responses API → Chat Completions Conversion ──────────────────────────

def responses_to_chat(body):
    """Convert an OpenAI Responses API request body to Chat Completions format."""
    model = _map_model(body.get("model", DEFAULT_MODEL))

    # Build messages from input
    oai_messages = []

    # Instructions → system message
    instructions = body.get("instructions", "")
    if instructions:
        oai_messages.append({"role": "system", "content": instructions})

    # Input can be a string or an array of message items
    input_val = body.get("input", "")
    if isinstance(input_val, str):
        oai_messages.append({"role": "user", "content": input_val})
    elif isinstance(input_val, list):
        for item in input_val:
            if isinstance(item, str):
                oai_messages.append({"role": "user", "content": item})
                continue
            role = item.get("role", "user")
            content = item.get("content", "")
            # Convert content parts
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                tool_results = []
                for part in content:
                    ptype = part.get("type", "")
                    if ptype == "input_text":
                        text_parts.append(part.get("text", ""))
                    elif ptype == "output_text":
                        text_parts.append(part.get("text", ""))
                    elif ptype == "text":
                        text_parts.append(part.get("text", ""))
                    elif ptype == "function_call":
                        tool_calls.append({
                            "id": part.get("call_id", part.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": part.get("arguments", "{}"),
                            }
                        })
                    elif ptype == "function_call_output":
                        tool_results.append({
                            "tool_call_id": part.get("call_id", ""),
                            "content": part.get("output", ""),
                        })
                msg_dict = {"role": role}
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                elif tool_calls:
                    msg_dict["content"] = None
                else:
                    msg_dict["content"] = ""
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                oai_messages.append(msg_dict)
                for tr in tool_results:
                    oai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"],
                    })
            elif isinstance(content, str):
                oai_messages.append({"role": role, "content": content})
            else:
                oai_messages.append({"role": role, "content": str(content)})

    # Convert tools (Responses API tools → Chat Completions tools)
    oai_tools = []
    for tool in body.get("tools", []):
        tool_type = tool.get("type", "")
        if tool_type == "function":
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                }
            })
        # Skip built-in tools (web_search, file_search, code_interpreter) — not supported by third-party models

    # Build Chat Completions request
    oai_body = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": min(body.get("max_output_tokens", 4096), MAX_OUTPUT_TOKENS_GLM51),
        "stream": body.get("stream", False),
    }

    if oai_tools:
        # Apply truncation to tools
        truncated_tools = _tool_anth_to_oai([{
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        } for t in oai_tools])
        oai_body["tools"] = truncated_tools
        tool_choice = body.get("tool_choice")
        if tool_choice:
            if isinstance(tool_choice, str):
                oai_body["tool_choice"] = tool_choice
            elif isinstance(tool_choice, dict):
                oai_body["tool_choice"] = _convert_tool_choice(tool_choice)

    if "temperature" in body:
        oai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        oai_body["top_p"] = body["top_p"]

    if oai_body["stream"]:
        oai_body["stream_options"] = {"include_usage": True}

    return oai_body


def chat_to_responses(oai_response, request_model):
    """Convert a Chat Completions response to Responses API format."""
    choice = oai_response.get("choices", [{}])[0]
    message = choice.get("message", {})

    output_items = []

    # Text content → output_text message
    text_content = message.get("content")
    if text_content:
        output_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text_content}],
        })

    # Tool calls → function_call items
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        output_items.append({
            "type": "function_call",
            "id": tc.get("id", ""),
            "call_id": tc.get("id", ""),
            "name": tc.get("function", {}).get("name", ""),
            "arguments": tc.get("function", {}).get("arguments", "{}"),
        })

    # If no output items, add an empty message
    if not output_items:
        output_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": ""}],
        })

    finish_reason = choice.get("finish_reason", "stop")
    status = "completed"
    if finish_reason == "length":
        status = "incomplete"

    usage = oai_response.get("usage", {})

    result = {
        "id": oai_response.get("id", f"resp_{int(time.time()*1000)}"),
        "object": "response",
        "model": request_model,
        "status": status,
        "output": output_items,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "created_at": int(time.time()),
        "completed_at": int(time.time()),
        "metadata": {},
    }

    return result


# ─── HTTP Handler ────────────────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    timeout = PROXY_TIMEOUT
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        self._headers_sent = False
        super().__init__(*args, **kwargs)

    def send_response(self, code, message=None):
        self._headers_sent = True
        return super().send_response(code, message)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/"):
            self._send_json(200, {
                "status": "ok",
                "proxy": "anthropic-to-openai",
                "upstreams": {k: v["chat_url"] for k, v in MODEL_UPSTREAMS.items()},
                "port": LISTEN_PORT,
                "timeout": PROXY_TIMEOUT,
                "max_tool_desc": MAX_TOOL_DESC,
                "max_schema_desc": MAX_SCHEMA_DESC,
            })
        elif parsed.path in ("/v1/models", "/models"):
            self._proxy_models()
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, x-api-key, X-Api-Key, "
                         "anthropic-version, anthropic-beta, anthropic-dangerous-direct-browser-access")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/messages":
            self._handle_messages()
        elif parsed.path == "/v1/responses":
            self._handle_responses()
        elif parsed.path in ("/v1/chat/completions", "/chat/completions"):
            # Passthrough: already in OpenAI format
            self._passthrough_openai()
        else:
            self._send_json(404, {"error": "not found"})

    # ─── Anthropic messages handler ────────────────────────────────────

    def _handle_messages(self):
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]
        entry = {"method": "POST", "path": "/v1/messages", "status": 0, "model": "?",
                 "duration_ms": 0, "error": None}
        metrics = {
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "path": "/v1/messages",
            "request_model": "?",
            "mapped_model": "?",
            "stream": False,
            "num_messages": 0,
            "num_tools": 0,
            "system_prompt_chars": 0,
            "total_input_chars": 0,
            "ttfb_ms": None,
            "duration_ms": 0,
            "status": 0,
            "finish_reason": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "tool_truncation": None,
            "content_blocks": 0,
            "tool_calls_count": 0,
            "error_type": None,
            "error_message": None,
            "upstream": "?",
            "proxy_retry": 0,
            "proxy_retry_reason": None,
        }

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            anth_body = json.loads(raw_body)
        except Exception as e:
            self._send_json(400, {"error": {"message": f"bad request: {e}"}})
            entry["status"] = 400; entry["error"] = str(e)
            metrics["status"] = 400; metrics["error_type"] = "BadRequest"; metrics["error_message"] = str(e)
            _log("ERROR", f"bad request: {e}")
            _log_metrics(metrics)
            return

        request_model = anth_body.get("model", DEFAULT_MODEL)
        is_stream = anth_body.get("stream", False)
        entry["model"] = request_model
        metrics["request_model"] = request_model
        metrics["stream"] = is_stream

        # Track system prompt size
        system_blocks = anth_body.get("system")
        if system_blocks:
            if isinstance(system_blocks, str):
                metrics["system_prompt_chars"] = len(system_blocks)
            elif isinstance(system_blocks, list):
                metrics["system_prompt_chars"] = sum(
                    len(b.get("text", "")) if isinstance(b, dict) else len(b)
                    for b in system_blocks
                )

        # Convert Anthropic → OpenAI
        oai_body = anth_to_openai(anth_body)
        mapped_model = oai_body.get("model", DEFAULT_MODEL)
        metrics["mapped_model"] = mapped_model
        metrics["num_messages"] = len(oai_body.get("messages", []))
        metrics["num_tools"] = len(oai_body.get("tools", []))
        metrics["total_input_chars"] = len(json.dumps(oai_body))

        # Track tool truncation metrics
        if metrics["num_tools"] > 0:
            total_orig = sum(len(t.get("description", "")) for t in anth_body.get("tools", [])
                            if t.get("type", "tool_use") == "tool_use")
            total_trunc = sum(len(t.get("function", {}).get("description", ""))
                            for t in oai_body.get("tools", [])
                            if t.get("type") == "function")
            metrics["tool_truncation"] = {
                "original_total_chars": total_orig,
                "truncated_total_chars": total_trunc,
                "reduction_pct": round((1 - total_trunc / total_orig) * 100, 1) if total_orig > 0 else 0,
                "num_tools": metrics["num_tools"],
            }

        _log("REQ", f"model={request_model}→{mapped_model} stream={is_stream} "
                    f"msgs={len(oai_body.get('messages',[]))} "
                    f"tools={len(oai_body.get('tools',[]))}")

        # ─── Select upstream based on model ──────────────────────────────
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]
        metrics["upstream"] = upstream_key

        # ─── Input token pre-check ──────────────────────────────────────────
        model_max_tokens = MODEL_MAX_INPUT_TOKENS.get(upstream_key, 131072)
        model_safety = MODEL_INPUT_TOKEN_SAFETY.get(upstream_key, 120000)
        estimated_tokens = int(metrics["total_input_chars"] / CHARS_PER_TOKEN_ESTIMATE)
        metrics["estimated_input_tokens"] = estimated_tokens

        # ─── Context compression ──────────────────────────────────────────
        # When input tokens approach the safety threshold, compress older messages
        # to keep the conversation within limits without returning a 400 error.
        compression_threshold = int(model_safety * COMPRESSION_THRESHOLD_FRACTION)
        if estimated_tokens > compression_threshold and ENABLE_CONTEXT_COMPRESSION:
            messages = oai_body.get("messages", [])
            num_messages = len(messages)
            _log("COMPRESS", f"tokens={estimated_tokens} > threshold={compression_threshold}, "
                             f"compressing {num_messages} messages")

            # Strategy: keep the last N messages intact (most recent context matters most)
            # Compress older messages by truncating tool_result content and long text messages
            # Keep system message + last ~20 messages intact, compress everything before that
            KEEP_RECENT = 20  # Number of recent messages to keep intact

            if num_messages > KEEP_RECENT + 2:  # +2 for system + first user msg
                compressed_messages = []
                recent_start_idx = num_messages - KEEP_RECENT

                # Compress older messages
                for i, msg in enumerate(messages[:recent_start_idx]):
                    role = msg.get("role", "")
                    content = msg.get("content", "")

                    if role == "tool":
                        # Truncate tool results — they're the biggest token consumers
                        tool_content = str(content)
                        if len(tool_content) > 500:
                            msg["content"] = tool_content[:500] + f"\n...[compressed from {len(tool_content)} chars]"
                    elif role == "assistant":
                        tool_calls = msg.get("tool_calls")
                        if tool_calls:
                            # Keep tool_calls structure but truncate arguments
                            for tc in tool_calls:
                                args = tc.get("function", {}).get("arguments", "")
                                if len(args) > 300:
                                    tc["function"]["arguments"] = args[:300] + "...[compressed]"
                        elif content and len(str(content)) > 200:
                            msg["content"] = str(content)[:200] + "...[compressed]"
                    elif role == "user":
                        if isinstance(content, str) and len(content) > 300:
                            msg["content"] = content[:300] + "...[compressed]"
                    compressed_messages.append(msg)

                # Keep recent messages intact
                compressed_messages.extend(messages[recent_start_idx:])
                oai_body["messages"] = compressed_messages

                # Recalculate estimated tokens after compression
                total_chars_after = sum(
                    len(str(m.get("content", ""))) +
                    sum(len(str(tc.get("function", {}).get("arguments", ""))) for tc in (m.get("tool_calls") or []))
                    for m in compressed_messages
                )
                estimated_tokens = int(total_chars_after / CHARS_PER_TOKEN_ESTIMATE)
                metrics["estimated_input_tokens"] = estimated_tokens
                metrics["context_compressed"] = True
                metrics["original_messages"] = num_messages
                metrics["compressed_messages"] = len(compressed_messages)
                metrics["original_tokens"] = int(metrics["total_input_chars"] / CHARS_PER_TOKEN_ESTIMATE)
                metrics["compressed_tokens"] = estimated_tokens
                _log("COMPRESS", f"compressed: {num_messages}→{len(compressed_messages)} messages, "
                                 f"~{metrics['original_tokens']}→{estimated_tokens} tokens")

        if estimated_tokens > model_safety:
            _log("INPUT-EXCEED", f"estimated_tokens={estimated_tokens} > safety={model_safety}, "
                                 f"input_chars={metrics['total_input_chars']}")
            err_msg = (f"Input exceeds model limit (~{estimated_tokens} estimated input tokens + ~32768 output = ~{estimated_tokens+32768} total, "
                       f"max {model_max_tokens}). Please start a new conversation.")
            metrics["status"] = 400
            metrics["error_type"] = "InputTooLong"
            metrics["error_message"] = err_msg
            self._send_json(400, {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": err_msg},
            })
            entry["status"] = 400
            entry["error"] = err_msg
            _log_metrics(metrics)
            return

        oai_data = json.dumps(oai_body).encode("utf-8")

        # Build upstream request headers
        auth_key = self.headers.get("x-api-key") or self.headers.get("X-Api-Key") or LITELLM_KEY
        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_key}",
            "Content-Length": str(len(oai_data)),
        }

        parsed_upstream = urllib.parse.urlparse(litellm_url)

        # ─── Forward to selected upstream with proxy-level retry ───────────
        # LiteLLM handles retries for rate-limit/timeout internally (num_retries=4)
        # But certain errors are NOT retried by LiteLLM or benefit from proxy retry:
        # 1. InternalServerError with "choices=None" — LiteLLM treats as fatal, won't retry
        # 2. 429 after LiteLLM exhausted all retries — cooldown may have expired by now
        # We retry at proxy level: LiteLLM will route to a different deployment
        # since cooldown marks the failing one.
        PROXY_MAX_RETRIES = 2
        result = None
        for attempt in range(1, PROXY_MAX_RETRIES + 1):
            try:
                if is_stream:
                    result = self._stream_to_anth(oai_data, headers_out, parsed_upstream, request_model, mapped_model, entry, metrics)
                else:
                    result = self._non_stream_to_anth(oai_data, headers_out, parsed_upstream, request_model, mapped_model, entry, metrics)
            except Exception as e:
                _log("ERROR", f"transport error: {type(e).__name__}: {e}")
                entry["status"] = 502; entry["error"] = str(e)
                metrics["status"] = 502; metrics["error_type"] = type(e).__name__; metrics["error_message"] = str(e)[:500]
                # Log transport errors (ConnectionRefused, TimeoutError) for root-cause analysis
                _log_error_detail({
                    "request_id": metrics.get("request_id", "?"),
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": type(e).__name__,
                    "upstream_status": 502,
                    "upstream_headers": {},
                    "upstream_error_body_full": str(e),
                    "litellm_detail": {},
                    "request_model": request_model,
                    "mapped_model": mapped_model,
                    "stream": is_stream,
                    "num_messages": metrics.get("num_messages", 0),
                    "num_tools": metrics.get("num_tools", 0),
                    "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                    "proxy_attempt": attempt,
                    "total_input_chars": metrics.get("total_input_chars", 0),
                    "transport_exception": traceback.format_exc()[:2000],
                })
                # Retry ConnectionRefusedError: LiteLLM may be temporarily down (e.g. container restart)
                # Latency-based routing will pick a different deployment on retry
                if isinstance(e, ConnectionRefusedError) and attempt < PROXY_MAX_RETRIES:
                    retry_delay = 2
                    _log("RETRY", f"ConnectionRefusedError on attempt {attempt}/{PROXY_MAX_RETRIES}, retrying in {retry_delay}s")
                    metrics["proxy_retry"] = attempt
                    metrics["proxy_retry_reason"] = "transport-ConnectionRefusedError"
                    _log_error_detail({
                        "request_id": metrics.get("request_id", "?"),
                        "timestamp": datetime.datetime.now().isoformat(),
                        "error_subcategory": "transport-ConnectionRefusedError_retry",
                        "upstream_status": 502,
                        "upstream_headers": {},
                        "upstream_error_body_full": str(e),
                        "litellm_detail": {
                            "retry_attempt": attempt,
                            "max_retries": PROXY_MAX_RETRIES,
                            "retry_reason": "transport-ConnectionRefusedError",
                        },
                        "request_model": request_model,
                        "mapped_model": mapped_model,
                        "stream": is_stream,
                        "num_messages": metrics.get("num_messages", 0),
                        "num_tools": metrics.get("num_tools", 0),
                        "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                        "proxy_attempt": attempt,
                        "total_input_chars": metrics.get("total_input_chars", 0),
                    })
                    time.sleep(retry_delay)
                    conn = self._make_upstream_conn(parsed_upstream)
                    continue
                self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": f"Transport error: {type(e).__name__}: {e}"}})
                _log_metrics(metrics)
                return

            if result is None:
                _log("ERROR", "No result from upstream")
                entry["status"] = 502
                _log_metrics(metrics)
                return

            if result.get("ok"):
                break

            # Check if this error should be retried at proxy level
            # 1. null-choices (InternalServerError with choices=None) — always retry
            # 2. 429 rate-limit after LiteLLM exhausted retries — retry 1 time with short delay
            #    (cooldown may have expired since LiteLLM started its 4-retry cycle)
            # 3. 502 ConnectionRefused — _non_stream_to_anth catches it and returns dict
            #    instead of raising, so the transport-exception retry path never fires.
            #    Latency-based routing will pick a different deployment on retry.
            err_text = result.get("error_text", "")
            should_retry = False
            retry_reason = ""
            retry_delay = 1  # seconds

            if "Invalid response object" in err_text and "choices" in err_text:
                should_retry = True
                retry_reason = "null-choices"
                retry_delay = 1
            elif result.get("status") == 429 and attempt == 1:
                # Only retry 429 once — cooldown may have partially expired
                should_retry = True
                retry_reason = "429-rate-limit"
                # Use Retry-After from upstream if present, else 15s (half of cooldown_time=60)
                retry_after_header = result.get("error_body", b"")
                # Check if upstream response headers contain retry-after (from error_detail logging context)
                retry_delay = 15  # half of cooldown_time to let LiteLLM cooldown clear some deployments
                try:
                    err_json_retry = json.loads(result.get("error_text", ""))
                    # Some LiteLLM responses include retry_after info in headers already logged in error_detail
                    retry_after_str = str(result.get("retry_after", ""))
                    if retry_after_str:
                        retry_delay = min(int(retry_after_str), 60)
                except (json.JSONDecodeError, ValueError):
                    pass
            elif result.get("status") == 502 and ("ConnectionRefusedError" in err_text or "Connection refused" in err_text):
                should_retry = True
                retry_reason = "502-ConnectionRefused"
                retry_delay = 2

            if should_retry and attempt < PROXY_MAX_RETRIES:
                _log("RETRY", f"{retry_reason} error on attempt {attempt}/{PROXY_MAX_RETRIES}, retrying in {retry_delay}s")
                metrics["proxy_retry"] = attempt
                metrics["proxy_retry_reason"] = retry_reason
                # Log the proxy-level retry error detail
                _log_error_detail({
                    "request_id": metrics.get("request_id", "?"),
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": retry_reason + "_retry",
                    "upstream_status": result.get("status", 0),
                    "upstream_headers": {},
                    "upstream_error_body_full": str(result.get("error_text", ""))[:3000],
                    "litellm_detail": {
                        "retry_attempt": attempt,
                        "max_retries": PROXY_MAX_RETRIES,
                        "retry_reason": retry_reason,
                    },
                    "request_model": request_model,
                    "mapped_model": mapped_model,
                    "stream": is_stream,
                    "num_messages": metrics.get("num_messages", 0),
                    "num_tools": metrics.get("num_tools", 0),
                    "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                    "proxy_attempt": attempt,
                    "total_input_chars": metrics.get("total_input_chars", 0),
                })
                # Brief pause to let LiteLLM cooldown mark the failing deployment
                time.sleep(retry_delay)
                # Re-establish connection for retry
                conn = self._make_upstream_conn(parsed_upstream)
                continue
            break

        if result.get("ok"):
            if result.get("already_sent"):
                pass
            else:
                self._send_json(200, result["anth_response"])
                entry["status"] = 200
        else:
            err_status = result.get("status", 502)
            err_body = result.get("error_body", b'{"error":{"message":"unknown error"}}')
            if result.get("is_stream"):
                self._send_anth_error(err_status, result.get("error_text", "unknown error"))
            else:
                self._send_raw(err_status, err_body, "application/json")
            entry["status"] = err_status
            entry["error"] = result.get("error_text", "")[:200]

        # ─── Final metrics ─────────────────────────────────────────────────
        entry["duration_ms"] = int((time.time() - t_start) * 1000)
        metrics["duration_ms"] = entry["duration_ms"]
        _log("DONE", f"status={entry['status']} model={entry['model']} "
                     f"dur={entry['duration_ms']}ms "
                     f"upstream={metrics['upstream']}")
        _log_metrics(metrics)

    # ─── Non-streaming Anthropic response ──────────────────────────────

    def _non_stream_to_anth(self, oai_data, headers_out, parsed_upstream,
                             request_model, mapped_model, entry, metrics):
        """Non-streaming request. Returns a result dict instead of sending to client,
        so the retry loop can decide whether to retry or send the final result."""
        conn = self._make_upstream_conn(parsed_upstream)
        path = parsed_upstream.path or "/v1/chat/completions"

        try:
            conn.request("POST", path, body=oai_data, headers=headers_out)
            resp = conn.getresponse()
            # Capture upstream response headers for error diagnosis
            resp_headers = dict(resp.getheaders())

            if resp.status != 200:
                err_body = resp.read()
                err_text = err_body.decode()[:500]
                err_full = err_body.decode()[:3000]  # full error body for error_detail log
                entry["status"] = resp.status
                entry["error"] = err_text
                metrics["status"] = resp.status
                metrics["error_type"] = "UpstreamError"
                metrics["error_message"] = err_text[:200]
                metrics["upstream_status"] = resp.status
                metrics["upstream_error_body"] = err_text[:500]
                # Capture LiteLLM deployment info from error response headers (was missing before)
                metrics["litellm_model_id"] = resp_headers.get("x-litellm-model-id", "")
                err_deployment = {}
                if resp_headers.get("x-litellm-model-id"):
                    err_deployment["model_id"] = resp_headers["x-litellm-model-id"]
                if resp_headers.get("x-litellm-model-api-base"):
                    err_deployment["api_base"] = resp_headers["x-litellm-model-api-base"]
                # Fallback: extract from hidden_params in error body
                try:
                    err_json_hidden = json.loads(err_full)
                    err_hidden = err_json_hidden.get("error", {}).get("_hidden_params", {}) or err_json_hidden.get("error", {}).get("hidden_params", {})
                    if err_hidden and not err_deployment.get("model_id"):
                        err_deployment["model_id"] = err_hidden.get("model_id", "")
                    if err_hidden and not err_deployment.get("api_base"):
                        err_deployment["api_base"] = err_hidden.get("api_base", "")
                except (json.JSONDecodeError, TypeError):
                    pass
                if err_deployment:
                    metrics["litellm_deployment"] = err_deployment
                _log("ERROR", f"upstream {resp.status}: {err_text[:200]} deployment={err_deployment}")

                # ─── Detailed error logging for root-cause analysis ───
                # Parse LiteLLM error response for deployment/key info
                litellm_detail = {}
                try:
                    err_json = json.loads(err_full)
                    err_obj = err_json.get("error", {})
                    litellm_detail = {
                        "litellm_error_type": err_obj.get("type", ""),
                        "litellm_error_code": err_obj.get("code", ""),
                        "litellm_error_message_full": str(err_obj.get("message", ""))[:2000],
                    }
                    # Extract hidden_params / metadata if present (contains deployment info)
                    hidden_params = err_obj.get("_hidden_params", {}) or err_obj.get("hidden_params", {})
                    if hidden_params:
                        litellm_detail["litellm_hidden_params"] = {
                            "model_id": hidden_params.get("model_id", ""),
                            "api_base": hidden_params.get("api_base", ""),
                            "model_info": str(hidden_params.get("model_info", {}))[:500],
                        }
                    metadata = err_obj.get("metadata", {})
                    if metadata:
                        litellm_detail["litellm_metadata"] = str(metadata)[:1000]
                except (json.JSONDecodeError, TypeError):
                    pass

                # Capture LiteLLM-specific response headers for deployment tracing
                litellm_headers = {}
                for h_key in ["x-litellm-call-id", "x-litellm-key-spend", "x-litellm-response-cost",
                               "x-litellm-model-id", "x-litellm-cache-hit"]:
                    if h_key in resp_headers:
                        litellm_headers[h_key] = resp_headers[h_key]
                litellm_detail["litellm_response_headers"] = litellm_headers

                # Determine error sub-category for focused logging
                err_subcategory = "upstream_error"
                if resp.status == 429 or "RateLimitError" in err_text:
                    err_subcategory = "429_rate_limit"
                elif "Invalid response object" in err_text and "choices" in err_text:
                    err_subcategory = "null_choices"
                elif resp.status == 502:
                    err_subcategory = "502_bad_gateway"

                _log_error_detail({
                    "request_id": metrics.get("request_id", "?"),
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": err_subcategory,
                    "upstream_status": resp.status,
                    "upstream_headers": resp_headers,
                    "upstream_error_body_full": err_full,
                    "litellm_detail": litellm_detail,
                    "request_model": request_model,
                    "mapped_model": mapped_model,
                    "stream": False,
                    "num_messages": metrics.get("num_messages", 0),
                    "num_tools": metrics.get("num_tools", 0),
                    "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                    "proxy_attempt": metrics.get("proxy_retry", 0),
                    "total_input_chars": metrics.get("total_input_chars", 0),
                })

                # Return error dict — don't send to client yet (retry loop handles this)
                return {"ok": False, "status": resp.status, "error_body": err_body,
                        "error_text": err_text}

            resp_body = resp.read().decode("utf-8")
            oai_response = json.loads(resp_body)

            # ─── Capture LiteLLM deployment info from successful response ───
            # LiteLLM includes model_id and rate limits in response headers
            litellm_deployment = {}
            resp_model_id = oai_response.get("model", "")
            # Extract deployment ID and ModelScope rate limits from response headers
            if "x-litellm-model-id" in resp_headers:
                litellm_deployment["model_id"] = resp_headers["x-litellm-model-id"]
            if "x-litellm-model-api-base" in resp_headers:
                litellm_deployment["api_base"] = resp_headers["x-litellm-model-api-base"]
            # Capture ModelScope rate limit info for diagnostic purposes
            ms_rate_limits = {}
            for h_key in resp_headers:
                if "ratelimit" in h_key.lower() or "remaining" in h_key.lower() or "limit" in h_key.lower():
                    ms_rate_limits[h_key] = resp_headers[h_key]
            if ms_rate_limits:
                litellm_deployment["upstream_rate_limits"] = ms_rate_limits
            # Hidden params from response body (if present)
            hidden_params = oai_response.get("_hidden_params", {}) or oai_response.get("hidden_params", {})
            if hidden_params:
                if not litellm_deployment.get("model_id"):
                    litellm_deployment["model_id"] = hidden_params.get("model_id", "")
                if not litellm_deployment.get("api_base"):
                    litellm_deployment["api_base"] = hidden_params.get("api_base", "")
                litellm_deployment["model_info"] = str(hidden_params.get("model_info", {}))[:500]
            metrics["litellm_model_id"] = resp_headers.get("x-litellm-model-id", resp_model_id)
            metrics["litellm_deployment"] = litellm_deployment

            # Convert OpenAI → Anthropic
            anth_response = openai_to_anth(oai_response, request_model)

            entry["status"] = 200
            metrics["status"] = 200

            usage = oai_response.get("usage", {})
            finish_reason = oai_response.get("choices", [{}])[0].get("finish_reason", "?")
            content_blocks = anth_response.get("content", [])
            tool_calls_count = sum(1 for b in content_blocks if b.get("type") == "tool_use")
            metrics["input_tokens"] = usage.get("prompt_tokens", 0)
            metrics["output_tokens"] = usage.get("completion_tokens", 0)
            metrics["finish_reason"] = finish_reason
            metrics["content_blocks"] = len(content_blocks)
            metrics["tool_calls_count"] = tool_calls_count
            _log("RESP", f"in={usage.get('prompt_tokens',0)} "
                         f"out={usage.get('completion_tokens',0)} "
                         f"finish={finish_reason} "
                         f"deployment={resp_model_id}")

            # Return success dict — retry loop will send it to client
            return {"ok": True, "anth_response": anth_response}

        except Exception as e:
            entry["status"] = 502
            entry["error"] = f"{type(e).__name__}: {e}"
            metrics["status"] = 502
            metrics["error_type"] = type(e).__name__
            metrics["error_message"] = str(e)[:500]
            _log("ERROR", f"transport error: {type(e).__name__}: {e}")
            # Log transport errors for root-cause analysis
            _log_error_detail({
                "request_id": metrics.get("request_id", "?"),
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": type(e).__name__,
                "upstream_status": 502,
                "upstream_headers": {},
                "upstream_error_body_full": str(e),
                "litellm_detail": {},
                "request_model": request_model,
                "mapped_model": mapped_model,
                "stream": False,
                "num_messages": metrics.get("num_messages", 0),
                "num_tools": metrics.get("num_tools", 0),
                "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                "proxy_attempt": metrics.get("proxy_retry", 0),
                "total_input_chars": metrics.get("total_input_chars", 0),
                "transport_exception": traceback.format_exc()[:2000],
            })
            return {"ok": False, "status": 502, "error_body": json.dumps({"error": {"message": str(e)}}).encode(),
                    "error_text": str(e)[:500]}
        finally:
            conn.close()

    # ─── Streaming Anthropic response (SSE conversion) ──────────────────

    def _stream_to_anth(self, oai_data, headers_out, parsed_upstream,
                         request_model, mapped_model, entry, metrics):
        """Streaming request. Returns a result dict for errors (so retry loop can try
        another model). For 200 success, streams directly to client (irreversible)."""
        conn = self._make_upstream_conn(parsed_upstream)
        path = parsed_upstream.path or "/v1/chat/completions"

        try:
            conn.request("POST", path, body=oai_data, headers=headers_out)
            resp = conn.getresponse()
            t_ttfb = time.time()  # TTFB: time to first byte from upstream

            if resp.status != 200:
                err_body = resp.read()
                err_text = err_body.decode()[:500]
                err_full = err_body.decode()[:3000]
                resp_headers = dict(resp.getheaders())
                entry["status"] = resp.status
                entry["error"] = err_text
                metrics["status"] = resp.status
                metrics["error_type"] = "UpstreamError"
                metrics["error_message"] = err_text[:200]
                metrics["upstream_status"] = resp.status
                metrics["upstream_error_body"] = err_text[:500]
                # Capture LiteLLM deployment info from error response headers (was missing before)
                metrics["litellm_model_id"] = resp_headers.get("x-litellm-model-id", "")
                err_deployment = {}
                if resp_headers.get("x-litellm-model-id"):
                    err_deployment["model_id"] = resp_headers["x-litellm-model-id"]
                if resp_headers.get("x-litellm-model-api-base"):
                    err_deployment["api_base"] = resp_headers["x-litellm-model-api-base"]
                # Fallback: extract from hidden_params in error body
                try:
                    err_json_hidden = json.loads(err_full)
                    err_hidden = err_json_hidden.get("error", {}).get("_hidden_params", {}) or err_json_hidden.get("error", {}).get("hidden_params", {})
                    if err_hidden and not err_deployment.get("model_id"):
                        err_deployment["model_id"] = err_hidden.get("model_id", "")
                    if err_hidden and not err_deployment.get("api_base"):
                        err_deployment["api_base"] = err_hidden.get("api_base", "")
                except (json.JSONDecodeError, TypeError):
                    pass
                if err_deployment:
                    metrics["litellm_deployment"] = err_deployment
                _log("ERROR", f"upstream {resp.status}: {err_text[:200]} deployment={err_deployment}")

                # ─── Detailed error logging for streaming errors ───
                litellm_detail = {}
                try:
                    err_json = json.loads(err_full)
                    err_obj = err_json.get("error", {})
                    litellm_detail = {
                        "litellm_error_type": err_obj.get("type", ""),
                        "litellm_error_code": err_obj.get("code", ""),
                        "litellm_error_message_full": str(err_obj.get("message", ""))[:2000],
                    }
                    hidden_params = err_obj.get("_hidden_params", {}) or err_obj.get("hidden_params", {})
                    if hidden_params:
                        litellm_detail["litellm_hidden_params"] = {
                            "model_id": hidden_params.get("model_id", ""),
                            "api_base": hidden_params.get("api_base", ""),
                            "model_info": str(hidden_params.get("model_info", {}))[:500],
                        }
                    metadata = err_obj.get("metadata", {})
                    if metadata:
                        litellm_detail["litellm_metadata"] = str(metadata)[:1000]
                except (json.JSONDecodeError, TypeError):
                    pass

                # Capture LiteLLM-specific response headers for deployment tracing
                litellm_headers = {}
                for h_key in ["x-litellm-call-id", "x-litellm-key-spend", "x-litellm-response-cost",
                               "x-litellm-model-id", "x-litellm-cache-hit"]:
                    if h_key in resp_headers:
                        litellm_headers[h_key] = resp_headers[h_key]
                litellm_detail["litellm_response_headers"] = litellm_headers

                err_subcategory = "upstream_error"
                if resp.status == 429 or "RateLimitError" in err_text:
                    err_subcategory = "429_rate_limit"
                elif "Invalid response object" in err_text and "choices" in err_text:
                    err_subcategory = "null_choices"

                _log_error_detail({
                    "request_id": metrics.get("request_id", "?"),
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": err_subcategory,
                    "upstream_status": resp.status,
                    "upstream_headers": resp_headers,
                    "upstream_error_body_full": err_full,
                    "litellm_detail": litellm_detail,
                    "request_model": request_model,
                    "mapped_model": mapped_model,
                    "stream": True,
                    "num_messages": metrics.get("num_messages", 0),
                    "num_tools": metrics.get("num_tools", 0),
                    "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                    "proxy_attempt": metrics.get("proxy_retry", 0),
                    "total_input_chars": metrics.get("total_input_chars", 0),
                })

                # Return error dict — don't send to client yet (retry loop handles this)
                return {"ok": False, "status": resp.status, "error_body": err_body,
                        "error_text": err_text, "is_stream": True}

# Start Anthropic SSE stream
            # Capture deployment info and rate limits from LiteLLM response headers before streaming
            resp_headers_dict = dict(resp.getheaders())
            litellm_deployment_stream = {}
            ms_rate_limits_stream = {}
            if "x-litellm-model-id" in resp_headers_dict:
                litellm_deployment_stream["model_id"] = resp_headers_dict["x-litellm-model-id"]
                litellm_model_id = resp_headers_dict["x-litellm-model-id"]
            if "x-litellm-model-api-base" in resp_headers_dict:
                litellm_deployment_stream["api_base"] = resp_headers_dict["x-litellm-model-api-base"]
            for h_key in resp_headers_dict:
                if "ratelimit" in h_key.lower() or "remaining" in h_key.lower() or "limit" in h_key.lower():
                    ms_rate_limits_stream[h_key] = resp_headers_dict[h_key]
            if ms_rate_limits_stream:
                litellm_deployment_stream["upstream_rate_limits"] = ms_rate_limits_stream
            metrics["litellm_deployment"] = litellm_deployment_stream

            # CRITICAL: Use Connection: close for SSE so client detects stream end
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")  # Close connection after stream ends
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # Signal that connection should close after this handler
            self.close_connection = True

            # Emit message_start
            msg_id = f"msg_{int(time.time()*1000)}"
            self._send_sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": request_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            })

            # State machine for content blocks
            content_block_idx = 0
            in_text_block = False
            in_tool_block = False
            in_thinking_block = False
            current_tool_id = ""
            current_tool_name = ""
            tool_args_buffer = ""
            total_output_tokens = 0
            input_tokens = 0
            finish_reason = None
            ttfb_recorded = False
            tool_calls_in_stream = 0
            litellm_model_id = ""  # Track which LiteLLM deployment was used

            buffer = ""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()

                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            # Close any open blocks
                            if in_text_block:
                                self._send_sse("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": content_block_idx - 1,
                                })
                            if in_thinking_block:
                                self._send_sse("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": content_block_idx - 1,
                                })
                            if in_tool_block:
                                self._send_sse("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": content_block_idx - 1,
                                })
                            # Emit message_delta with final stop reason
                            stop_reason = "end_turn"
                            if finish_reason == "tool_calls":
                                stop_reason = "tool_use"
                            elif finish_reason == "length":
                                stop_reason = "max_tokens"

                            self._send_sse("message_delta", {
                                "type": "message_delta",
                                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                "usage": {"output_tokens": total_output_tokens},
                            })
                            self._send_sse("message_stop", {"type": "message_stop"})
                            self.wfile.flush()
                            entry["status"] = 200
                            metrics["status"] = 200
                            metrics["finish_reason"] = finish_reason
                            metrics["input_tokens"] = input_tokens
                            metrics["output_tokens"] = total_output_tokens
                            metrics["content_blocks"] = content_block_idx
                            metrics["tool_calls_count"] = tool_calls_in_stream
                            if litellm_model_id:
                                metrics["litellm_model_id"] = litellm_model_id
                            if t_ttfb and not ttfb_recorded:
                                metrics["ttfb_ms"] = int((time.time() - t_ttfb) * 1000)
                                ttfb_recorded = True
                            _log("STREAM-DONE", f"finish={finish_reason} out={total_output_tokens}")
                            return {"ok": True, "already_sent": True}

                        try:
                            oai_chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Extract usage data
                        chunk_usage = oai_chunk.get("usage")
                        if chunk_usage:
                            if chunk_usage.get("prompt_tokens"):
                                input_tokens = chunk_usage["prompt_tokens"]
                            if chunk_usage.get("completion_tokens"):
                                total_output_tokens = chunk_usage["completion_tokens"]

                        # Capture model/deployment info from LiteLLM streaming response
                        chunk_model = oai_chunk.get("model", "")
                        if chunk_model and not litellm_model_id:
                            litellm_model_id = chunk_model

                        choices = oai_chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        chunk_finish = choices[0].get("finish_reason")
                        if chunk_finish:
                            finish_reason = chunk_finish

                        # Handle reasoning_content delta (GLM-5.1 built-in thinking mode)
                        # GLM-5.1 streams reasoning in delta.reasoning_content, content in delta.content
                        reasoning_delta = delta.get("reasoning_content")
                        if reasoning_delta is not None and reasoning_delta != "" and ENABLE_THINKING_MODE:
                            if not in_thinking_block:
                                # Close any open text/tool block before starting thinking
                                if in_text_block:
                                    self._send_sse("content_block_stop", {
                                        "type": "content_block_stop",
                                        "index": content_block_idx - 1,
                                    })
                                    in_text_block = False
                                if in_tool_block:
                                    self._send_sse("content_block_stop", {
                                        "type": "content_block_stop",
                                        "index": content_block_idx - 1,
                                    })
                                    in_tool_block = False
                                self._send_sse("content_block_start", {
                                    "type": "content_block_start",
                                    "index": content_block_idx,
                                    "content_block": {
                                        "type": "thinking",
                                        "thinking": "",
                                        "signature": THINKING_SIGNATURE,
                                    },
                                })
                                content_block_idx += 1
                                in_thinking_block = True
                            self._send_sse("content_block_delta", {
                                "type": "content_block_delta",
                                "index": content_block_idx - 1,
                                "delta": {"type": "thinking_delta", "thinking": reasoning_delta},
                            })
                        # Empty reasoning_content means thinking phase ended — close thinking block
                        # This happens when: (1) content delta follows, or (2) tool_calls follow
                        elif reasoning_delta == "" and in_thinking_block and (delta.get("content") or delta.get("tool_calls")):
                            self._send_sse("content_block_stop", {
                                "type": "content_block_stop",
                                "index": content_block_idx - 1,
                            })
                            in_thinking_block = False

                        # Handle text delta
                        text_delta = delta.get("content")
                        if text_delta is not None and text_delta != "":
                            if not in_text_block:
                                # Start a new text block
                                if in_tool_block:
                                    self._send_sse("content_block_stop", {
                                        "type": "content_block_stop",
                                        "index": content_block_idx - 1,
                                    })
                                    in_tool_block = False
                                self._send_sse("content_block_start", {
                                    "type": "content_block_start",
                                    "index": content_block_idx,
                                    "content_block": {"type": "text", "text": ""},
                                })
                                content_block_idx += 1
                                in_text_block = True
                            self._send_sse("content_block_delta", {
                                "type": "content_block_delta",
                                "index": content_block_idx - 1,
                                "delta": {"type": "text_delta", "text": text_delta},
                            })

                        # Handle tool call delta
                        tool_calls_delta = delta.get("tool_calls")
                        if tool_calls_delta:
                            for tc_delta in tool_calls_delta:
                                tc_idx = tc_delta.get("index", 0)
                                tc_function = tc_delta.get("function", {})

                                # Tool call start (has name and id)
                                if tc_delta.get("id"):
                                    tool_calls_in_stream += 1
                                    # Close any open text or thinking block
                                    if in_text_block:
                                        self._send_sse("content_block_stop", {
                                            "type": "content_block_stop",
                                            "index": content_block_idx - 1,
                                        })
                                        in_text_block = False
                                    if in_thinking_block:
                                        self._send_sse("content_block_stop", {
                                            "type": "content_block_stop",
                                            "index": content_block_idx - 1,
                                        })
                                        in_thinking_block = False

                                    current_tool_id = tc_delta["id"]
                                    current_tool_name = tc_function.get("name", "")
                                    tool_args_buffer = ""

                                    self._send_sse("content_block_start", {
                                        "type": "content_block_start",
                                        "index": content_block_idx,
                                        "content_block": {
                                            "type": "tool_use",
                                            "id": current_tool_id,
                                            "name": current_tool_name,
                                            "input": {},
                                        }
                                    })
                                    content_block_idx += 1
                                    in_tool_block = True

                                # Tool call arguments delta
                                args_delta = tc_function.get("arguments", "")
                                if args_delta:
                                    self._send_sse("content_block_delta", {
                                        "type": "content_block_delta",
                                        "index": content_block_idx - 1,
                                        "delta": {"type": "input_json_delta", "partial_json": args_delta},
                                    })
                                    tool_args_buffer += args_delta

                        # Handle empty content (model might send None for content)
                        # This happens when tool_calls are present but content is null

                    # Ignore other SSE lines (event:, comments, etc.)

            # If we exit the loop without [DONE], close gracefully
            if in_text_block or in_tool_block or in_thinking_block:
                self._send_sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": content_block_idx - 1,
                })
            self._send_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": total_output_tokens},
            })
            self._send_sse("message_stop", {"type": "message_stop"})
            self.wfile.flush()
            entry["status"] = 200

        except http.client.RemoteDisconnected:
            _log("WARN", "upstream disconnected during streaming")
            entry["status"] = 502
            entry["error"] = "RemoteDisconnected"
            metrics["status"] = 502; metrics["error_type"] = "RemoteDisconnected"; metrics["error_message"] = "upstream disconnected"
            # Log for root-cause analysis
            _log_error_detail({
                "request_id": metrics.get("request_id", "?"),
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": "RemoteDisconnected",
                "upstream_status": 502,
                "upstream_headers": {},
                "upstream_error_body_full": "RemoteDisconnected during streaming",
                "litellm_detail": {},
                "request_model": request_model,
                "mapped_model": mapped_model,
                "stream": True,
                "num_messages": metrics.get("num_messages", 0),
                "num_tools": metrics.get("num_tools", 0),
                "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                "proxy_attempt": metrics.get("proxy_retry", 0),
                "total_input_chars": metrics.get("total_input_chars", 0),
            })
            # If headers were already sent to client, the stream is broken — can't retry
            # If headers were NOT sent yet, return error dict for retry
            if not self._headers_sent:
                return {"ok": False, "status": 502, "error_body": json.dumps({"error": {"message": "upstream disconnected"}}).encode(),
                        "error_text": "upstream disconnected", "is_stream": True}
        except Exception as e:
            _log("ERROR", f"stream error: {type(e).__name__}: {e}")
            traceback.print_exc()
            entry["status"] = 502
            entry["error"] = f"{type(e).__name__}: {e}"
            metrics["status"] = 502; metrics["error_type"] = type(e).__name__; metrics["error_message"] = str(e)[:500]
            # Log for root-cause analysis
            _log_error_detail({
                "request_id": metrics.get("request_id", "?"),
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": type(e).__name__,
                "upstream_status": 502,
                "upstream_headers": {},
                "upstream_error_body_full": str(e),
                "litellm_detail": {},
                "request_model": request_model,
                "mapped_model": mapped_model,
                "stream": True,
                "num_messages": metrics.get("num_messages", 0),
                "num_tools": metrics.get("num_tools", 0),
                "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                "proxy_attempt": metrics.get("proxy_retry", 0),
                "total_input_chars": metrics.get("total_input_chars", 0),
                "transport_exception": traceback.format_exc()[:2000],
            })
            if not self._headers_sent:
                return {"ok": False, "status": 502, "error_body": json.dumps({"error": {"message": str(e)}}).encode(),
                        "error_text": str(e)[:500], "is_stream": True}
        finally:
            conn.close()

        # Stream completed successfully — response already sent to client
        return {"ok": True, "already_sent": True}

    # ─── Models endpoint ────────────────────────────────────────────────

    def _proxy_models(self):
        """Aggregate /v1/models from all upstream LiteLLM instances."""
        anth_models = {
            "object": "list",
            "data": []
        }
        seen_ids = set()

        for model_key, upstream in MODEL_UPSTREAMS.items():
            parsed = urllib.parse.urlparse(upstream["models_url"])
            conn = self._make_upstream_conn(parsed)

            try:
                headers_out = {"Authorization": f"Bearer {LITELLM_KEY}"}
                path = parsed.path or "/v1/models"
                conn.request("GET", path, headers=headers_out)
                resp = conn.getresponse()

                if resp.status != 200:
                    conn.close()
                    continue

                resp_body = resp.read().decode("utf-8")
                oai_models = json.loads(resp_body)

                for m in oai_models.get("data", []):
                    model_id = m.get("id", "")
                    if model_id not in seen_ids:
                        seen_ids.add(model_id)
                        # Map model_id to upstream key for context_length
                        upstream_key = MODEL_MAP.get(model_id, model_id)
                        context_len = MODEL_MAX_INPUT_TOKENS.get(upstream_key, 131072)
                        anth_models["data"].append({
                            "id": model_id,
                            "object": "model",
                            "created": m.get("created", 0),
                            "owned_by": m.get("owned_by", ""),
                            "display_name": model_id,
                            "context_length": context_len,
                        })
            except Exception as e:
                _log("ERROR", f"models proxy error for {model_key}: {e}")
            finally:
                conn.close()

        self._send_json(200, anth_models)

    # ─── Responses API handler ──────────────────────────────────────────

    def _handle_responses(self):
        """Handle OpenAI Responses API requests by converting to Chat Completions,
        forwarding to LiteLLM, then converting back to Responses format."""
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            resp_body = json.loads(raw_body)
        except Exception as e:
            self._send_json(400, {"error": {"message": f"bad request: {e}"}})
            _log("ERROR", f"responses bad request: {e}")
            return

        request_model = resp_body.get("model", DEFAULT_MODEL)
        is_stream = resp_body.get("stream", False)
        _log("RESP-API", f"model={request_model} stream={is_stream} "
                         f"tools={len(resp_body.get('tools', []))}")

        # Convert Responses → Chat Completions
        oai_body = responses_to_chat(resp_body)
        mapped_model = oai_body.get("model", DEFAULT_MODEL)
        oai_data = json.dumps(oai_body).encode("utf-8")

        # Select upstream
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]

        auth_key = self.headers.get("Authorization", "")
        if auth_key.startswith("Bearer "):
            auth_key = auth_key[7:]
        else:
            auth_key = LITELLM_KEY
        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_key}",
            "Content-Length": str(len(oai_data)),
        }

        parsed_upstream = urllib.parse.urlparse(litellm_url)

        if is_stream:
            # Streaming: convert Chat Completions SSE → Responses SSE
            self._stream_responses(oai_data, headers_out, parsed_upstream, request_model)
        else:
            # Non-streaming with null-choices retry
            NULL_CHOICES_MAX_RETRIES = 3
            for attempt in range(1, NULL_CHOICES_MAX_RETRIES + 1):
                conn = self._make_upstream_conn(parsed_upstream)
                path = parsed_upstream.path or "/v1/chat/completions"
                try:
                    conn.request("POST", path, body=oai_data, headers=headers_out)
                    resp = conn.getresponse()
                    if resp.status != 200:
                        err_body = resp.read()
                        err_text = err_body.decode()[:500]
                        if attempt < NULL_CHOICES_MAX_RETRIES and "Invalid response object" in err_text and "choices" in err_text:
                            _log("RETRY", f"responses null-choices attempt {attempt}/{NULL_CHOICES_MAX_RETRIES}")
                            time.sleep(2)
                            conn.close()
                            continue
                        self._send_json(resp.status, {"error": {"message": err_text}})
                        _log("ERROR", f"responses upstream {resp.status}: {err_text[:200]}")
                        return

                    resp_data = resp.read().decode("utf-8")
                    oai_response = json.loads(resp_data)
                    responses_response = chat_to_responses(oai_response, request_model)
                    self._send_json(200, responses_response)
                    _log("RESP-API-DONE", f"status=200 model={request_model} "
                                          f"dur={int((time.time()-t_start)*1000)}ms")
                    return
                except Exception as e:
                    _log("ERROR", f"responses transport error: {type(e).__name__}: {e}")
                    self._send_json(502, {"error": {"message": f"Transport error: {type(e).__name__}: {e}"}})
                    return
                finally:
                    conn.close()
            self._send_json(502, {"error": {"message": "All retries exhausted: null-choices error from upstream"}})

    def _stream_responses(self, oai_data, headers_out, parsed_upstream, request_model):
        """Stream Chat Completions SSE and convert to Responses API SSE format."""
        conn = self._make_upstream_conn(parsed_upstream)
        path = parsed_upstream.path or "/v1/chat/completions"

        try:
            conn.request("POST", path, body=oai_data, headers=headers_out)
            resp = conn.getresponse()

            if resp.status != 200:
                err_body = resp.read()
                err_text = err_body.decode()[:500]
                self._send_json(resp.status, {"error": {"message": err_text}})
                _log("ERROR", f"responses stream upstream {resp.status}: {err_text[:200]}")
                return

            # Send SSE headers
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.close_connection = True

            resp_id = f"resp_{int(time.time()*1000)}"
            current_text = ""
            total_output_tokens = 0
            input_tokens = 0
            finish_reason = None
            function_calls = {}  # call_id → {name, arguments_buffer}
            item_index = 0
            text_started = False

            # Emit response.created event
            self._send_sse("response.created", {
                "type": "response.created",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "model": request_model,
                    "status": "in_progress",
                    "output": [],
                    "created_at": int(time.time()),
                }
            })

            buffer = ""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        oai_chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Extract usage
                    chunk_usage = oai_chunk.get("usage")
                    if chunk_usage:
                        if chunk_usage.get("prompt_tokens"):
                            input_tokens = chunk_usage["prompt_tokens"]
                        if chunk_usage.get("completion_tokens"):
                            total_output_tokens = chunk_usage["completion_tokens"]

                    choices = oai_chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    chunk_finish = choices[0].get("finish_reason")
                    if chunk_finish:
                        finish_reason = chunk_finish

                    # Text delta → output_text.delta
                    text_delta = delta.get("content")
                    if text_delta is not None and text_delta != "":
                        if not text_started:
                            self._send_sse("response.output_item.added", {
                                "type": "response.output_item.added",
                                "output_index": item_index,
                                "item": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                }
                            })
                            self._send_sse("response.content_part.added", {
                                "type": "response.content_part.added",
                                "output_index": item_index,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": ""},
                            })
                            text_started = True
                            current_text = ""
                        current_text += text_delta
                        self._send_sse("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "output_index": item_index,
                            "content_index": 0,
                            "delta": text_delta,
                        })

                    # Function call delta
                    tool_calls_delta = delta.get("tool_calls")
                    if tool_calls_delta:
                        for tc_delta in tool_calls_delta:
                            call_id = tc_delta.get("id", "")
                            tc_function = tc_delta.get("function", {})

                            if call_id:
                                # New function call
                                fc_name = tc_function.get("name", "")
                                function_calls[call_id] = {
                                    "name": fc_name,
                                    "arguments_buffer": "",
                                }
                                self._send_sse("response.output_item.added", {
                                    "type": "response.output_item.added",
                                    "output_index": item_index,
                                    "item": {
                                        "type": "function_call",
                                        "id": call_id,
                                        "call_id": call_id,
                                        "name": fc_name,
                                        "arguments": "",
                                    }
                                })
                                # If text block was open, close it first
                                if text_started:
                                    self._send_sse("response.content_part.done", {
                                        "type": "response.content_part.done",
                                        "output_index": item_index - 1,
                                        "content_index": 0,
                                        "part": {"type": "output_text", "text": current_text},
                                    })
                                    self._send_sse("response.output_item.done", {
                                        "type": "response.output_item.done",
                                        "output_index": item_index - 1,
                                        "item": {
                                            "type": "message",
                                            "role": "assistant",
                                            "content": [{"type": "output_text", "text": current_text}],
                                        }
                                    })
                                    text_started = False
                                    item_index += 1
                                item_index += 1

                            # Arguments delta
                            args_delta = tc_function.get("arguments", "")
                            if args_delta and call_id in function_calls:
                                function_calls[call_id]["arguments_buffer"] += args_delta
                                self._send_sse("response.function_call_arguments.delta", {
                                    "type": "response.function_call_arguments.delta",
                                    "output_index": item_index,
                                    "call_id": call_id,
                                    "delta": args_delta,
                                })

            # Close any open content
            if text_started:
                self._send_sse("response.content_part.done", {
                    "type": "response.content_part.done",
                    "output_index": item_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": current_text},
                })
                self._send_sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": item_index,
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": current_text}],
                    }
                })

            # Close function call items
            for call_id, fc_data in function_calls.items():
                fc_idx = list(function_calls.keys()).index(call_id)
                self._send_sse("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "output_index": item_index + fc_idx,
                    "call_id": call_id,
                    "arguments": fc_data["arguments_buffer"],
                })
                self._send_sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": item_index + fc_idx,
                    "item": {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": fc_data["name"],
                        "arguments": fc_data["arguments_buffer"],
                    }
                })

            # Determine final status
            status = "completed"
            if finish_reason == "length":
                status = "incomplete"

            self._send_sse("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "model": request_model,
                    "status": status,
                    "output": [],
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": total_output_tokens,
                        "total_tokens": input_tokens + total_output_tokens,
                    },
                    "created_at": int(time.time()),
                    "completed_at": int(time.time()),
                    "metadata": {},
                }
            })
            self.wfile.flush()
            _log("RESP-API-STREAM-DONE", f"finish={finish_reason} out={total_output_tokens}")

        except http.client.RemoteDisconnected:
            _log("WARN", "responses upstream disconnected during streaming")
        except Exception as e:
            _log("ERROR", f"responses stream error: {type(e).__name__}: {e}")
            if not self._headers_sent:
                self._send_json(502, {"error": {"message": f"Transport error: {type(e).__name__}: {e}"}})
        finally:
            conn.close()

    # ─── OpenAI passthrough ────────────────────────────────────────────

    def _passthrough_openai(self):
        """Passthrough for requests already in OpenAI format.
        Routes to the default upstream (glm5.1)."""
        default_upstream = MODEL_UPSTREAMS[DEFAULT_UPSTREAM_MODEL]
        parsed_upstream = urllib.parse.urlparse(default_upstream["chat_url"])
        conn = self._make_upstream_conn(parsed_upstream)

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            auth_key = self.headers.get("Authorization") or f"Bearer {LITELLM_KEY}"
            headers_out = {
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Authorization": auth_key,
                "Content-Length": str(len(body)),
            }
            path = parsed_upstream.path or "/v1/chat/completions"
            conn.request("POST", path, body=body, headers=headers_out)
            resp = conn.getresponse()
            resp_body = resp.read()
            self._send_raw(resp.status, resp_body, resp.getheader("Content-Type", "application/json"))
        except Exception as e:
            self._send_json(502, {"error": {"message": f"{type(e).__name__}: {e}"}})
        finally:
            conn.close()

    # ─── Helper methods ─────────────────────────────────────────────────

    def _make_upstream_conn(self, parsed_url):
        use_https = parsed_url.scheme == "https"
        if use_https:
            import ssl
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(
                parsed_url.hostname,
                parsed_url.port or 443,
                timeout=PROXY_TIMEOUT,
                context=ctx,
            )
        return http.client.HTTPConnection(
            parsed_url.hostname,
            parsed_url.port or 80,
            timeout=PROXY_TIMEOUT,
        )

    def _send_anth_error(self, status_code, error_text):
        """Send an Anthropic-format error as SSE stream."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            # Anthropic SDK expects error in the stream
            self._send_sse("error", {
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream error {status_code}: {error_text[:200]}"},
            })
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _log("WARN", f"Client disconnected before error could be sent (status={status_code})")

    def _send_sse(self, event_type, data_dict):
        payload = f"event: {event_type}\ndata: {json.dumps(data_dict, ensure_ascii=False)}\n\n".encode("utf-8")
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_raw(self, code, body_bytes, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass  # Suppress default logging


# ─── Main ──────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    _log("START", f"Anthropic→OpenAI converter proxy on {LISTEN_HOST}:{LISTEN_PORT}")
    for model_key, upstream in MODEL_UPSTREAMS.items():
        _log("START", f"  {model_key}: {upstream['chat_url']}")
    _log("START", f"API key: {LITELLM_KEY[:8]}...")
    _log("START", f"MAX_TOOL_DESC={MAX_TOOL_DESC}, MAX_SCHEMA_DESC={MAX_SCHEMA_DESC}")
    _log("START", f"Timeout: {PROXY_TIMEOUT}s")

    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("SHUTDOWN", "Interrupted")
        server.shutdown()


if __name__ == "__main__":
    main()