import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline  # noqa: E402

URL = "https://x.com/author/status/123"
TAG = "gemma4:12b-it-q4_K_M"


def extraction(count=2, topic="scrittura creativa"):
    return {"topic": topic, "prompts": [
        {
            "title_en": f"Prompt number {n}",
            "prompt_en": f"Act as an editor for [topic] and rewrite {{text}} number {n}.",
            "title_it": f"Prompt numero {n}",
            "prompt_it": f"Agisci come editor per [topic] e riscrivi {{text}} numero {n}.",
        } for n in range(1, count + 1)
    ]}


def chat_payload(structured=None, content=None, **extra):
    if content is None:
        content = json.dumps(structured if structured is not None else extraction())
    body = {
        "model": TAG,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 120,
    }
    body.update(extra)
    return body


class Response:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

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


def http_error(code, payload, url="http://127.0.0.1:11434/api/chat"):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return urllib.error.HTTPError(url, code, "error", hdrs=None, fp=io.BytesIO(raw))


class OllamaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "out"
        self.root.mkdir()
        cache = Path(self.temp.name) / "cache"
        self.requests = []
        for target, value in (
            ("output_directory", lambda: self.root),
            ("PROCESSED_INDEX", cache / "processed.json"),
            ("LOCK_FILE", cache / "pipeline.lock"),
            ("fetch_thread", lambda url: {"author": "some_author", "tweet_text": "Thread intro", "thread": []}),
        ):
            patcher = patch.object(pipeline, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(pipeline.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def settings(self, **overrides):
        base = {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": TAG,
            "timeout_seconds": 240,
            "num_ctx": 4096,
            "num_predict": 2048,
            "temperature": 0.0,
            "think": False,
            "keep_alive": "5m",
        }
        base.update(overrides)
        return base

    def run_process(self, *payloads, settings=None, job=None):
        replies = list(payloads)

        def urlopen(request, timeout=None):
            self.requests.append((request, timeout))
            if not replies:
                raise AssertionError("unexpected Ollama call")
            outcome = replies.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)

        with patch.object(pipeline, "provider_settings", return_value=settings or self.settings()), \
                patch.object(pipeline.urllib.request, "urlopen", side_effect=urlopen), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")) as claude:
            output = pipeline.process(job if job is not None else {"url": URL})
        return output, claude

    def test_valid_json_writes_the_same_bilingual_files(self):
        output, claude = self.run_process(chat_payload())
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa'.")
        english = self.root / "Scrittura-Creativa/Inglese/01-prompt-number-1.md"
        self.assertEqual(
            english.read_text(),
            "# Prompt number 1\n\nAct as an editor for [topic] and rewrite {text} number 1.\n",
        )
        self.assertIn("{text}", (self.root / "Scrittura-Creativa/Italiano/02-prompt-numero-2.md").read_text())
        claude.assert_not_called()
        request, timeout = self.requests[0]
        body = json.loads(request.data.decode())
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/chat"))
        self.assertEqual(timeout, 240)
        self.assertEqual(body["model"], TAG)
        self.assertIs(body["stream"], False)
        self.assertIs(body["think"], False)
        self.assertEqual(body["format"], pipeline.EXTRACTION_SCHEMA)
        self.assertEqual(body["options"]["num_ctx"], 4096)
        self.assertEqual(body["options"]["num_predict"], 2048)
        self.assertEqual(body["keep_alive"], "5m")
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][0]["content"], pipeline.SYSTEM_PROMPT)

    def test_fenced_json_is_accepted(self):
        content = "```json\n" + json.dumps(extraction()) + "\n```"
        output, claude = self.run_process(chat_payload(content=content))
        self.assertTrue(output.startswith("Fatto."))
        claude.assert_not_called()

    def test_malformed_json_retries_then_is_permanent_and_skips_claude(self):
        bad = chat_payload(content="not json at all")
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(bad, bad, bad)
        self.assertFalse(error.exception.transient)
        self.assertIn("non valida", str(error.exception))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_malformed_then_valid_does_not_need_three_calls(self):
        output, _ = self.run_process(chat_payload(content="nope"), chat_payload())
        self.assertTrue(output.startswith("Fatto."))
        self.assertEqual(len(self.requests), 2)

    def test_timeout_stays_transient_and_skips_claude(self):
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(socket.timeout("timed out"), socket.timeout("timed out"), socket.timeout("timed out"))
        self.assertTrue(error.exception.transient)
        self.assertIn("Nessun fallback", str(error.exception))
        self.assertEqual(len(self.requests), 3)

    def test_urlerror_timeout_is_transient(self):
        failure = urllib.error.URLError(TimeoutError("timed out"))
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(failure, failure, failure)
        self.assertTrue(error.exception.transient)

    def test_missing_model_is_permanent_after_one_call(self):
        failure = http_error(404, {"error": "model 'gemma4:12b-it-q4_K_M' not found"})
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(failure)
        self.assertFalse(error.exception.transient)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Nessun fallback", str(error.exception))

    def test_runner_failure_is_retried_without_claude(self):
        failures = [
            http_error(500, {"error": "llama runner process has terminated: out of memory"})
            for _ in range(3)
        ]
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(*failures)
        self.assertTrue(error.exception.transient)
        self.assertIn("out of memory", str(error.exception))
        self.assertEqual(len(self.requests), 3)

    def test_think_only_response_is_rejected(self):
        payload = chat_payload(content="  ")
        payload["message"]["thinking"] = "internal trace"
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(payload, payload, payload)
        self.assertIn("thinking", str(error.exception))
        self.assertFalse(error.exception.transient)

    def test_tool_call_is_rejected(self):
        payload = chat_payload()
        payload["message"]["tool_calls"] = [{"function": {"name": "write"}}]
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(payload, payload, payload)
        self.assertIn("tool_calls", str(error.exception))

    def test_length_stop_and_full_context_are_rejected(self):
        with self.assertRaises(pipeline.PipelineError):
            self.run_process(chat_payload(done_reason="length"), chat_payload(done_reason="length"), chat_payload(done_reason="length"))
        self.requests.clear()
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(chat_payload(prompt_eval_count=4096), chat_payload(prompt_eval_count=4096), chat_payload(prompt_eval_count=4096))
        self.assertIn("num_ctx", str(error.exception))

    def test_oversized_thread_is_not_sent(self):
        huge = "Prompt: " + ("rewrite the paragraph. " * 8000)

        def fetch(url):
            return {"author": "some_author", "tweet_text": huge, "thread": []}

        with patch.object(pipeline, "provider_settings", return_value=self.settings()), \
                patch.object(pipeline, "fetch_thread", fetch), \
                patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("sent")), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            with self.assertRaises(pipeline.PipelineError) as error:
                pipeline.process({"url": URL})
        self.assertFalse(error.exception.transient)
        self.assertIn("Nessun troncamento", str(error.exception))
        self.assertIn("262144", str(error.exception))

    def test_explicit_think_true_is_sent(self):
        output, _ = self.run_process(chat_payload(), settings=self.settings(think=True))
        self.assertTrue(output.startswith("Fatto."))
        body = json.loads(self.requests[0][0].data.decode())
        self.assertIs(body["think"], True)

    def test_processed_post_does_not_call_ollama_again(self):
        self.run_process(chat_payload())
        self.requests.clear()
        output, claude = self.run_process()
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa'.")
        self.assertEqual(self.requests, [])
        claude.assert_not_called()

    def test_saved_extraction_does_not_call_a_provider(self):
        job = {"url": URL, "author": "some_author", "extraction": extraction(), "topic": "Scrittura-Creativa"}
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("ollama")), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            output = pipeline.process(job)
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa'.")

    def test_one_local_call_at_a_time(self):
        state = {"current": 0, "max": 0}
        gate = threading.Lock()

        def urlopen(request, timeout=None):
            with gate:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
            try:
                time.sleep(0.05)
                return Response(chat_payload())
            finally:
                with gate:
                    state["current"] -= 1

        settings = self.settings()
        errors = []

        def worker():
            try:
                pipeline.run_ollama("Prompt: thanks [name].", [], settings)
            except Exception as exc:
                errors.append(exc)

        with patch.object(pipeline.urllib.request, "urlopen", side_effect=urlopen):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(state["max"], 1)


