import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline  # noqa: E402

URL = "https://x.com/author/status/123"


def settings(**overrides):
    base = {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "gemma4:12b-it-q4_K_M",
        "timeout_seconds": 30,
        "num_ctx": 8192,
        "num_predict": 2048,
        "temperature": 0.0,
        "think": False,
        "keep_alive": "5m",
    }
    base.update(overrides)
    return base


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


def prompt_from(line):
    return {
        "title_en": "Note",
        "prompt_en": line,
        "title_it": "Nota",
        "prompt_it": "Traduzione completa e non troncata di: " + line,
    }


def payload_for(text):
    lines = [line for line in text.splitlines() if line.startswith("Prompt:")]
    body = {"topic": "Note di prova", "prompts": [prompt_from(line) for line in lines]}
    return {
        "message": {"role": "assistant", "content": json.dumps(body)},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 80,
    }


class SegmentPlanTests(unittest.TestCase):
    def test_slices_cover_the_original_at_4096_and_8192(self):
        source, planted = pipeline.smoke_long_source()
        self.assertGreater(len(source), 8000)
        for ctx in (4096, 8192):
            segments = pipeline.plan_segments(source, [], settings(num_ctx=ctx))
            self.assertGreaterEqual(len(segments), 2)
            self.assertEqual("".join(source[start:end] for start, end in segments), source)
            self.assertEqual(segments[0][0], 0)
            self.assertEqual(segments[-1][1], len(source))
            for left, right in zip(segments, segments[1:]):
                self.assertEqual(left[1], right[0])
            self.assertTrue(all(prompt in source for prompt in planted))

    def test_exact_duplicate_and_boundary_fragment_collapse(self):
        short = "Prompt: Write a note to [name]."
        longer = short + " Include {detail} and keep the closing line intact."
        other = "Prompt: List three colors for {topic}."
        kept = pipeline.prefer_complete_prompts([
            prompt_from(short),
            prompt_from(longer),
            prompt_from(short),
            prompt_from(other),
        ])
        texts = [item["prompt_en"] for item in kept]
        self.assertEqual(texts, [longer, other])

    def test_single_prompt_predict_stays_inside_the_cap(self):
        medium = "Prompt: " + ("rewrite this paragraph. " * 280)
        chosen = pipeline.predict_for_single_prompt(settings(), medium)
        self.assertGreater(chosen, 2048)
        self.assertLessEqual(chosen, pipeline.OLLAMA_NUM_PREDICT_MAX)
        huge = "Prompt: " + ("rewrite this paragraph. " * 800)
        with self.assertRaises(pipeline.PipelineError) as error:
            pipeline.predict_for_single_prompt(settings(), huge)
        self.assertFalse(error.exception.transient)
        self.assertIn(str(pipeline.OLLAMA_NUM_PREDICT_MAX), str(error.exception))
        self.assertIn("262144", str(error.exception))


class SegmentExtractionTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.active = 0
        self.max_active = 0
        sleep = patch.object(pipeline.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def serve(self, decide):
        def urlopen(request, timeout=None):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                body = json.loads(request.data.decode())
                self.calls.append(body)
                self.assertNotIn("tools", body)
                self.assertLessEqual(body["options"]["num_predict"], pipeline.OLLAMA_NUM_PREDICT_MAX)
                time.sleep(0.01)
                return Response(decide(body))
            finally:
                self.active -= 1

        return urlopen

    def test_long_input_is_segmented_once_each_and_deduped(self):
        source, planted = pipeline.smoke_long_source()
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=self.serve(lambda body: payload_for(body["messages"][1]["content"]))), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            extraction = pipeline.extract_with_ollama(source, [], settings())
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertEqual(self.max_active, 1)
        self.assertEqual([item["prompt_en"] for item in extraction["prompts"]], planted)
        self.assertTrue(all(item["options"]["num_ctx"] == 8192 for item in self.calls))
        self.assertTrue(all(item["options"]["num_predict"] == 2048 for item in self.calls))
        sources = [item["messages"][1]["content"].split("Post e thread X:\n\n", 1)[-1] for item in self.calls]
        self.assertTrue(all(part and part in source for part in sources))

    def test_length_stop_splits_instead_of_repeating(self):
        text = "\n\n".join(f"Block {index} stays intact for the model." for index in range(8))

        def decide(body):
            source = body["messages"][1]["content"].split("Post e thread X:\n\n", 1)[-1]
            if source.count("Block") > 2:
                return {
                    "message": {"role": "assistant", "content": ""},
                    "done": True, "done_reason": "length", "prompt_eval_count": 100,
                }
            return payload_for("Prompt: Keep block boundaries verbatim.")

        with patch.object(pipeline.urllib.request, "urlopen", side_effect=self.serve(decide)), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            extraction = pipeline.extract_text(text, [], settings())
        bodies = [json.dumps(item["messages"][1]["content"]) for item in self.calls]
        self.assertEqual(len(bodies), len(set(bodies)))
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertTrue(extraction["prompts"])
        self.assertNotIn("claude", extraction["topic"].lower())

    def test_partial_segment_failure_writes_nothing(self):
        source, _planted = pipeline.smoke_long_source()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        out = root / "out"
        out.mkdir()
        state = {"n": 0}

        def decide(body):
            state["n"] += 1
            if state["n"] == 1:
                return payload_for(body["messages"][1]["content"])
            raise socket.timeout("timed out")

        with patch.object(pipeline, "provider_settings", return_value=settings()), \
                patch.object(pipeline, "output_directory", lambda: out), \
                patch.object(pipeline, "PROCESSED_INDEX", root / "processed.json"), \
                patch.object(pipeline, "LOCK_FILE", root / "lock"), \
                patch.object(pipeline, "fetch_thread", lambda url: {"author": "a", "tweet_text": source, "thread": []}), \
                patch.object(pipeline.urllib.request, "urlopen", side_effect=self.serve(decide)), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("claude")):
            with self.assertRaises(pipeline.PipelineError) as error:
                pipeline.process({"url": URL})
        self.assertTrue(error.exception.transient)
        self.assertEqual(list(out.iterdir()), [])
        self.assertGreaterEqual(state["n"], 2)
        self.assertLess(state["n"], 8)


if __name__ == "__main__":
    unittest.main()
