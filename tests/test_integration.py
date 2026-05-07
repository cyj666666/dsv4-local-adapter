from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import base64
import json

from ds_adapter.integration import (
    PROVIDER_KEY,
    apply_codex_config,
    adapter_v1_url,
    build_cc_provider_deeplink,
    build_cc_settings_config,
    patch_codex_config_text,
    test_codex_import_roundtrip,
)


class IntegrationTests(unittest.TestCase):
    def test_patch_codex_config_text_adds_local_provider(self):
        original = (
            'model_provider = "custom"\n'
            'model = "gpt-5.4"\n'
            '\n'
            '[model_providers.custom]\n'
            'name = "custom"\n'
            'base_url = "http://127.0.0.1:15721/v1"\n'
        )
        patched = patch_codex_config_text(original, "ds-v4")
        self.assertIn(f'model_provider = "{PROVIDER_KEY}"', patched)
        self.assertIn(f'base_url = "{adapter_v1_url()}"', patched)
        self.assertIn(f"[model_providers.{PROVIDER_KEY}]", patched)

    def test_codex_import_roundtrip_restores_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            original = 'model_provider = "custom"\nmodel = "gpt-5.4"\n'
            config_path.write_text(original, encoding="utf-8")

            result = test_codex_import_roundtrip(config_path, "ds-v4")

            self.assertTrue(result["ok"])
            self.assertTrue(result["import_ok"])
            self.assertTrue(result["restored_ok"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_apply_codex_config_persists_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            original = 'model_provider = "custom"\nmodel = "gpt-5.4"\n'
            config_path.write_text(original, encoding="utf-8")

            result = apply_codex_config(config_path, "deepseek-v4-pro")

            self.assertTrue(result["ok"])
            changed = config_path.read_text(encoding="utf-8")
            self.assertIn(f'model_provider = "{PROVIDER_KEY}"', changed)
            self.assertIn(f'base_url = "{adapter_v1_url()}"', changed)
            self.assertNotEqual(changed, original)

    def test_build_cc_provider_deeplink_contains_codex_provider_payload(self):
        deeplink = build_cc_provider_deeplink("deepseek-v4-flash", "sk-test-123")
        parsed = urlparse(deeplink)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "ccswitch")
        self.assertEqual(parsed.netloc, "v1")
        self.assertEqual(parsed.path, "/import")
        self.assertEqual(params["resource"], ["provider"])
        self.assertEqual(params["app"], ["codex"])
        self.assertEqual(params["endpoint"], [adapter_v1_url()])
        self.assertEqual(params["model"], ["deepseek-v4-flash"])
        self.assertEqual(params["configFormat"], ["json"])
        self.assertEqual(params["apiKey"], ["sk-test-123"])

        config_blob = base64.b64decode(params["config"][0]).decode("utf-8")
        self.assertEqual(config_blob, build_cc_settings_config("deepseek-v4-flash", "sk-test-123"))
        payload = json.loads(config_blob)
        self.assertIn(f'base_url = "{adapter_v1_url()}"', payload["config"])
        self.assertIn('requires_openai_auth = true', payload["config"])
        self.assertEqual(payload["auth"], {"OPENAI_API_KEY": "sk-test-123"})


if __name__ == "__main__":
    unittest.main()
