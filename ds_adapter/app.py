from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import Settings
from .translator import (
    chat_completion_to_response,
    iter_text_chunks,
    new_id,
    normalize_chat_messages_for_upstream,
    response_request_to_chat_request,
)

TRACE_LOG_PATH = Path(__file__).resolve().parents[1] / "adapter_request_trace.jsonl"

DEEPSEEK_ERROR_GUIDE: dict[int, dict[str, str]] = {
    400: {
        "title": "格式错误",
        "cause": "请求体格式错误。",
        "solution": "请根据错误信息提示修改请求体。",
    },
    401: {
        "title": "认证失败",
        "cause": "API Key 错误，认证失败。",
        "solution": "请检查您的 API Key 是否正确；如没有 API Key，请先创建 API Key。",
    },
    402: {
        "title": "余额不足",
        "cause": "账号余额不足。",
        "solution": "请确认账户余额，并前往 DeepSeek 充值页面进行充值。",
    },
    422: {
        "title": "参数错误",
        "cause": "请求体参数错误。",
        "solution": "请根据错误信息提示修改相关参数。",
    },
    429: {
        "title": "请求速率达到上限",
        "cause": "请求速率（TPM 或 RPM）达到上限。",
        "solution": "请合理规划您的请求速率。",
    },
    500: {
        "title": "服务器故障",
        "cause": "DeepSeek 服务器内部故障。",
        "solution": "请等待后重试。若问题一直存在，请联系 DeepSeek 解决。",
    },
    503: {
        "title": "服务器繁忙",
        "cause": "DeepSeek 服务器当前负载过高。",
        "solution": "请稍后重试您的请求。",
    },
}


class UpstreamError(RuntimeError):
    def __init__(self, status_code: int, message: str, error_type: str = "upstream_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type


def openai_error_response(status_code: int, message: str, error_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": status_code,
            }
        },
    )


def parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_model_ids(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return fallback
    if isinstance(value, list):
        parsed = tuple(str(item).strip() for item in value if str(item).strip())
        return parsed or fallback
    parsed = tuple(part.strip() for part in str(value).split(",") if part.strip())
    return parsed or fallback


def _extract_preview_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, list):
        return None

    parts: list[str] = []
    for item in value[:3]:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                parts.append(stripped)
            continue
        if not isinstance(item, dict):
            continue

        role = str(item.get("role") or item.get("type") or "").strip()
        content = item.get("content")
        text: str | None = None
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"input_text", "output_text", "text"}:
                    candidate = str(part.get("text") or "").strip()
                    if candidate:
                        text = candidate
                        break
        if text:
            parts.append(f"{role}:{text}" if role else text)

    if not parts:
        return None
    return "\n".join(parts)[:240]


def append_request_trace(entry: dict[str, Any]) -> None:
    try:
        with TRACE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def trace_responses_request(payload: dict[str, Any], duration_ms: float, used_upstream: bool, outcome: str) -> None:
    tools = payload.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else 0
    original_tool_names = []
    if isinstance(tools, list):
        for tool in tools[:24]:
            if not isinstance(tool, dict):
                continue
            if isinstance(tool.get("function"), dict) and tool["function"].get("name"):
                original_tool_names.append(str(tool["function"]["name"]))
                continue
            if tool.get("name"):
                original_tool_names.append(str(tool["name"]))
    text_format_type = None
    if isinstance(payload.get("text"), dict) and isinstance(payload["text"].get("format"), dict):
        text_format_type = payload["text"]["format"].get("type")
    append_request_trace(
        {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "endpoint": "/v1/responses",
            "duration_ms": round(duration_ms, 2),
            "used_upstream": used_upstream,
            "outcome": outcome,
            "model": payload.get("model"),
            "stream": bool(payload.get("stream")),
            "tool_count": tool_count,
            "original_tool_names": original_tool_names,
            "has_instructions": bool(payload.get("instructions")),
            "has_reasoning": bool(payload.get("reasoning") or payload.get("reasoning_effort")),
            "text_format_type": text_format_type,
            "input_preview": _extract_preview_text(payload.get("input")),
        }
    )


