"""Provider selection stays inside a temp config. No .env and no live model."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
os.environ["PROMPTX_SKIP_DOTENV"] = "1"
import pipeline  # noqa: E402

spec = importlib.util.spec_from_file_location("listener_hermetic", PROJECT / "prompt-listener.py")
listener = importlib.util.module_from_spec(spec)
spec.loader.exec_module(listener)

TAG = "gemma4:12b-it-q4_K_M"


class IsolatedConfigTests(unittest.TestCase):
    def write(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        if text is not None:
            (root / "config.yaml").write_text(text)
        patcher = patch.object(pipeline, "PROJECT_DIR", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def test_missing_config_selects_claude_without_network(self):
        self.write(None)
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("ollama")), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            self.assertEqual(pipeline.provider_settings(), {"provider": "claude"})

    def test_ollama_config_selects_the_tag_without_calling_it(self):
        self.write(f"output_dir: /tmp/prompts\nprovider: ollama\nollama:\n  model: {TAG}\n")
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("ollama")), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            settings = pipeline.provider_settings()
        self.assertEqual(settings["provider"], "ollama")
        self.assertEqual(settings["model"], TAG)
        self.assertEqual(settings["num_ctx"], 4096)
        self.assertIs(settings["think"], False)

    def test_check_uses_only_the_injected_directory(self):
        claude_root = self.write("output_dir: /tmp/prompts\nprovider: claude\n")
        with patch.object(listener.pipeline, "PROJECT_DIR", claude_root), \
                patch.object(listener, "check_output", return_value="ok"), \
                patch.object(listener, "check_runtime", return_value="ok"), \
                patch.object(listener, "BOT_TOKEN", "test"), \
                patch.object(listener.pipeline.urllib.request, "urlopen", side_effect=AssertionError("ollama")), \
                patch.object(listener.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '{"loggedIn":false}', "")):
            self.assertFalse(listener.check_setup())

        ollama_root = self.write(f"output_dir: /tmp/prompts\nprovider: ollama\nollama:\n  model: {TAG}\n")

        def urlopen(request, timeout=None):
            self.assertEqual(request.get_method(), "GET")
            self.assertTrue(request.full_url.endswith("/api/tags"))
            body = json.dumps({"models": [{"name": TAG}]}).encode()
            return _Body(body)

        with patch.object(listener.pipeline, "PROJECT_DIR", ollama_root), \
                patch.object(listener, "check_output", return_value="ok"), \
                patch.object(listener, "check_runtime", return_value="ok"), \
                patch.object(listener, "BOT_TOKEN", "test"), \
                patch.object(listener.pipeline.urllib.request, "urlopen", side_effect=urlopen), \
                patch.object(listener.subprocess, "run", side_effect=AssertionError("claude")):
            self.assertTrue(listener.check_setup())


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self, n=-1):
        if n is None or n < 0 or n >= len(self.raw):
            data, self.raw = self.raw, b""
            return data
        data, self.raw = self.raw[:n], self.raw[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


if __name__ == "__main__":
    unittest.main()
