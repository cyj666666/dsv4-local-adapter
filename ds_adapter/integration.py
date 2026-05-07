from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import quote, urlencode
import webbrowser

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

from .defaults import DEFAULT_DEEPSEEK_MODEL, DEFAULT_MODEL_OWNER, DEFAULT_UPSTREAM_CHAT_PATH

FIXED_HOST = "127.0.0.1"
FIXED_PORT = 9468
FIXED_CHAT_PATH = DEFAULT_UPSTREAM_CHAT_PATH
FIXED_TIMEOUT_SECONDS = 120
FIXED_STREAM_CHUNK_SIZE = 64
FIXED_MODEL_OWNER = DEFAULT_MODEL_OWNER

PROVIDER_ID = "ds_adapter_local_9468"
PROVIDER_KEY = "dsadapter_local"
PROVIDER_NAME = "DeepSeek V4 \u672c\u5730\u4e2d\u8f6c"
PROVIDER_NOTE = "Managed by dsV4 adapter window tool."
CC_SWITCH_PROTOCOL = "ccswitch"
CC_SWITCH_IMPORT_PATH = "v1/import"
CC_SWITCH_DIR = Path.home() / ".cc-switch"
CC_SWITCH_SETTINGS_PATH = CC_SWITCH_DIR / "settings.json"
CC_SWITCH_DB_PATH = CC_SWITCH_DIR / "cc-switch.db"


def adapter_root_url() -> str:
    return f"http://{FIXED_HOST}:{FIXED_PORT}"


def adapter_v1_url() -> str:
    return f"{adapter_root_url()}/v1"


def detect_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def detect_cc_switch_dir() -> Path:
    return CC_SWITCH_DIR


def build_codex_provider_config(adapter_model: str, requires_openai_auth: bool = False) -> str:
    model = adapter_model.strip() or DEFAULT_DEEPSEEK_MODEL
    return (
        f'model_provider = "{PROVIDER_KEY}"\n'
        f'model = "{model}"\n'
        'disable_response_storage = true\n\n'
        '[model_providers]\n'
        f'[model_providers.{PROVIDER_KEY}]\n'
        f'name = "{PROVIDER_KEY}"\n'
        'wire_api = "responses"\n'
        f'requires_openai_auth = {"true" if requires_openai_auth else "false"}\n'
        f'base_url = "{adapter_v1_url()}"\n'
    )


