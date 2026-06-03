from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import time
from typing import Any
from uuid import uuid4

from .config import Settings

UNSUPPORTED_IMAGE_NOTE = "[image input omitted: upstream DeepSeek chat API only supports text-only user content.]"
JSON_SCHEMA_DOWNGRADE_NOTE = (
    "Return only one valid JSON object with no markdown fences or extra prose. "
    "Follow this JSON schema exactly:"
)
ALLOWED_CHAT_ROLES = {"system", "user", "assistant", "tool", "latest_reminder"}
ROLE_ALIASES = {
    "developer": "system",
    "function": "tool",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def sanitize_tool_name(name: str | None) -> str:
    raw = str(name or "tool").strip() or "tool"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    sanitized = sanitized[:64] or "tool"
    if sanitized != raw or len(raw) > 64:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        base = sanitized[: max(1, 64 - 9)]
        sanitized = f"{base}_{digest}"
    return sanitized


def normalize_message_role(role: Any, default: str = "user") -> str:
    fallback = default if default in ALLOWED_CHAT_ROLES else "user"
    if role is None:
        return fallback

    normalized = str(role).strip().lower()
    if not normalized:
        return fallback

    normalized = ROLE_ALIASES.get(normalized, normalized)
    if normalized in ALLOWED_CHAT_ROLES:
        return normalized
    return fallback


def normalize_chat_messages_for_upstream(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages

    normalized_messages: list[Any] = []
    for item in messages:
        if not isinstance(item, dict):
            normalized_messages.append(item)
            continue
        normalized_item = deepcopy(item)
        normalized_item["role"] = normalize_message_role(item.get("role"), "user")
        normalized_messages.append(normalized_item)
    return normalized_messages


def render_tool_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (int, float, bool)):
        return json.dumps(output)
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                chunks.append(json.dumps(item, ensure_ascii=False))
                continue
            item_type = item.get("type")
            if item_type in {"input_text", "output_text", "text"}:
                chunks.append(str(item.get("text", "")))
            else:
                chunks.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(part for part in chunks if part)
    return json.dumps(output, ensure_ascii=False)


def _content_part_to_chat_part(part: Any) -> list[dict[str, Any]]:
    if isinstance(part, str):
        return [{"type": "text", "text": part}]
    if not isinstance(part, dict):
        return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)}]

    part_type = part.get("type")
    if part_type in {"input_text", "output_text", "text"}:
        return [{"type": "text", "text": str(part.get("text", ""))}]
    if part_type in {"input_image", "image_url"}:
        # DeepSeek's chat/completions endpoint currently accepts text-only user content.
        # Downgrade image parts to a textual marker instead of forwarding image_url payloads.
        return [{"type": "text", "text": UNSUPPORTED_IMAGE_NOTE}]
    if part_type == "input_file":
        filename = part.get("filename") or part.get("file_id") or "file"
        return [{"type": "text", "text": f"[file input omitted: {filename}]"}]
    return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)}]


def content_to_chat_content(content: Any) -> str | list[dict[str, Any]]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = _content_part_to_chat_part(content)
        if len(parts) == 1 and parts[0]["type"] == "text":
            return parts[0]["text"]
        return parts

    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)

    chat_parts: list[dict[str, Any]] = []
    for part in content:
        chat_parts.extend(_content_part_to_chat_part(part))

    if chat_parts and all(part["type"] == "text" for part in chat_parts):
        return "\n".join(part["text"] for part in chat_parts if part["text"])

    return chat_parts


def _custom_tool_schema(description: str | None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": description or "Plain-text input for the tool.",
            }
        },
        "required": ["input"],
        "additionalProperties": False,
    }