def trace_upstream_payload(chat_request: dict[str, Any], note: str) -> None:
    tools = chat_request.get("tools")
    tool_names = []
    if isinstance(tools, list):
        for tool in tools[:24]:
            if not isinstance(tool, dict):
                continue
            function_block = tool.get("function")
            if isinstance(function_block, dict):
                name = function_block.get("name")
                if name:
                    tool_names.append(str(name))
    append_request_trace(
        {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "endpoint": "upstream:/chat/completions",
            "note": note,
            "model": chat_request.get("model"),
            "stream": bool(chat_request.get("stream")),
            "tool_count": len(tools) if isinstance(tools, list) else 0,
            "has_response_format": "response_format" in chat_request,
            "response_format_type": (
                chat_request.get("response_format", {}).get("type")
                if isinstance(chat_request.get("response_format"), dict)
                else None
            ),
            "tool_names": tool_names,
            "has_thinking": "thinking" in chat_request,
            "has_reasoning_effort": "reasoning_effort" in chat_request,
            "message_preview": _extract_preview_text(chat_request.get("messages")),
        }
    )


def trace_chat_request(payload: dict[str, Any], duration_ms: float, used_upstream: bool, outcome: str) -> None:
    tools = payload.get("tools") or payload.get("functions")
    tool_count = len(tools) if isinstance(tools, list) else 0
    append_request_trace(
        {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "endpoint": "/v1/chat/completions",
            "duration_ms": round(duration_ms, 2),
            "used_upstream": used_upstream,
            "outcome": outcome,
            "model": payload.get("model"),
            "stream": bool(payload.get("stream")),
            "tool_count": tool_count,
            "message_preview": _extract_preview_text(payload.get("messages")),
        }
    )


def localize_deepseek_error(status_code: int, raw_message: str) -> str:
    guide = DEEPSEEK_ERROR_GUIDE.get(status_code)
    cleaned = " ".join(str(raw_message or "").split()).strip()
    if not guide:
        return cleaned or f"DeepSeek 返回 HTTP {status_code}。"

    lines = [
        f"DeepSeek 返回 {status_code}：{guide['title']}。",
        f"原因：{guide['cause']}",
        f"处理建议：{guide['solution']}",
    ]
    if cleaned:
        lines.append(f"原始错误：{cleaned}")
    return "\n".join(lines)


DEEPSEEK_ERROR_GUIDE_CLEAN: dict[int, dict[str, str]] = {
    400: {
        "title": "格式错误",
        "cause": "请求体格式错误。",
        "solution": "请根据错误信息提示修改请求体。",
    },
    401: {
        "title": "认证失败",
        "cause": "API Key 错误，认证失败。",
        "solution": "请检查您的 API Key 是否正确；如没有 API Key，请先创建 API Key。",
    },
    402: {
        "title": "余额不足",
        "cause": "账户余额不足。",
        "solution": "请确认账户余额，并前往 DeepSeek 充值页面进行充值。",
    },
    422: {
        "title": "参数错误",
        "cause": "请求体参数错误。",
        "solution": "请根据错误信息提示修改相关参数。",
    },
    429: {
        "title": "请求速率达到上限",
        "cause": "请求速率（TPM 或 RPM）达到上限。",
        "solution": "请合理规划您的请求速率。",
    },
    500: {
        "title": "服务器故障",
        "cause": "DeepSeek 服务器内部故障。",
        "solution": "请等待后重试。若问题一直存在，请联系 DeepSeek 解决。",
    },
    503: {
        "title": "服务器繁忙",
        "cause": "DeepSeek 服务器当前负载过高。",
        "solution": "请稍后重试您的请求。",
    },
}


def localize_deepseek_error(status_code: int, raw_message: str) -> str:
    guide = DEEPSEEK_ERROR_GUIDE_CLEAN.get(status_code)
    cleaned = " ".join(str(raw_message or "").split()).strip()
    if not guide:
        return cleaned or f"DeepSeek 返回 HTTP {status_code}。"

    lines = [
        f"DeepSeek 返回 {status_code}：{guide['title']}。",
        f"原因：{guide['cause']}",
        f"处理建议：{guide['solution']}",
    ]
    if cleaned:
        lines.append(f"原始错误：{cleaned}")
    return "\n".join(lines)


