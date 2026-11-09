import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline  # noqa: E402

URL = "https://x.com/author/status/123"


def extraction():
    return {"topic": "note", "prompts": [{
        "title_en": "Thanks",
        "prompt_en": "Write a two-sentence thank-you note to [name].",
        "title_it": "Grazie",
        "prompt_it": "Scrivi una nota di ringraziamento di due frasi per [name].",
    }]}


def completed(structured, **extra):
    body = {"is_error": False, "structured_output": structured, **extra}
    return subprocess.CompletedProcess([], 0, json.dumps(body), "")


class ClaudeConfigTests(unittest.TestCase):
    def use(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        if text is not None:
            (root / "config.yaml").write_text(text)
        patcher = patch.object(pipeline, "PROJECT_DIR", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def test_missing_config_selects_sonnet_medium(self):
        self.use(None)
        settings = pipeline.provider_settings()
        self.assertEqual(settings["provider"], "claude")
        self.assertEqual(settings["model"], "claude-sonnet-5-5")
        self.assertEqual(settings["effort"], "medium")

    def test_explicit_block_is_independent_of_settings_json(self):
        root = self.use("provider: claude\nclaude:\n  model: claude-sonnet-5-5\n  effort: medium\n")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text('{"model":"haiku"}\n')
        settings = pipeline.provider_settings()
        self.assertEqual(settings["model"], "claude-sonnet-5-5")
        self.assertEqual(settings["effort"], "medium")

    def test_haiku_and_bad_effort_are_rejected(self):
        for text in (
            "provider: claude\nclaude:\n  model: haiku\n",
            "provider: claude\nclaude:\n  model: claude-haiku-4-5-20251001\n",
            "provider: claude\nclaude:\n  effort: ultra\n",
        ):
            self.use(text)
            with self.assertRaises(pipeline.PipelineError):
                pipeline.provider_settings()

    def test_ollama_config_does_not_become_sonnet(self):
        self.use("provider: ollama\nollama:\n  model: gemma4:12b-it-q4_K_M\n  num_ctx: 8192\n")
        settings = pipeline.provider_settings()
        self.assertEqual(settings["provider"], "ollama")
        self.assertEqual(settings["model"], "gemma4:12b-it-q4_K_M")


class ClaudeRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.out = root / "out"
        self.out.mkdir()
        for target, value in (
            ("output_directory", lambda: self.out),
            ("PROCESSED_INDEX", root / "processed.json"),
            ("LOCK_FILE", root / "lock"),
            ("CLAUDE_WORKDIR", root / "work"),
            ("fetch_thread", lambda url: {"author": "a", "tweet_text": "Write a two-sentence thank-you note to [name].", "thread": []}),
            ("provider_settings", lambda: {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}),
        ):
            patcher = patch.object(pipeline, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(pipeline.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_quota_error_is_a_single_permanent_failure(self):
        failure = subprocess.CompletedProcess([], 1, json.dumps({
            "is_error": True,
            "result": "You've hit your limit for the week.",
        }), "")
        with patch.object(pipeline.subprocess, "run", side_effect=[failure, failure]) as run:
            with self.assertRaises(pipeline.PipelineError) as error:
                pipeline.process({"url": URL})
        self.assertFalse(error.exception.transient)
        self.assertIn("06:00 Europe/Rome", str(error.exception))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_screenshot_metrics_stay_in_the_log(self):
        prompts = [{
            "title_en": f"Prompt {n}",
            "prompt_en": f"Write prompt number {n} for [name] with {{detail}}.",
            "title_it": f"Prompt {n}",
            "prompt_it": f"Scrivi il prompt numero {n} per [name] con {{detail}}.",
        } for n in range(1, 9)]
        result = completed(
            {"topic": "entertainment gratuito claude", "prompts": prompts},
            usage={
                "input_tokens": 2,
                "output_tokens": 2161,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 251659,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
            total_cost_usd=1.02825,
            duration_ms=16017,
            duration_api_ms=15972,
            modelUsage={"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 2161}},
        )
        with self.assertLogs("promptx", level="INFO") as captured, \
                patch.object(pipeline.subprocess, "run", return_value=result):
            output = pipeline.process({"url": URL})
        self.assertEqual(
            output,
            "Fatto. 8 prompt in 'Entertainment-Gratuito-Claude'.\n"
            "Modello Sonnet 5.5, effort medium, 16 secondi.\n"
            "Finestra 5h: non disponibile.",
        )
        self.assertNotIn("251659", output)
        self.assertNotIn("1.02825", output)
        self.assertNotIn("cost", output.lower())
        self.assertNotIn("%", output)
        logged = "\n".join(captured.output)
        self.assertIn('"input_tokens": 2', logged)
        self.assertIn('"output_tokens": 2161', logged)
        self.assertIn('"cache_read_input_tokens": 0', logged)
        self.assertIn('"cache_creation_input_tokens": 251659', logged)
        self.assertIn('"total_cost_usd": 1.02825', logged)
        self.assertIn("cli_list_price_estimate_not_pro_charge", logged)
        self.assertIn('"duration_ms": 16017', logged)
        self.assertIn('"duration_api_ms": 15972', logged)
        self.assertIn('"quota_5h_percent": null', logged)

    def test_absent_usage_adds_no_stats_line(self):
        with patch.object(pipeline.subprocess, "run", return_value=completed(extraction())):
            output = pipeline.process({"url": URL})
        self.assertEqual(output, "Fatto. 1 prompt in 'Note'.")

    def test_zero_counts_are_logged_and_not_shown(self):
        result = completed(
            extraction(),
            usage={"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            duration_ms=400,
        )
        with self.assertLogs("promptx", level="INFO") as captured, \
                patch.object(pipeline.subprocess, "run", return_value=result):
            output = pipeline.process({"url": URL})
        self.assertIn("meno di un secondo", output)
        self.assertNotIn("cache", output)
        self.assertIn('"input_tokens": 0', "\n".join(captured.output))
        self.assertIn('"cache_read_input_tokens": 0', "\n".join(captured.output))

    def test_plain_text_preserves_symbols_and_stays_short(self):
        text = pipeline.format_claude_stats(
            {"duration_ms": 2000, "effective_model": "custom_model*v2", "output_tokens": 10**12},
            "custom_model*v2",
            "medium",
        )
        self.assertIn("custom_model*v2", text)
        self.assertNotIn(str(10**12), text)
        self.assertLess(len(text), 200)
        data = extraction()
        data["topic"] = "show_night *live* <tv>"
        result = completed(data, usage={"input_tokens": 2, "output_tokens": 9, "cache_creation_input_tokens": 10**12}, duration_ms=16017)
        with patch.object(pipeline.subprocess, "run", return_value=result):
            output = pipeline.process({"url": URL})
        self.assertIn("Show-Night-Live-Tv", output)
        self.assertNotIn("*", output)
        self.assertNotIn("<", output)
        self.assertNotIn(str(10**12), output)
        self.assertLess(len(output), 400)

    def test_official_five_hour_percent_is_copied_when_present(self):
        result = completed(
            extraction(),
            usage={"input_tokens": 10, "output_tokens": 4},
            total_cost_usd=9.5,
            duration_ms=1000,
            rate_limits={"five_hour": {"used_percentage": 37, "resets_at": 1}},
        )
        with patch.object(pipeline.subprocess, "run", return_value=result):
            output = pipeline.process({"url": URL})
        self.assertIn("Finestra 5h: 37%.", output)
        self.assertNotIn("non disponibile", output)
        self.assertNotIn("9.5", output)
        self.assertNotIn("cost", output.lower())


if __name__ == "__main__":
    unittest.main()