def _normalize_function_tool(tool: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    if "function" in tool and isinstance(tool["function"], dict):
        function_def = deepcopy(tool["function"])
    else:
        function_def = {
            "name": tool.get("name"),
            "description": tool.get("description"),
            "parameters": tool.get("parameters"),
            "strict": tool.get("strict"),
        }

    original_name = str(function_def.get("name") or "tool")
    sanitized_name = sanitize_tool_name(original_name)
    normalized = {"type": "function", "function": {"name": sanitized_name}}
    if function_def.get("description") is not None:
        normalized["function"]["description"] = function_def["description"]
    if function_def.get("parameters") is not None:
        normalized["function"]["parameters"] = function_def["parameters"]
    if function_def.get("strict") is not None:
        normalized["function"]["strict"] = function_def["strict"]
    return normalized, original_name, sanitized_name


def normalize_tools(
    tools: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, str]]:
    if not tools:
        return [], {}, {}

    normalized: list[dict[str, Any]] = []
    tool_meta: dict[str, dict[str, str]] = {}
    original_to_sanitized: dict[str, str] = {}

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        tool_type = tool.get("type", "function")
        if tool_type == "function":
            normalized_tool, original_name, sanitized_name = _normalize_function_tool(tool)
            normalized.append(normalized_tool)
            tool_meta[sanitized_name] = {"kind": "function", "original_name": original_name}
            original_to_sanitized[original_name] = sanitized_name
            continue

        original_name = str(tool.get("name") or "").strip()
        if not original_name:
            continue
        sanitized_name = sanitize_tool_name(original_name)

        description = tool.get("description")
        normalized.append(
            {
                "type": "function",
                "function": {
                    "name": sanitized_name,
                    "description": description,
                    "parameters": _custom_tool_schema(description),
                },
            }
        )
        tool_meta[sanitized_name] = {"kind": tool_type, "original_name": original_name}
        original_to_sanitized[original_name] = sanitized_name

    return normalized, tool_meta, original_to_sanitized


def _filter_tools_for_allowed_names(
    tools: list[dict[str, Any]],
    tool_meta: dict[str, dict[str, str]],
    names: set[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    filtered_tools: list[dict[str, Any]] = []
    filtered_meta: dict[str, dict[str, str]] = {}
    for tool in tools:
        name = tool.get("function", {}).get("name")
        if name in names:
            filtered_tools.append(tool)
            filtered_meta[name] = tool_meta[name]
    return filtered_tools, filtered_meta


def map_tool_choice(
    tool_choice: Any,
    chat_tools: list[dict[str, Any]],
    tool_meta: dict[str, dict[str, str]],
    original_to_sanitized: dict[str, str],
) -> tuple[Any, list[dict[str, Any]], dict[str, dict[str, str]]]:
    if tool_choice is None:
        return None, chat_tools, tool_meta
    if isinstance(tool_choice, str):
        return tool_choice, chat_tools, tool_meta
    if not isinstance(tool_choice, dict):
        return None, chat_tools, tool_meta

    choice_type = tool_choice.get("type")
    if choice_type == "function":
        name = tool_choice.get("name") or tool_choice.get("function", {}).get("name")
        if not name:
            return None, chat_tools, tool_meta
        mapped_name = original_to_sanitized.get(str(name), sanitize_tool_name(str(name)))
        return {"type": "function", "function": {"name": mapped_name}}, chat_tools, tool_meta

    if choice_type == "allowed_tools":
        allowed = {
            original_to_sanitized.get(str(entry.get("name")), sanitize_tool_name(str(entry.get("name"))))
            for entry in tool_choice.get("tools", [])
            if isinstance(entry, dict) and entry.get("name")
        }
        filtered_tools, filtered_meta = _filter_tools_for_allowed_names(chat_tools, tool_meta, allowed)
        mode = tool_choice.get("mode", "auto")
        return mode, filtered_tools, filtered_meta

    return None, chat_tools, tool_meta


def response_input_to_chat_messages(
    input_value: Any,
    instructions: str | None = None,
    tool_name_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    tool_name_map = tool_name_map or {}

    if input_value is None:
        messages: list[dict[str, Any]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        return messages

    normalized_items: list[Any]
    if isinstance(input_value, str):
        normalized_items = [{"role": "user", "content": input_value}]
    elif isinstance(input_value, list):
        normalized_items = list(input_value)
    else:
        normalized_items = [{"role": "user", "content": render_tool_output(input_value)}]

    responded_call_ids: set[str] = set()
    for item in normalized_items:
        if isinstance(item, dict) and item.get("type") in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(item.get("call_id") or "")
            if call_id:
                responded_call_ids.add(call_id)

    def build_tool_call(item: dict[str, Any]) -> dict[str, Any]:
        original_name = str(item.get("name", "tool"))
        mapped_name = tool_name_map.get(original_name, sanitize_tool_name(original_name))
        arguments = item.get("arguments", "{}")
        if item.get("type") == "custom_tool_call":
            arguments = json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
        elif not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        return {
            "id": item.get("call_id", new_id("call")),
            "type": "function",
            "function": {
                "name": mapped_name,
                "arguments": arguments,
            },
        }

    messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    def flush_pending_tool_calls() -> None:
        if not pending_tool_calls:
            return
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": deepcopy(pending_tool_calls),
            }
        )
        pending_tool_calls.clear()

    for item in normalized_items:
        if isinstance(item, str):
            flush_pending_tool_calls()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            flush_pending_tool_calls()
            messages.append({"role": "user", "content": json.dumps(item, ensure_ascii=False)})
            continue

        item_type = item.get("type")
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            flush_pending_tool_calls()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id", new_id("call")),
                    "content": render_tool_output(item.get("output")),
                }
            )
            continue

        if item_type == "function_call":
            tc = build_tool_call(item)
            if tc["id"] in responded_call_ids:
                pending_tool_calls.append(tc)
            continue

        if item_type == "custom_tool_call":
            tc = build_tool_call(item)
            if tc["id"] in responded_call_ids:
                pending_tool_calls.append(tc)
            continue

        if item_type == "reasoning":
            continue

        role = item.get("role")
        if role or item_type == "message":
            flush_pending_tool_calls()
            normalized_role = normalize_message_role(role, "user")
            message: dict[str, Any] = {
                "role": normalized_role,
                "content": content_to_chat_content(item.get("content")),
            }
            if normalized_role == "tool" and item.get("tool_call_id"):
                message["tool_call_id"] = item.get("tool_call_id")
            messages.append(message)
            continue

        flush_pending_tool_calls()
        messages.append({"role": "user", "content": render_tool_output(item)})

    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})

    return messages