def build_settings_from_payload(current: Settings, payload: dict[str, Any]) -> Settings:
    return Settings(
        upstream_base_url=str(payload.get("upstream_base_url") or current.upstream_base_url).rstrip("/"),
        upstream_chat_path=str(payload.get("upstream_chat_path") or current.upstream_chat_path),
        upstream_api_key=(payload.get("upstream_api_key") if payload.get("upstream_api_key") is not None else current.upstream_api_key) or None,
        adapter_model_ids=parse_model_ids(payload.get("adapter_model_ids"), current.adapter_model_ids),
        upstream_model=str(payload.get("upstream_model") or current.upstream_model),
        request_timeout_seconds=float(payload.get("request_timeout_seconds") or current.request_timeout_seconds),
        synthesize_stream_chunk_size=max(1, int(payload.get("synthesize_stream_chunk_size") or current.synthesize_stream_chunk_size)),
        forward_authorization=parse_bool(payload.get("forward_authorization"), current.forward_authorization),
        model_owner=str(payload.get("model_owner") or current.model_owner),
    )


def public_settings_payload(settings: Settings) -> dict[str, Any]:
    return {
        "upstream_base_url": settings.upstream_base_url,
        "upstream_chat_path": settings.upstream_chat_path,
        "upstream_model": settings.upstream_model,
        "adapter_model_ids": ",".join(settings.adapter_model_ids),
        "has_upstream_api_key": bool(settings.upstream_api_key),
        "forward_authorization": settings.forward_authorization,
        "model_owner": settings.model_owner,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "synthesize_stream_chunk_size": settings.synthesize_stream_chunk_size,
    }


