#!/usr/bin/env python3
"""Run the maintained prompt pipeline for one X post URL.

Same work as `prompt-listener.py --process`: fetch the public thread, ask the
configured provider once (Claude Code unless config.yaml selects Ollama), and
write bilingual files. Existing complete files are kept.
"""

import sys

import pipeline


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 optimize-pipeline.py <x-post-url>", file=sys.stderr)
        return 1
    try:
        print(pipeline.process({"url": sys.argv[1]}))
    except pipeline.PipelineError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