class ProviderConfigTests(unittest.TestCase):
    def write_config(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "config.yaml").write_text(text)
        patcher = patch.object(pipeline, "PROJECT_DIR", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def test_absent_config_stays_on_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pipeline, "PROJECT_DIR", Path(directory)):
                self.assertEqual(pipeline.provider_settings(), {
                    "provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
                })

    def test_blank_provider_stays_on_claude(self):
        self.write_config("output_dir: out\nprovider: ''\n")
        self.assertEqual(pipeline.provider_settings(), {
            "provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
        })

    def test_explicit_claude_ignores_a_broken_ollama_block(self):
        self.write_config("output_dir: out\nprovider: claude\nollama:\n  model: 'not a tag'\n")
        self.assertEqual(pipeline.provider_settings(), {
            "provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
        })

    def test_verified_tag_and_defaults_are_explicit(self):
        self.write_config(f"output_dir: out\nprovider: ollama\nollama:\n  model: {TAG}\n")
        settings = pipeline.provider_settings()
        self.assertEqual(settings["model"], TAG)
        self.assertEqual(settings["base_url"], "http://127.0.0.1:11434")
        self.assertEqual(settings["num_ctx"], 4096)
        self.assertEqual(settings["num_predict"], 2048)
        self.assertIs(settings["think"], False)
        self.assertEqual(settings["keep_alive"], "5m")
        body = pipeline.ollama_chat_body("Prompt: thanks [name].", [], settings)
        self.assertEqual(body["options"]["num_ctx"], 4096)
        self.assertIs(body["think"], False)
        self.assertNotIn("tools", body)

    def test_model_card_context_is_rejected(self):
        self.write_config(
            f"output_dir: out\nprovider: ollama\nollama:\n  model: {TAG}\n  num_ctx: 262144\n"
        )
        with self.assertRaises(pipeline.PipelineError) as error:
            pipeline.provider_settings()
        self.assertFalse(error.exception.transient)
        self.assertIn("262144", str(error.exception))
        self.assertIn("8192", str(error.exception))

    def test_missing_model_tag_is_rejected(self):
        self.write_config("output_dir: out\nprovider: ollama\nollama: {}\n")
        with self.assertRaises(pipeline.PipelineError) as error:
            pipeline.provider_settings()
        self.assertIn("ollama.model", str(error.exception))

    def test_remote_host_unload_and_non_boolean_think_are_rejected(self):
        cases = [
            f"provider: ollama\nollama:\n  model: {TAG}\n  base_url: http://example.com:11434\n",
            f"provider: ollama\nollama:\n  model: {TAG}\n  keep_alive: 0\n",
            f"provider: ollama\nollama:\n  model: {TAG}\n  keep_alive: '0s'\n",
            f"provider: ollama\nollama:\n  model: {TAG}\n  think: 'false'\n",
            "provider: somewhere\n",
        ]
        for text in cases:
            self.write_config("output_dir: out\n" + text)
            with self.assertRaises(pipeline.PipelineError):
                pipeline.provider_settings()

    def test_example_config_keeps_claude_and_documents_the_verified_tag(self):
        example = Path(__file__).resolve().parents[1] / "config.yaml.example"
        text = example.read_text()
        loaded = yaml.safe_load(text)
        self.assertEqual(set(loaded), {"output_dir"})
        self.assertIn(TAG, text)
        self.assertNotRegex(text, r"(?m)^provider:")
        self.assertNotRegex(text, r"(?m)^  model:")

    def test_tags_check_accepts_the_installed_name_without_generation(self):
        self.write_config(f"output_dir: out\nprovider: ollama\nollama:\n  model: {TAG}\n")
        seen = []

        def urlopen(request, timeout=None):
            seen.append((request.get_method(), request.full_url, timeout))
            return Response({"models": [{"name": TAG, "model": TAG}]})

        with patch.object(pipeline.urllib.request, "urlopen", side_effect=urlopen):
            detail = pipeline.check_ollama()
        self.assertIn(TAG, detail)
        self.assertEqual(seen, [("GET", "http://127.0.0.1:11434/api/tags", 10)])

    def test_tags_check_does_not_pull_a_missing_model(self):
        self.write_config(f"output_dir: out\nprovider: ollama\nollama:\n  model: {TAG}\n")

        def urlopen(request, timeout=None):
            self.assertNotIn("/api/pull", request.full_url)
            return Response({"models": [{"name": "other:latest"}]})

        with patch.object(pipeline.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(pipeline.PipelineError) as error:
                pipeline.check_ollama()
        self.assertIn("Nessun modello viene scaricato", str(error.exception))

    def test_explicit_claude_process_does_not_call_ollama(self):
        root = self.write_config("provider: claude\n")
        out = root / "out"
        out.mkdir()
        (root / "config.yaml").write_text(f"output_dir: \"{out}\"\nprovider: claude\nollama:\n  model: 'bad tag'\n")
        cache = root / "cache"
        body = {"is_error": False, "structured_output": extraction()}
        result = subprocess.CompletedProcess([], 0, json.dumps(body), "")
        with patch.object(pipeline, "PROCESSED_INDEX", cache / "processed.json"), \
                patch.object(pipeline, "LOCK_FILE", cache / "pipeline.lock"), \
                patch.object(pipeline, "CLAUDE_WORKDIR", root / "work"), \
                patch.object(pipeline, "fetch_thread", lambda url: {"author": "some_author", "tweet_text": "Hi", "thread": []}), \
                patch.object(pipeline.subprocess, "run", return_value=result) as claude, \
                patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("ollama")):
            output = pipeline.process({"url": URL})
        self.assertTrue(output.startswith("Fatto."))
        self.assertEqual(claude.call_count, 1)
        command = claude.call_args.args[0]
        self.assertEqual(command[command.index("--tools") + 1], "")