def build_cc_settings_config(adapter_model: str, api_key: str | None = None) -> str:
    api_key_value = (api_key or "").strip()
    payload = {
        "auth": {"OPENAI_API_KEY": api_key_value} if api_key_value else {},
        "config": build_codex_provider_config(adapter_model, requires_openai_auth=bool(api_key_value)),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _extract_base_url_from_provider_config(settings_config: str) -> str | None:
    try:
        payload = json.loads(settings_config)
    except json.JSONDecodeError:
        return None

    config_text = payload.get("config")
    if not isinstance(config_text, str):
        return None

    match = re.search(r'(?m)^base_url\s*=\s*"([^"]+)"\s*$', config_text)
    if match:
        return match.group(1).strip()
    return None


def get_cc_codex_status(cc_dir: Path | None = None) -> dict[str, Any]:
    root = cc_dir or detect_cc_switch_dir()
    settings_path = root / "settings.json"
    db_path = root / "cc-switch.db"

    result: dict[str, Any] = {
        "ok": False,
        "cc_dir": str(root),
        "settings_path": str(settings_path),
        "db_path": str(db_path),
        "target_base_url": adapter_v1_url(),
        "current_provider_id": None,
        "current_provider_name": None,
        "current_provider_base_url": None,
        "local_provider_found": False,
        "local_provider_id": None,
        "local_provider_name": None,
        "local_provider_base_url": None,
        "local_provider_current": False,
        "proxy_enabled": None,
        "proxy_listen": None,
        "message": "",
    }

    if not settings_path.exists():
        result["message"] = f"未找到 CC Switch 设置文件：{settings_path}"
        return result
    if not db_path.exists():
        result["message"] = f"未找到 CC Switch 数据库：{db_path}"
        return result

    try:
        settings_payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["message"] = f"读取 CC Switch 设置失败：{exc}"
        return result

    current_provider_id = settings_payload.get("currentProviderCodex")
    if isinstance(current_provider_id, str) and current_provider_id.strip():
        result["current_provider_id"] = current_provider_id.strip()

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        provider_rows = cur.execute(
            """
            SELECT id, name, settings_config, is_current, created_at
            FROM providers
            WHERE app_type = 'codex'
            ORDER BY created_at DESC
            """
        ).fetchall()

        proxy_row = cur.execute(
            """
            SELECT proxy_enabled, listen_address, listen_port
            FROM proxy_config
            WHERE app_type = 'codex'
            """
        ).fetchone()
    except Exception as exc:
        result["message"] = f"读取 CC Switch 数据库失败：{exc}"
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass

    providers: list[dict[str, Any]] = []
    for row in provider_rows:
        base_url = _extract_base_url_from_provider_config(str(row["settings_config"]))
        providers.append(
            {
                "id": row["id"],
                "name": row["name"],
                "base_url": base_url,
                "is_current": bool(row["is_current"]),
            }
        )

    current_provider = None
    if result["current_provider_id"]:
        current_provider = next((item for item in providers if item["id"] == result["current_provider_id"]), None)
    if current_provider is None:
        current_provider = next((item for item in providers if item["is_current"]), None)

    local_provider = next((item for item in providers if item["base_url"] == adapter_v1_url()), None)

    if current_provider is not None:
        result["current_provider_id"] = current_provider["id"]
        result["current_provider_name"] = current_provider["name"]
        result["current_provider_base_url"] = current_provider["base_url"]

    if local_provider is not None:
        result["local_provider_found"] = True
        result["local_provider_id"] = local_provider["id"]
        result["local_provider_name"] = local_provider["name"]
        result["local_provider_base_url"] = local_provider["base_url"]
        result["local_provider_current"] = current_provider is not None and current_provider["id"] == local_provider["id"]

    if proxy_row is not None:
        result["proxy_enabled"] = bool(proxy_row["proxy_enabled"])
        result["proxy_listen"] = f"{proxy_row['listen_address']}:{proxy_row['listen_port']}"

    if result["local_provider_current"]:
        result["ok"] = True
        result["message"] = (
            f"CC 当前已切换到本地中转：{result['local_provider_name']} "
            f"({result['local_provider_base_url']})"
        )
        return result

    current_name = result["current_provider_name"] or "未识别"
    current_url = result["current_provider_base_url"] or "未识别"
    if result["local_provider_found"]:
        result["message"] = (
            f"CC 已导入本地中转，但当前仍在使用 {current_name} ({current_url})。"
            f" 请在 CC 中切换到 {result['local_provider_name']}。"
        )
        return result

    result["message"] = (
        f"CC 里还没有检测到指向 {adapter_v1_url()} 的 Codex Provider。"
        " 请先执行“一键导入到 CC”并在 CC 弹窗中确认。"
    )
    return result


def build_cc_provider_deeplink(adapter_model: str, api_key: str | None = None) -> str:
    model = adapter_model.strip() or DEFAULT_DEEPSEEK_MODEL
    api_key_value = (api_key or "").strip()
    encoded_config = base64.b64encode(build_cc_settings_config(model, api_key_value).encode("utf-8")).decode("ascii")
    params = {
        "resource": "provider",
        "app": "codex",
        "name": PROVIDER_NAME,
        "endpoint": adapter_v1_url(),
        "homepage": adapter_root_url(),
        "model": model,
        "notes": PROVIDER_NOTE,
        "configFormat": "json",
        "config": encoded_config,
        "enabled": "true",
    }
    if api_key_value:
        params["apiKey"] = api_key_value
    query = urlencode(params, quote_via=quote)
    return f"{CC_SWITCH_PROTOCOL}://{CC_SWITCH_IMPORT_PATH}?{query}"


def detect_cc_protocol_command() -> str | None:
    if winreg is None:
        return None

    candidates = (
        (winreg.HKEY_CURRENT_USER, r"Software\Classes\ccswitch\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"ccswitch\shell\open\command"),
    )
    for root, sub_key in candidates:
        try:
            with winreg.OpenKey(root, sub_key) as handle:
                value, _ = winreg.QueryValueEx(handle, None)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return value
    return None


def launch_cc_provider_import(adapter_model: str, api_key: str | None = None) -> dict[str, Any]:
    deeplink = build_cc_provider_deeplink(adapter_model, api_key)
    protocol_command = detect_cc_protocol_command()
    if not protocol_command:
        return {
            "ok": False,
            "message": "未检测到 CC Switch 的 ccswitch:// 协议注册。",
            "deeplink": deeplink,
        }

    try:
        if hasattr(os, "startfile"):
            os.startfile(deeplink)  # type: ignore[attr-defined]
        else:  # pragma: no cover - Windows desktop is the main target
            launched = webbrowser.open(deeplink)
            if not launched:
                raise OSError("webbrowser.open returned false")
    except OSError as exc:
        return {
            "ok": False,
            "message": f"唤起 CC Switch 失败: {exc}",
            "deeplink": deeplink,
            "protocol_command": protocol_command,
        }

    return {
        "ok": True,
        "message": "已唤起 CC Switch，请在 CC 弹窗中确认导入。",
        "deeplink": deeplink,
        "protocol_command": protocol_command,
        "requires_confirmation": True,
    }


def _replace_or_insert_top_level(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    replacement = f'{key} = "{value}"'
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    prefix = replacement + "\n"
    return prefix + text if text else prefix


def _replace_or_insert_bool(text: str, key: str, value: bool) -> str:
    rendered = "true" if value else "false"
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    replacement = f"{key} = {rendered}"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    prefix = replacement + "\n"
    return prefix + text if text else prefix


def _replace_or_append_provider_section(text: str) -> str:
    section_text = (
        f"\n[model_providers.{PROVIDER_KEY}]\n"
        f'name = "{PROVIDER_KEY}"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
        f'base_url = "{adapter_v1_url()}"\n'
    )
    section_pattern = re.compile(
        rf"(?ms)^\[model_providers\.{re.escape(PROVIDER_KEY)}\]\n.*?(?=^\[|\Z)"
    )
    if section_pattern.search(text):
        return section_pattern.sub(section_text.lstrip("\n"), text, count=1)
    return text.rstrip() + "\n" + section_text


def patch_codex_config_text(original_text: str, adapter_model: str) -> str:
    text = original_text
    text = _replace_or_insert_top_level(text, "model_provider", PROVIDER_KEY)
    text = _replace_or_insert_top_level(text, "model", adapter_model.strip() or DEFAULT_DEEPSEEK_MODEL)
    text = _replace_or_insert_bool(text, "disable_response_storage", True)

    if "[model_providers]" not in text:
        text = text.rstrip() + "\n\n[model_providers]\n"

    text = _replace_or_append_provider_section(text)
    return text


def test_codex_import_roundtrip(config_path: Path, adapter_model: str) -> dict[str, Any]:
    if not config_path.exists():
        return {
            "ok": False,
            "message": f"Codex \u914d\u7f6e\u6587\u4ef6\u4e0d\u5b58\u5728: {config_path}",
        }

    original_text = config_path.read_text(encoding="utf-8")
    patched_text = patch_codex_config_text(original_text, adapter_model)

    config_path.write_text(patched_text, encoding="utf-8")
    changed_text = config_path.read_text(encoding="utf-8")
    expected_url = adapter_v1_url()
    expected_provider_line = f'model_provider = "{PROVIDER_KEY}"'
    import_ok = expected_url in changed_text and expected_provider_line in changed_text

    config_path.write_text(original_text, encoding="utf-8")
    restored_ok = config_path.read_text(encoding="utf-8") == original_text

    return {
        "ok": import_ok and restored_ok,
        "message": (
            "Codex \u5bfc\u5165\u6d4b\u8bd5\u6210\u529f\u5e76\u5df2\u56de\u6eda\u3002"
            if import_ok and restored_ok
            else "Codex \u5bfc\u5165\u6d4b\u8bd5\u672a\u901a\u8fc7\u3002"
        ),
        "config_path": str(config_path),
        "import_ok": import_ok,
        "restored_ok": restored_ok,
        "expected_base_url": expected_url,
    }


def apply_codex_config(config_path: Path, adapter_model: str) -> dict[str, Any]:
    if not config_path.exists():
        return {
            "ok": False,
            "message": f"Codex \u914d\u7f6e\u6587\u4ef6\u4e0d\u5b58\u5728: {config_path}",
        }

    original_text = config_path.read_text(encoding="utf-8")
    patched_text = patch_codex_config_text(original_text, adapter_model)
    config_path.write_text(patched_text, encoding="utf-8")
    changed_text = config_path.read_text(encoding="utf-8")
    expected_url = adapter_v1_url()
    expected_provider_line = f'model_provider = "{PROVIDER_KEY}"'
    apply_ok = expected_url in changed_text and expected_provider_line in changed_text

    return {
        "ok": apply_ok,
        "message": (
            "Codex \u914d\u7f6e\u5df2\u5e94\u7528\u3002"
            if apply_ok
            else "Codex \u914d\u7f6e\u5199\u5165\u540e\u6821\u9a8c\u672a\u901a\u8fc7\u3002"
        ),
        "config_path": str(config_path),
        "applied": apply_ok,
        "expected_base_url": expected_url,
    }


def launch_cc_provider_import(adapter_model: str, api_key: str | None = None) -> dict[str, Any]:
    deeplink = build_cc_provider_deeplink(adapter_model, api_key)
    protocol_command = detect_cc_protocol_command()
    if not protocol_command:
        return {
            "ok": False,
            "message": "未检测到 CC Switch 的 ccswitch:// 协议注册。",
            "deeplink": deeplink,
        }

    try:
        if hasattr(os, "startfile"):
            os.startfile(deeplink)  # type: ignore[attr-defined]
        else:  # pragma: no cover - Windows desktop is the main target
            launched = webbrowser.open(deeplink)
            if not launched:
                raise OSError("webbrowser.open returned false")
    except OSError as exc:
        return {
            "ok": False,
            "message": f"唤起 CC Switch 失败: {exc}",
            "deeplink": deeplink,
            "protocol_command": protocol_command,
        }

    return {
        "ok": True,
        "message": "已唤起 CC Switch，请在 CC 弹窗中确认导入。",
        "deeplink": deeplink,
        "protocol_command": protocol_command,
        "requires_confirmation": True,
    }