def map_text_format(text_config: Any) -> dict[str, Any] | None:
    if not isinstance(text_config, dict):
        return None
    format_config = text_config.get("format")
    if not isinstance(format_config, dict):
        return None

    format_type = format_config.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type != "json_schema":
        return None
    # DeepSeek currently rejects json_schema here with:
    # "This response_format type is unavailable now".
    # Downgrade to json_object and move the schema itself into instructions.
    return {"type": "json_object"}


def build_text_format_guidance(text_config: Any) -> str | None:
    if not isinstance(text_config, dict):
        return None
    format_config = text_config.get("format")
    if not isinstance(format_config, dict):
        return None
    if format_config.get("type") != "json_schema":
        return None

    schema = format_config.get("schema") or format_config.get("json_schema") or {}
    if not schema:
        return "Return only one valid JSON object with no markdown fences or extra prose."
    return f"{JSON_SCHEMA_DOWNGRADE_NOTE}\n{json.dumps(schema, ensure_ascii=False)}"


def normalize_reasoning_effort(payload: dict[str, Any]) -> str | None:
    reasoning_effort = payload.get("reasoning_effort")
    reasoning_block = payload.get("reasoning")

    if reasoning_effort is None and isinstance(reasoning_block, dict):
        reasoning_effort = reasoning_block.get("effort")

    if reasoning_effort is None:
        return None

    normalized = str(reasoning_effort).strip().lower()
    return normalized or None


def model_uses_default_thinking(model_name: str | None) -> bool:
    normalized = str(model_name or "").strip().lower()
    return normalized.startswith("deepseek-v4-")


def apply_reasoning_preferences(
    payload: dict[str, Any],
    chat_request: dict[str, Any],
) -> None:
    normalized = normalize_reasoning_effort(payload)
    if normalized is None:
        return

    if normalized == "none":
        chat_request["thinking"] = {"type": "disabled"}
        chat_request.pop("reasoning_effort", None)
        return

    effort_map = {
        "minimal": "high",
        "low": "high",
        "medium": "high",
        "high": "high",
        "xhigh": "max",
        "max": "max",
    }
    mapped = effort_map.get(normalized)
    if not mapped:
        return

    chat_request["thinking"] = {"type": "enabled"}
    chat_request["reasoning_effort"] = mapped


def has_tool_execution_context(input_value: Any) -> bool:
    if not isinstance(input_value, list):
        return False
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"}:
            return True
    return False