def config_page_html(settings: Settings) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>dsV4 中转配置</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f1ea;
      --panel: rgba(255,255,255,0.88);
      --ink: #1c2430;
      --muted: #5a6676;
      --accent: #14532d;
      --accent-2: #0f766e;
      --border: rgba(23, 37, 84, 0.12);
      --shadow: 0 24px 60px rgba(28, 36, 48, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(20,83,45,0.16), transparent 32%),
        radial-gradient(circle at top right, rgba(15,118,110,0.16), transparent 28%),
        linear-gradient(180deg, #fbfaf7 0%, var(--bg) 100%);
      min-height: 100vh;
      padding: 32px 20px;
    }}
    .wrap {{
      max-width: 920px;
      margin: 0 auto;
      display: grid;
      gap: 18px;
    }}
    .hero, .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }}
    .hero {{
      padding: 24px 26px;
    }}
    .hero h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 700;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .panel {{
      padding: 22px 24px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
      gap: 16px;
    }}
    label {{
      display: grid;
      gap: 8px;
      font-size: 14px;
      color: var(--muted);
    }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid rgba(28, 36, 48, 0.14);
      border-radius: 14px;
      padding: 12px 14px;
      font: inherit;
      color: var(--ink);
      background: rgba(255,255,255,0.95);
    }}
    textarea {{ min-height: 120px; resize: vertical; }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 18px;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    .primary {{ background: var(--accent); color: white; }}
    .secondary {{ background: rgba(20,83,45,0.1); color: var(--accent); }}
    .ghost {{ background: rgba(15,118,110,0.1); color: var(--accent-2); }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      padding: 16px;
      border-radius: 18px;
      background: #17212b;
      color: #eff6ff;
      min-height: 140px;
      line-height: 1.5;
    }}
    .hint {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>dsV4 中转配置页</h1>
      <p>先在这里填上游地址和模型，再让 cc-switch 指向本服务的 <code>/v1</code>。这页只改当前运行中的内存配置，不会碰你的 Codex 本地配置文件。</p>
    </section>

    <section class="panel">
      <div class="grid">
        <label>上游 Base URL
          <input id="upstream_base_url" value="{settings.upstream_base_url}" placeholder="https://api.deepseek.com" />
        </label>
        <label>上游 Chat 路径
          <input id="upstream_chat_path" value="{settings.upstream_chat_path}" placeholder="/chat/completions" />
        </label>
        <label>上游模型名
          <input id="upstream_model" value="{settings.upstream_model}" placeholder="deepseek-v4-pro" />
        </label>
        <label>对外暴露模型名
          <input id="adapter_model_ids" value="{",".join(settings.adapter_model_ids)}" placeholder="deepseek-v4-pro,deepseek-v4-flash" />
        </label>
        <label>可选 Upstream API Key
          <input id="upstream_api_key" type="password" placeholder="留空则透传 Authorization" />
        </label>
        <label>模型归属显示
          <input id="model_owner" value="{settings.model_owner}" placeholder="deepseek" />
        </label>
        <label>请求超时秒数
          <input id="request_timeout_seconds" type="number" step="1" min="1" value="{settings.request_timeout_seconds}" />
        </label>
        <label>合成流分块长度
          <input id="synthesize_stream_chunk_size" type="number" step="1" min="1" value="{settings.synthesize_stream_chunk_size}" />
        </label>
      </div>
      <label style="margin-top:16px;">
        <span>Authorization 透传策略</span>
        <select id="forward_authorization">
          <option value="true" {"selected" if settings.forward_authorization else ""}>开启</option>
          <option value="false" {"selected" if not settings.forward_authorization else ""}>关闭</option>
        </select>
      </label>
      <div class="actions">
        <button class="primary" onclick="saveConfig()">保存当前配置</button>
        <button class="secondary" onclick="testUpstream()">测试上游连通</button>
        <button class="ghost" onclick="testResponses()">测试 /v1/responses</button>
      </div>
      <div class="hint">如果你后面要让 cc-switch 接它，目标地址就是 <code>http://127.0.0.1:18000/v1</code>。</div>
    </section>

    <section class="panel">
      <pre id="status">服务已启动，等待你填写配置。</pre>
    </section>
  </div>

  <script>
    function formPayload() {{
      return {{
        upstream_base_url: document.getElementById('upstream_base_url').value.trim(),
        upstream_chat_path: document.getElementById('upstream_chat_path').value.trim(),
        upstream_model: document.getElementById('upstream_model').value.trim(),
        adapter_model_ids: document.getElementById('adapter_model_ids').value.trim(),
        upstream_api_key: document.getElementById('upstream_api_key').value,
        model_owner: document.getElementById('model_owner').value.trim(),
        request_timeout_seconds: document.getElementById('request_timeout_seconds').value,
        synthesize_stream_chunk_size: document.getElementById('synthesize_stream_chunk_size').value,
        forward_authorization: document.getElementById('forward_authorization').value === 'true',
      }};
    }}

    function show(data) {{
      document.getElementById('status').textContent =
        typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    }}

    async function saveConfig() {{
      show('正在保存配置...');
      const res = await fetch('/admin/config', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(formPayload())
      }});
      show(await res.json());
    }}

    async function testUpstream() {{
      show('正在测试上游连通...');
      const res = await fetch('/admin/test-upstream', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ ...formPayload(), prompt: '请回复 ok' }})
      }});
      show(await res.json());
    }}

    async function testResponses() {{
      show('正在测试本地 /v1/responses ...');
      const res = await fetch('/v1/responses', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          model: document.getElementById('upstream_model').value.trim() || 'deepseek-v4-pro',
          input: '请简单回复 hello'
        }})
      }});
      show(await res.json());
    }}
  </script>
