from __future__ import annotations

from dataclasses import dataclass
import os

from .defaults import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_MODEL_OWNER,
    DEFAULT_UPSTREAM_CHAT_PATH,
)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


@dataclass(slots=True)
class Settings:
    upstream_base_url: str
    upstream_chat_path: str
    upstream_api_key: str | None
    adapter_model_ids: tuple[str, ...]
    upstream_model: str
    request_timeout_seconds: float
    synthesize_stream_chunk_size: int
    forward_authorization: bool
    model_owner: str

    @classmethod
    def from_env(cls) -> "Settings":
        adapter_model_ids = tuple(
            part.strip()
            for part in os.getenv("ADAPTER_MODEL_IDS", DEFAULT_DEEPSEEK_MODEL).split(",")
            if part.strip()
        )
        upstream_model = os.getenv("UPSTREAM_MODEL") or (adapter_model_ids[0] if adapter_model_ids else DEFAULT_DEEPSEEK_MODEL)
        return cls(
            upstream_base_url=os.getenv("UPSTREAM_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).rstrip("/"),
            upstream_chat_path=_normalize_path(os.getenv("UPSTREAM_CHAT_PATH", DEFAULT_UPSTREAM_CHAT_PATH)),
            upstream_api_key=os.getenv("UPSTREAM_API_KEY"),
            adapter_model_ids=adapter_model_ids or (DEFAULT_DEEPSEEK_MODEL,),
            upstream_model=upstream_model,
            request_timeout_seconds=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "120")),
            synthesize_stream_chunk_size=max(1, int(os.getenv("STREAM_CHUNK_SIZE", "64"))),
            forward_authorization=_bool_env("FORWARD_AUTHORIZATION", True),
            model_owner=os.getenv("MODEL_OWNER", DEFAULT_MODEL_OWNER),
        )

    @property
    def upstream_chat_url(self) -> str:
        return f"{self.upstream_base_url}{self.upstream_chat_path}"