def response_request_to_chat_request(
    payload: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    chat_tools, tool_meta, original_to_sanitized = normalize_tools(payload.get("tools"))
    chat_tool_choice, chat_tools, tool_meta = map_tool_choice(
        payload.get("tool_choice"),
        chat_tools,
        tool_meta,
        original_to_sanitized,
    )

    instructions = payload.get("instructions")
    text_format_guidance = build_text_format_guidance(payload.get("text"))
    if text_format_guidance:
        instructions = (
            f"{instructions}\n\n{text_format_guidance}"
            if instructions
            else text_format_guidance
        )

    chat_request: dict[str, Any] = {
        "model": settings.upstream_model,
        "messages": response_input_to_chat_messages(
            payload.get("input"),
            instructions,
            original_to_sanitized,
        ),
        "stream": False,
    }

    if payload.get("temperature") is not None:
        chat_request["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        chat_request["top_p"] = payload["top_p"]
    if payload.get("max_output_tokens") is not None:
        chat_request["max_tokens"] = payload["max_output_tokens"]
    if payload.get("parallel_tool_calls") is not None:
        chat_request["parallel_tool_calls"] = payload["parallel_tool_calls"]
    if payload.get("stop") is not None:
        chat_request["stop"] = payload["stop"]
    if payload.get("metadata") is not None:
        chat_request["metadata"] = payload["metadata"]

    input_has_tool_context = has_tool_execution_context(payload.get("input"))
    explicit_reasoning = normalize_reasoning_effort(payload)

    if not chat_tools and not input_has_tool_context:
        apply_reasoning_preferences(payload, chat_request)
    elif model_uses_default_thinking(settings.upstream_model):
        # DeepSeek V4 defaults to thinking mode. For tool-heavy replayed conversations,
        # that requires reasoning_content passthrough we do not fully preserve yet.
        chat_request["thinking"] = {"type": "disabled"}
        chat_request.pop("reasoning_effort", None)

    if (
        "thinking" not in chat_request
        and explicit_reasoning is None
        and model_uses_default_thinking(settings.upstream_model)
    ):
        chat_request["thinking"] = {"type": "disabled"}

    response_format = map_text_format(payload.get("text"))
    if response_format is not None:
        chat_request["response_format"] = response_format

    if chat_tools:
        chat_request["tools"] = chat_tools
    if chat_tool_choice is not None:
        chat_request["tool_choice"] = chat_tool_choice

    return chat_request, tool_meta


def _usage_from_chat(chat_response: dict[str, Any]) -> dict[str, int]:
    usage = chat_response.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _text_message_output(text: str) -> dict[str, Any]:
    return {
        "id": new_id("msg"),
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
    }


def _extract_custom_input(arguments: str) -> str:
    try:
        payload = json.loads(arguments)
    except Exception:
        return arguments
    if isinstance(payload, dict) and "input" in payload:
        value = payload["input"]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return arguments


def chat_completion_to_response(
    chat_response: dict[str, Any],
    original_payload: dict[str, Any],
    tool_meta: dict[str, dict[str, str]],
) -> dict[str, Any]:
    choices = chat_response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content")
    tool_calls = message.get("tool_calls") or []

    output: list[dict[str, Any]] = []

    if isinstance(content, list):
        text_chunks: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_chunks.append(str(part.get("text", "")))
            elif isinstance(part, str):
                text_chunks.append(part)
        content = "\n".join(part for part in text_chunks if part)

    if content:
        output.append(_text_message_output(str(content)))

    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue

        function_block = tool_call.get("function") or {}
        upstream_name = function_block.get("name", "tool")
        tool_info = tool_meta.get(str(upstream_name), {})
        name = tool_info.get("original_name", upstream_name)
        call_id = tool_call.get("id") or new_id("call")
        arguments = function_block.get("arguments", "")
        item_id = new_id("fc")

        if tool_info.get("kind") == "custom":
            output.append(
                {
                    "id": item_id.replace("fc_", "ctc_", 1),
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "input": _extract_custom_input(arguments),
                }
            )
        else:
            output.append(
                {
                    "id": item_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )

    response = {
        "id": new_id("resp"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": original_payload.get("instructions"),
        "model": original_payload.get("model") or chat_response.get("model"),
        "output": output,
        "parallel_tool_calls": original_payload.get("parallel_tool_calls", True),
        "temperature": original_payload.get("temperature"),
        "tool_choice": original_payload.get("tool_choice", "auto" if original_payload.get("tools") else "none"),
        "tools": original_payload.get("tools", []),
        "top_p": original_payload.get("top_p"),
        "truncation": "disabled",
        "usage": _usage_from_chat(chat_response),
        "metadata": original_payload.get("metadata", {}),
    }
    return response


def iter_text_chunks(text: str, chunk_size: int) -> list[str]:
    if not text:
        return []
    return [text[index:index + chunk_size] for index in range(0, len(text), chunk_size)]
