import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("optimize_pipeline", PROJECT / "optimize-pipeline.py")
optimize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(optimize)


class OptimizeEntryTests(unittest.TestCase):
    def test_delegates_to_pipeline(self):
        url = "https://x.com/author/status/123"
        with patch.object(sys, "argv", ["optimize-pipeline.py", url]), \
                patch.object(optimize.pipeline, "process", return_value="Fatto. 1 prompt in 'T'.") as process:
            self.assertEqual(optimize.main(), 0)
        process.assert_called_once_with({"url": url})

    def test_usage_without_url(self):
        with patch.object(sys, "argv", ["optimize-pipeline.py"]):
            self.assertEqual(optimize.main(), 1)

    def test_pipeline_error_is_reported(self):
        with patch.object(sys, "argv", ["optimize-pipeline.py", "https://x.com/a/status/1"]), \
                patch.object(optimize.pipeline, "process", side_effect=optimize.pipeline.PipelineError("config.yaml assente", transient=False)):
            self.assertEqual(optimize.main(), 1)


if __name__ == "__main__":
    unittest.main()