</body>
</html>"""


class UpstreamClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _build_headers(self, inbound_authorization: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.settings.upstream_api_key}"
        elif self.settings.forward_authorization and inbound_authorization:
            headers["Authorization"] = inbound_authorization
        return headers

    async def create_chat_completion(
        self,
        body: dict[str, Any],
        inbound_authorization: str | None = None,
    ) -> dict[str, Any]:
        headers = self._build_headers(inbound_authorization)

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.post(self.settings.upstream_chat_url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise UpstreamError(502, f"Failed to reach upstream chat API: {exc}", "api_connection_error") from exc

        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None

            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                error = payload["error"]
                raw_message = str(error.get("message") or "Upstream request failed.")
                append_request_trace(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "endpoint": "upstream:/chat/completions",
                        "note": "upstream_error",
                        "status_code": response.status_code,
                        "error_type": str(error.get("type") or "upstream_error"),
                        "raw_error_message": raw_message,
                    }
                )
                raise UpstreamError(
                    response.status_code,
                    localize_deepseek_error(response.status_code, raw_message),
                    str(error.get("type") or "upstream_error"),
                )

            append_request_trace(
                {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "endpoint": "upstream:/chat/completions",
                    "note": "upstream_error",
                    "status_code": response.status_code,
                    "error_type": "upstream_error",
                    "raw_error_message": response.text[:1200],
                }
            )
            raise UpstreamError(
                response.status_code,
                localize_deepseek_error(response.status_code, response.text),
            )

        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(502, "Upstream returned non-JSON content.", "invalid_upstream_response") from exc

    async def stream_chat_completion(
        self,
        body: dict[str, Any],
        inbound_authorization: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        headers = self._build_headers(inbound_authorization)

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                async with client.stream("POST", self.settings.upstream_chat_url, headers=headers, json=body) as response:
                    if response.status_code >= 400:
                        raw_bytes = await response.aread()
                        raw_text = raw_bytes.decode("utf-8", errors="replace")
                        try:
                            payload = json.loads(raw_text)
                        except ValueError:
                            payload = None

                        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                            error = payload["error"]
                            raw_message = str(error.get("message") or "Upstream request failed.")
                            append_request_trace(
                                {
                                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "endpoint": "upstream:/chat/completions",
                                    "note": "upstream_error",
                                    "status_code": response.status_code,
                                    "error_type": str(error.get("type") or "upstream_error"),
                                    "raw_error_message": raw_message,
                                }
                            )
                            raise UpstreamError(
                                response.status_code,
                                localize_deepseek_error(response.status_code, raw_message),
                                str(error.get("type") or "upstream_error"),
                            )

                        append_request_trace(
                            {
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "endpoint": "upstream:/chat/completions",
                                "note": "upstream_error",
                                "status_code": response.status_code,
                                "error_type": "upstream_error",
                                "raw_error_message": raw_text[:1200],
                            }
                        )
                        raise UpstreamError(
                            response.status_code,
                            localize_deepseek_error(response.status_code, raw_text),
                        )

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        stripped = line.strip()
                        if not stripped.startswith("data:"):
                            continue
                        data = stripped[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            yield json.loads(data)
                        except ValueError as exc:
                            raise UpstreamError(502, f"Upstream returned invalid streaming JSON: {data[:200]}", "invalid_upstream_response") from exc
        except httpx.RequestError as exc:
            raise UpstreamError(502, f"Failed to reach upstream chat API: {exc}", "api_connection_error") from exc


class EventWriter:
    def __init__(self) -> None:
        self.sequence_number = 0

    def encode(self, event_name: str, payload: dict[str, Any]) -> bytes:
        event = {"sequence_number": self.sequence_number, **payload}
        self.sequence_number += 1
        return (
            f"event: {event_name}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        ).encode("utf-8")


def _response_stub_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": new_id("resp"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "instructions": payload.get("instructions"),
        "model": payload.get("model"),
        "output": [],
        "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        "temperature": payload.get("temperature"),
        "tool_choice": payload.get("tool_choice", "auto" if payload.get("tools") else "none"),
        "tools": payload.get("tools", []),
        "top_p": payload.get("top_p"),
        "truncation": "disabled",
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "metadata": payload.get("metadata", {}),
    }


def _message_output_item(item_id: str, text: str, role: str = "assistant", status: str = "completed") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": role,
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
    }


def _function_output_item(item_id: str, call_id: str, name: str, arguments: str, status: str = "completed") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "status": status,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


async def stream_chat_chunks_as_response_events(
    payload: dict[str, Any],
    upstream_client: UpstreamClient,
    chat_request: dict[str, Any],
    tool_meta: dict[str, dict[str, str]],
    inbound_authorization: str | None,
    started_at: float,
) -> AsyncIterator[bytes]:
    writer = EventWriter()
    response_stub = _response_stub_from_payload(payload)
    response_id = response_stub["id"]
    yield writer.encode("response.created", {"type": "response.created", "response": response_stub})
    yield writer.encode("response.in_progress", {"type": "response.in_progress", "response": response_stub})

    message_item_id: str | None = None
    message_started = False
    message_text = ""
    message_output_index: int | None = None

    tool_states: dict[int, dict[str, Any]] = {}
    output_items: list[dict[str, Any]] = []
    usage_payload = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    try:
        async for chunk in upstream_client.stream_chat_completion(chat_request, inbound_authorization):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or {}

            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
                usage_payload = {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                }

            content_delta = delta.get("content")
            if content_delta is not None:
                if not message_started:
                    message_started = True
                    message_item_id = new_id("msg")
                    message_output_index = len(output_items)
                    partial_item = {
                        "id": message_item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    }
                    output_items.append(partial_item)
                    yield writer.encode(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": message_output_index,
                            "item": partial_item,
                        },
                    )
                    yield writer.encode(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "response_id": response_id,
                            "item_id": message_item_id,
                            "output_index": message_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    )
                if content_delta:
                    message_text += str(content_delta)
                    yield writer.encode(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "item_id": message_item_id,
                            "output_index": message_output_index,
                            "content_index": 0,
                            "delta": str(content_delta),
                        },
                    )

            for tool_delta in delta.get("tool_calls") or []:
                if not isinstance(tool_delta, dict):
                    continue
                tool_index = int(tool_delta.get("index") or 0)
                state = tool_states.get(tool_index)
                if state is None:
                    call_id = tool_delta.get("id") or new_id("call")
                    function_block = tool_delta.get("function") or {}
                    upstream_name = function_block.get("name", "tool")
                    tool_info = tool_meta.get(str(upstream_name), {})
                    original_name = tool_info.get("original_name", upstream_name)
                    item_id = new_id("fc")
                    output_index = len(output_items)
                    state = {
                        "item_id": item_id,
                        "call_id": call_id,
                        "upstream_name": upstream_name,
                        "name": original_name,
                        "arguments": "",
                        "output_index": output_index,
                    }
                    tool_states[tool_index] = state
                    partial_item = {
                        "id": item_id,
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": call_id,
                        "name": original_name,
                        "arguments": "",
                    }
                    output_items.append(partial_item)
                    yield writer.encode(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": partial_item,
                        },
                    )

                if tool_delta.get("id"):
                    state["call_id"] = tool_delta["id"]
                function_block = tool_delta.get("function") or {}
                if function_block.get("name"):
                    upstream_name = function_block["name"]
                    tool_info = tool_meta.get(str(upstream_name), {})
                    state["upstream_name"] = upstream_name
                    state["name"] = tool_info.get("original_name", upstream_name)
                arguments_delta = function_block.get("arguments")
                if arguments_delta:
                    state["arguments"] += str(arguments_delta)
                    yield writer.encode(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "response_id": response_id,
                            "item_id": state["item_id"],
                            "output_index": state["output_index"],
                            "delta": str(arguments_delta),
                        },
                    )

            finish_reason = choice.get("finish_reason")
            if finish_reason == "tool_calls":
                for tool_index in sorted(tool_states):
                    state = tool_states[tool_index]
                    final_item = _function_output_item(
                        state["item_id"],
                        state["call_id"],
                        state["name"],
                        state["arguments"],
                    )
                    output_items[state["output_index"]] = final_item
                    yield writer.encode(
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "response_id": response_id,
                            "item_id": state["item_id"],
                            "output_index": state["output_index"],
                            "arguments": state["arguments"],
                        },
                    )
                    yield writer.encode(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "response_id": response_id,
                            "output_index": state["output_index"],
                            "item": final_item,
                        },
                    )
            elif finish_reason == "stop" and message_started and message_item_id is not None and message_output_index is not None:
                final_message_item = _message_output_item(message_item_id, message_text, status="completed")
                output_items[message_output_index] = final_message_item
                yield writer.encode(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": message_output_index,
                        "content_index": 0,
                        "text": message_text,
                    },
                )
                yield writer.encode(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": message_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": message_text, "annotations": []},
                    },
                )
                yield writer.encode(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": message_output_index,
                        "item": final_message_item,
                    },
                )
    except UpstreamError as exc:
        trace_responses_request(payload, (time.perf_counter() - started_at) * 1000, True, f"upstream_error:{exc.status_code}")
        yield writer.encode(
            "error",
            {
                "type": "error",
                "error": {
                    "message": exc.message,
                    "type": exc.error_type,
                    "param": None,
                    "code": exc.status_code,
                },
            },
        )
        return
    except Exception as exc:  # pragma: no cover - defensive fallback
        trace_responses_request(payload, (time.perf_counter() - started_at) * 1000, False, "adapter_error")
        yield writer.encode(
            "error",
            {
                "type": "error",
                "error": {
                    "message": f"Adapter failure: {exc}",
                    "type": "adapter_error",
                    "param": None,
                    "code": 500,
                },
            },
        )
        return

    response_stub["status"] = "completed"
    response_stub["output"] = output_items
    response_stub["usage"] = usage_payload
    trace_responses_request(payload, (time.perf_counter() - started_at) * 1000, True, "ok_stream")
    yield writer.encode("response.completed", {"type": "response.completed", "response": response_stub})


async def stream_response_object(response_object: dict[str, Any], chunk_size: int) -> AsyncIterator[bytes]:
    writer = EventWriter()
    response_id = response_object["id"]
    created_payload = deepcopy(response_object)
    created_payload["status"] = "in_progress"
    created_payload["output"] = []
    yield writer.encode("response.created", {"type": "response.created", "response": created_payload})
    yield writer.encode("response.in_progress", {"type": "response.in_progress", "response": created_payload})

    for output_index, item in enumerate(response_object.get("output", [])):
        item_type = item.get("type")
        item_id = item.get("id", new_id("item"))

        if item_type == "message":
            text = ""
            content = item.get("content") or []
            if content and isinstance(content[0], dict):
                text = str(content[0].get("text", ""))

            in_progress_item = {
                "id": item_id,
                "type": "message",
                "status": "in_progress",
                "role": item.get("role", "assistant"),
                "content": [],
            }
            yield writer.encode(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            yield writer.encode(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "response_id": response_id,
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }
            )
            for chunk in iter_text_chunks(text, chunk_size):
                yield writer.encode(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": response_id,
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": 0,
                        "delta": chunk,
                    }
                )
            yield writer.encode(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "response_id": response_id,
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "text": text,
                }
            )
            yield writer.encode(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "response_id": response_id,
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text, "annotations": []},
                }
            )
            yield writer.encode(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": output_index,
                    "item": item,
                }
            )
            continue

        if item_type == "function_call":
            function_item = {
                "id": item_id,
                "type": "function_call",
                "call_id": item.get("call_id"),
                "name": item.get("name"),
                "arguments": "",
                "status": "in_progress",
            }
            arguments = str(item.get("arguments", ""))
            yield writer.encode(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": output_index,
                    "item": function_item,
                }
            )
            for chunk in iter_text_chunks(arguments, chunk_size):
                yield writer.encode(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "response_id": response_id,
                        "item_id": item_id,
                        "output_index": output_index,
                        "delta": chunk,
                    }
                )
            yield writer.encode(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": response_id,
                    "item_id": item_id,
                    "output_index": output_index,
                    "arguments": arguments,
                }
            )
            yield writer.encode(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": output_index,
                    "item": item,
                }
            )
            continue

        yield writer.encode(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "response_id": response_id,
                "output_index": output_index,
                "item": item,
            }
        )
        yield writer.encode(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "response_id": response_id,
                "output_index": output_index,
                "item": item,
            }
        )

    yield writer.encode("response.completed", {"type": "response.completed", "response": response_object})


async def error_event_stream(status_code: int, message: str, error_type: str) -> AsyncIterator[bytes]:
    writer = EventWriter()
    yield writer.encode(
        "error",
        {
            "type": "error",
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": status_code,
            },
        }
    )


def create_app(
    settings: Settings | None = None,
    upstream_client: UpstreamClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    upstream_client = upstream_client or UpstreamClient(settings)

    app = FastAPI(title="dsV4 OpenAI Adapter", version="0.1.0")
    app.state.settings = settings
    app.state.upstream_client = upstream_client

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return HTMLResponse(config_page_html(app.state.settings))

    @app.get("/admin/config")
    async def get_config() -> dict[str, Any]:
        return public_settings_payload(app.state.settings)

    @app.post("/admin/config")
    async def set_config(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise UpstreamError(400, "Config payload must be a JSON object.", "invalid_request_error")
        updated = build_settings_from_payload(app.state.settings, payload)
        app.state.settings = updated
        app.state.upstream_client.settings = updated
        return {
            "ok": True,
            "message": "配置已更新到当前运行实例。",
            "config": public_settings_payload(updated),
        }

    @app.post("/admin/test-upstream")
    async def test_upstream(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            return openai_error_response(400, "Config payload must be a JSON object.")
        updated = build_settings_from_payload(app.state.settings, payload)
        app.state.settings = updated
        app.state.upstream_client.settings = updated
        probe_body = {
            "model": updated.upstream_model,
            "messages": [{"role": "user", "content": str(payload.get("prompt") or "Reply with ok.")}],
            "stream": False,
        }
        try:
            upstream_payload = await app.state.upstream_client.create_chat_completion(
                probe_body,
                request.headers.get("authorization"),
            )
        except UpstreamError as exc:
            return openai_error_response(exc.status_code, exc.message, exc.error_type)
        return {
            "ok": True,
            "message": "上游已连通。",
            "config": public_settings_payload(updated),
            "upstream_preview": upstream_payload,
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        current_settings = app.state.settings
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": current_settings.model_owner,
                }
                for model_id in current_settings.adapter_model_ids
            ],
        }

    @app.post("/v1/chat/completions")
    async def proxy_chat_completions(request: Request) -> JSONResponse:
        current_settings = app.state.settings
        payload = await request.json()
        payload.setdefault("model", current_settings.upstream_model)
        payload["messages"] = normalize_chat_messages_for_upstream(payload.get("messages"))
        started_at = time.perf_counter()
        if payload.get("stream"):
            payload["stream"] = False
        try:
            response_payload = await app.state.upstream_client.create_chat_completion(
                payload,
                request.headers.get("authorization"),
            )
        except UpstreamError as exc:
            trace_chat_request(payload, (time.perf_counter() - started_at) * 1000, True, f"upstream_error:{exc.status_code}")
            return openai_error_response(exc.status_code, exc.message, exc.error_type)
        trace_chat_request(payload, (time.perf_counter() - started_at) * 1000, True, "ok")
        return JSONResponse(response_payload)

    @app.post("/v1/responses")
    async def create_response(request: Request):
        current_settings = app.state.settings
        payload = await request.json()
        started_at = time.perf_counter()
        if not isinstance(payload, dict):
            trace_responses_request({"input": None}, (time.perf_counter() - started_at) * 1000, False, "bad_request")
            return openai_error_response(400, "Request body must be a JSON object.")

        try:
            chat_request, tool_kinds = response_request_to_chat_request(payload, current_settings)
            trace_upstream_payload(chat_request, "before_upstream")
            if payload.get("stream"):
                chat_request["stream"] = True
                return StreamingResponse(
                    stream_chat_chunks_as_response_events(
                        payload,
                        app.state.upstream_client,
                        chat_request,
                        tool_kinds,
                        request.headers.get("authorization"),
                        started_at,
                    ),
                    media_type="text/event-stream",
                )
            chat_response = await app.state.upstream_client.create_chat_completion(
                chat_request,
                request.headers.get("authorization"),
            )
            response_object = chat_completion_to_response(chat_response, payload, tool_kinds)
        except UpstreamError as exc:
            trace_responses_request(payload, (time.perf_counter() - started_at) * 1000, True, f"upstream_error:{exc.status_code}")
            if payload.get("stream"):
                return StreamingResponse(
                    error_event_stream(exc.status_code, exc.message, exc.error_type),
                    media_type="text/event-stream",
                )
            return openai_error_response(exc.status_code, exc.message, exc.error_type)
        except Exception as exc:  # pragma: no cover - defensive fallback
            trace_responses_request(payload, (time.perf_counter() - started_at) * 1000, False, "adapter_error")
            if payload.get("stream"):
                return StreamingResponse(
                    error_event_stream(500, f"Adapter failure: {exc}", "adapter_error"),
                    media_type="text/event-stream",
                )
            return openai_error_response(500, f"Adapter failure: {exc}", "adapter_error")

        trace_responses_request(payload, (time.perf_counter() - started_at) * 1000, True, "ok")
        return JSONResponse(response_object)

    return app


app = create_app()