class SmokeTests(unittest.TestCase):
    def test_smoke_requires_ollama_and_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(pipeline, "PROJECT_DIR", root):
                with self.assertRaises(pipeline.PipelineError) as error:
                    pipeline.smoke_local_extraction()
        self.assertIn("claude", str(error.exception))

    def test_smoke_uses_the_local_model_and_the_pipeline_lock(self):
        root_dir = tempfile.TemporaryDirectory()
        self.addCleanup(root_dir.cleanup)
        root = Path(root_dir.name)
        (root / "config.yaml").write_text(f"output_dir: out\nprovider: ollama\nollama:\n  model: {TAG}\n")
        seen = {}

        def urlopen(request, timeout=None):
            seen["body"] = json.loads(request.data.decode())
            seen["locked"] = pipeline.LOCK_FILE.exists()
            return Response(chat_payload(extraction(1, topic="nota di prova")))

        with patch.object(pipeline, "PROJECT_DIR", root), \
                patch.object(pipeline, "LOCK_FILE", root / "pipeline.lock"), \
                patch.object(pipeline.urllib.request, "urlopen", side_effect=urlopen), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            extraction_result = pipeline.smoke_local_extraction()
        self.assertEqual(extraction_result["topic"], "nota di prova")
        self.assertEqual(seen["body"]["model"], TAG)
        self.assertIn("[name]", seen["body"]["messages"][1]["content"])
        self.assertTrue(seen["locked"])
        self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
