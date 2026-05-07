from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from ds_adapter.app import create_app
from ds_adapter.config import Settings


class FakeUpstreamClient:
    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.last_body = None
        self.last_auth = None
        self.call_count = 0
        self.stream_chunks = None

    async def create_chat_completion(self, body, inbound_authorization=None):
        self.call_count += 1
        self.last_body = body
        self.last_auth = inbound_authorization
        return self.response_payload

    async def stream_chat_completion(self, body, inbound_authorization=None):
        self.call_count += 1
        self.last_body = body
        self.last_auth = inbound_authorization
        for chunk in self.stream_chunks or []:
            yield chunk


class AdapterTests(unittest.TestCase):
    def test_models_endpoint(self):
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4", "ds-v4-coder"),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=32,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=FakeUpstreamClient({}))
        client = TestClient(app)

        response = client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["data"]], ["ds-v4", "ds-v4-coder"])

    def test_responses_endpoint_translates_to_chat(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_test",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "hello from upstream",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "instructions": "Be concise.",
                "reasoning": {"effort": "xhigh"},
                "input": [{"role": "user", "content": "Say hello."}],
            },
            headers={"Authorization": "Bearer passthrough"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hello from upstream")
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(upstream.last_auth, "Bearer passthrough")
        self.assertEqual(upstream.last_body["messages"][0]["role"], "system")
        self.assertEqual(upstream.last_body["messages"][1]["role"], "user")
        self.assertEqual(upstream.last_body["reasoning_effort"], "max")
        self.assertEqual(upstream.last_body["thinking"]["type"], "enabled")
        self.assertEqual(upstream.last_body["model"], "ds-v4")

    def test_responses_endpoint_ignores_downstream_model_and_uses_adapter_model(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_test_model_override",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "total_tokens": 6,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("whatever-user-sees",),
            upstream_model="deepseek-v4-pro",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            json={
                "model": "claude-sonnet-4",
                "input": "Say hi.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.last_body["model"], "deepseek-v4-pro")
        self.assertEqual(response.json()["model"], "claude-sonnet-4")

    def test_json_schema_text_format_downgrades_to_json_object(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_json_schema",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "{\"ok\":true}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "instructions": "Be strict.",
                "input": "Return ok=true",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "probe",
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                        "strict": True,
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.last_body["response_format"]["type"], "json_object")
        self.assertIn("Follow this JSON schema exactly", upstream.last_body["messages"][0]["content"])

    def test_streaming_response_emits_named_sse_events(self):
        upstream = FakeUpstreamClient({})
        upstream.stream_chunks = [
            {
                "id": "chunk_tool_1",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            },
            {
                "id": "chunk_tool_2",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {"name": "lookup_docs", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chunk_tool_3",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"query\":\"adapter\"}"}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
            },
        ]
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=6,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        with client.stream(
            "POST",
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_docs",
                            "description": "Find docs.",
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "input": "Need docs.",
            },
        ) as response:
            body = "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: response.created", body)
        self.assertIn("event: response.in_progress", body)
        self.assertIn("event: response.function_call_arguments.delta", body)
        self.assertIn("event: response.completed", body)

    def test_streaming_response_passthroughs_text_deltas(self):
        upstream = FakeUpstreamClient({})
        upstream.stream_chunks = [
            {
                "id": "chunk_1",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            },
            {
                "id": "chunk_2",
                "choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}],
            },
            {
                "id": "chunk_3",
                "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        ]
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=6,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "gpt-5.4", "stream": True, "input": "Need hello."},
        ) as response:
            body = "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(upstream.last_body["stream"])
        self.assertIn('"delta": "hel"', body)
        self.assertIn('"delta": "lo"', body)
        self.assertIn('"text": "hello"', body)
        self.assertIn('"total_tokens": 5', body)

    def test_streaming_response_passthroughs_tool_call_argument_deltas(self):
        upstream = FakeUpstreamClient({})
        upstream.stream_chunks = [
            {
                "id": "chunk_tool_1",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            },
            {
                "id": "chunk_tool_2",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {"name": "lookup_docs", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chunk_tool_3",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"q\""}}]},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chunk_tool_4",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            },
        ]
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=6,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        with client.stream(
            "POST",
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_docs",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        },
                    }
                ],
                "input": "Need docs.",
            },
        ) as response:
            body = "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: response.function_call_arguments.delta", body)
        self.assertIn('"delta": "{\\"q\\""', body)
        self.assertIn('"arguments": "{\\"q\\":1}"', body)
        self.assertIn('"call_id": "call_123"', body)

    def test_chat_completions_stream_flag_falls_back_to_non_stream_upstream(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_stream_fallback",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "fallback ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(upstream.last_body["stream"])
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "fallback ok")

    def test_responses_endpoint_converts_developer_role_to_system(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_developer_role",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 1,
                    "total_tokens": 10,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "input": [
                    {"role": "developer", "content": "You are a careful assistant."},
                    {"role": "user", "content": "Reply ok."},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.last_body["messages"][0]["role"], "system")
        self.assertEqual(upstream.last_body["messages"][1]["role"], "user")

    def test_chat_completions_endpoint_converts_developer_role_to_system(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_developer_chat",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "role normalized",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "messages": [
                    {"role": "developer", "content": "Use concise answers."},
                    {"role": "user", "content": "hello"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.last_body["messages"][0]["role"], "system")
        self.assertEqual(upstream.last_body["messages"][1]["role"], "user")

    def test_responses_endpoint_groups_consecutive_tool_calls_before_tool_outputs(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_grouped_tool_calls",
                "model": "ds-v4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("ds-v4",),
            upstream_model="ds-v4",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "tool_a",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "tool_b",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        },
                    },
                ],
                "input": [
                    {"role": "user", "content": "run tools"},
                    {"type": "function_call", "call_id": "call_a", "name": "tool_a", "arguments": "{}"},
                    {"type": "function_call", "call_id": "call_b", "name": "tool_b", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_a", "output": "A ok"},
                    {"type": "function_call_output", "call_id": "call_b", "output": "B ok"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(upstream.last_body["messages"]), 4)
        self.assertEqual(upstream.last_body["messages"][1]["role"], "assistant")
        self.assertEqual(len(upstream.last_body["messages"][1]["tool_calls"]), 2)
        self.assertEqual(upstream.last_body["messages"][1]["tool_calls"][0]["id"], "call_a")
        self.assertEqual(upstream.last_body["messages"][1]["tool_calls"][1]["id"], "call_b")
        self.assertEqual(upstream.last_body["messages"][2]["role"], "tool")
        self.assertEqual(upstream.last_body["messages"][2]["tool_call_id"], "call_a")
        self.assertEqual(upstream.last_body["messages"][3]["tool_call_id"], "call_b")

    def test_responses_endpoint_disables_default_thinking_for_tool_heavy_requests(self):
        upstream = FakeUpstreamClient(
            {
                "id": "chatcmpl_disable_thinking",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            }
        )
        settings = Settings(
            upstream_base_url="http://127.0.0.1:9999/v1",
            upstream_chat_path="/chat/completions",
            upstream_api_key=None,
            adapter_model_ids=("deepseek-v4-pro",),
            upstream_model="deepseek-v4-pro",
            request_timeout_seconds=30.0,
            synthesize_stream_chunk_size=8,
            forward_authorization=True,
            model_owner="deepseek",
        )
        app = create_app(settings=settings, upstream_client=upstream)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "reasoning": {"effort": "xhigh"},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "tool_a",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        },
                    }
                ],
                "input": [
                    {"role": "user", "content": "run tool"},
                    {"type": "function_call", "call_id": "call_a", "name": "tool_a", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_a", "output": "A ok"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.last_body["thinking"]["type"], "disabled")
        self.assertNotIn("reasoning_effort", upstream.last_body)


if __name__ == "__main__":
    unittest.main()
