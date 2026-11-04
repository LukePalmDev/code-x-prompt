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


def extraction(count=2, topic="scrittura creativa"):
    return {"topic": topic, "prompts": [
        {
            "title_en": f"Prompt number {n}",
            "prompt_en": f"Act as an editor for [topic] and rewrite {{text}} number {n}.",
            "title_it": f"Prompt numero {n}",
            "prompt_it": f"Agisci come editor per [topic] e riscrivi {{text}} numero {n}.",
        } for n in range(1, count + 1)
    ]}


def claude_result(structured=None, **response):
    body = {"is_error": False, "structured_output": structured, **response}
    return subprocess.CompletedProcess([], 0, json.dumps(body), "")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "out"
        self.root.mkdir()
        cache = Path(self.temp.name) / "cache"
        for target, value in (
            ("output_directory", lambda: self.root),
            ("PROCESSED_INDEX", cache / "processed.json"),
            ("LOCK_FILE", cache / "pipeline.lock"),
            ("CLAUDE_WORKDIR", Path(self.temp.name) / "work"),
            ("configured_model", lambda: "claude-haiku-test"),
            ("fetch_thread", lambda url: {"author": "some_author", "tweet_text": "Thread intro", "thread": []}),
        ):
            patcher = patch.object(pipeline, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(pipeline.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_process(self, *results, job=None):
        with patch.object(pipeline.subprocess, "run", side_effect=results) as run:
            output = pipeline.process(job if job is not None else {"url": URL})
        return output, run

    def test_process_writes_bilingual_files(self):
        output, run = self.run_process(claude_result(extraction()))
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa'.")
        english = self.root / "Scrittura-Creativa/Inglese/01-prompt-number-1.md"
        italian = self.root / "Scrittura-Creativa/Italiano/02-prompt-numero-2.md"
        self.assertEqual(english.read_text(), "# Prompt number 1\n\nAct as an editor for [topic] and rewrite {text} number 1.\n")
        self.assertIn("{text}", italian.read_text())
        self.assertEqual(run.call_count, 1)

    def test_claude_runs_without_tools_outside_project(self):
        _, run = self.run_process(claude_result(extraction()))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--json-schema", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-haiku-test")
        self.assertEqual(command[command.index("--fallback-model") + 1], "sonnet")
        self.assertNotEqual(run.call_args.kwargs["cwd"], pipeline.PROJECT_DIR)

    def test_invalid_answer_retries_with_fallback_model(self):
        _, run = self.run_process(claude_result(None), claude_result(extraction()))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertNotIn("--fallback-model", command)

    def test_retired_model_uses_fallback_model(self):
        retired = claude_result(result="There's an issue with the selected model (old). It may not exist.", is_error=True)
        output, run = self.run_process(retired, claude_result(extraction()))
        self.assertTrue(output.startswith("Fatto."))
        self.assertEqual(run.call_count, 2)

    def test_auth_error_is_permanent(self):
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(claude_result(result="Not logged in", is_error=True))
        self.assertFalse(error.exception.transient)
        self.assertIn("Not logged in", str(error.exception))

    def test_network_error_stays_transient_after_retries(self):
        failure = claude_result(result="API Error: Connection error.", is_error=True)
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(failure, failure, failure)
        self.assertTrue(error.exception.transient)

    def test_timeout_is_transient(self):
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(*[subprocess.TimeoutExpired("claude", 600)] * 3)
        self.assertTrue(error.exception.transient)

    def test_truncated_translation_is_rejected(self):
        data = extraction(1)
        data["prompts"][0]["prompt_it"] = "Agisci"
        with self.assertRaisesRegex(ValueError, "troncata"):
            pipeline.validate_extraction(data)

    def test_no_prompts_is_permanent_and_writes_nothing(self):
        with self.assertRaises(pipeline.PipelineError) as error:
            self.run_process(claude_result({"topic": "Vuoto", "prompts": []}))
        self.assertFalse(error.exception.transient)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_folder_is_never_reused_for_new_post(self):
        (self.root / "scrittura-creativa").mkdir()
        output, _ = self.run_process(claude_result(extraction()))
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa-SomeAuthor'.")

    def test_processed_post_skips_claude(self):
        self.run_process(claude_result(extraction()))
        output, run = self.run_process()
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa'.")
        self.assertEqual(run.call_count, 0)

    def test_progress_is_saved_before_writing(self):
        saved = []
        with patch.object(pipeline.subprocess, "run", return_value=claude_result(extraction())):
            pipeline.process({"url": URL}, save=lambda job: saved.append(dict(job)))
        self.assertIn("extraction", saved[0])
        self.assertEqual(saved[-1]["topic"], "Scrittura-Creativa")

    def test_resume_completes_partial_folder_without_claude(self):
        folder = self.root / "Scrittura-Creativa/Inglese"
        folder.mkdir(parents=True)
        complete = folder / "01-custom-name.md"
        complete.write_text("# Kept\n\nOriginal content that must stay.\n")
        (folder / "02-prompt-number-2.md").write_text("")
        job = {"url": URL, "author": "a", "extraction": extraction(), "topic": "Scrittura-Creativa"}
        output, run = self.run_process(job=job)
        self.assertEqual(output, "Fatto. 2 prompt in 'Scrittura-Creativa'.")
        self.assertEqual(run.call_count, 0)
        self.assertEqual(complete.read_text(), "# Kept\n\nOriginal content that must stay.\n")
        self.assertIn("number 2", (folder / "02-prompt-number-2.md").read_text())
        self.assertEqual(len(list(folder.glob("02-*.md"))), 1)

    def test_names_are_sanitized(self):
        self.assertEqual(pipeline.choose_topic("ricerca lavoro con ChatGPT", "x", "1", []), "Ricerca-Lavoro-Con-ChatGPT")
        self.assertEqual(pipeline.choose_topic("../Città", "x", "1", []), "Citta")
        self.assertEqual(pipeline.slugify("Città & Lavoro: il piano!"), "citta-lavoro-il-piano")

    def test_cloud_only_files_are_verified_by_size(self):
        self.run_process(claude_result(extraction()))
        for file in self.root.glob("*/*/*.md"):
            file.write_text("# Titolo\n\n" + "Prompt completo [topic]. " * 4)
        evicted = OSError(pipeline.errno.EDEADLK, "Resource deadlock avoided")
        with patch.object(pipeline.Path, "read_text", side_effect=evicted):
            pipeline.verify_output("Fatto. 2 prompt in 'Scrittura-Creativa'.")

    def test_cloud_only_truncated_file_is_rejected(self):
        self.run_process(claude_result(extraction()))
        (self.root / "Scrittura-Creativa/Italiano/01-prompt-numero-1.md").write_text("# Titolo\n")
        evicted = OSError(pipeline.errno.EDEADLK, "Resource deadlock avoided")
        with patch.object(pipeline.Path, "read_text", side_effect=evicted):
            with self.assertRaisesRegex(ValueError, "File vuoto"):
                pipeline.verify_output("Fatto. 2 prompt in 'Scrittura-Creativa'.")

    def test_topic_cannot_escape_output_root(self):
        with self.assertRaisesRegex(ValueError, "Cartella topic"):
            pipeline.verify_output("Fatto. 1 prompt in '../Scrittura'.")

    def test_missing_output_folder_is_permanent(self):
        missing = self.root / "missing"
        with patch.object(pipeline, "output_directory", lambda: missing):
            with self.assertRaises(pipeline.PipelineError) as error:
                pipeline.process({"url": URL})
        self.assertFalse(error.exception.transient)
        self.assertIn("assente", str(error.exception))


class ConfigTests(unittest.TestCase):
    def test_missing_config_is_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pipeline, "PROJECT_DIR", Path(directory)):
                with self.assertRaises(pipeline.PipelineError) as error:
                    pipeline.output_directory()
        self.assertFalse(error.exception.transient)
        self.assertIn("config.yaml", str(error.exception))

    def test_missing_output_dir_is_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("other: 1\n")
            with patch.object(pipeline, "PROJECT_DIR", root):
                with self.assertRaises(pipeline.PipelineError) as error:
                    pipeline.output_directory()
        self.assertFalse(error.exception.transient)
        self.assertIn("output_dir", str(error.exception))

    def test_output_dir_accepts_relative_absolute_and_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            with patch.object(pipeline, "PROJECT_DIR", root):
                config.write_text('output_dir: "out"\n')
                self.assertEqual(pipeline.output_directory(), root / "out")
                config.write_text('output_dir: "/tmp/prompts"\n')
                self.assertEqual(pipeline.output_directory(), Path("/tmp/prompts"))
                config.write_text('output_dir: "~/prompts"\n')
                self.assertEqual(pipeline.output_directory(), Path.home() / "prompts")


if __name__ == "__main__":
    unittest.main()
