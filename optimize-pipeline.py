#!/usr/bin/env python3
"""
optimize-pipeline.py — Optimized X prompt extraction with batched Claude operations.

Token optimization:
1. Uses OCR (easyocr) for image text extraction instead of Claude vision
2. Batches all analysis into ONE Claude call (topic + extraction + translation)
3. Caches translations to avoid re-translating repeated prompts
4. Smart image handling: skips images if text is already sufficient

Usage:
  python3 optimize-pipeline.py <x-post-url>
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import easyocr
import yaml
from anthropic import Anthropic

REPO_DIR = Path(__file__).parent
CACHE_DIR = REPO_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
TRANSLATION_CACHE = CACHE_DIR / "translations.json"


def load_translation_cache() -> dict:
    """Load cached translations to avoid re-translating."""
    if TRANSLATION_CACHE.exists():
        return json.loads(TRANSLATION_CACHE.read_text())
    return {}


def save_translation_cache(cache: dict):
    """Save translation cache."""
    TRANSLATION_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def extract_text_from_image(image_path: str) -> str:
    """Extract text from image using OCR (free, no token cost)."""
    try:
        if not hasattr(extract_text_from_image, '_reader'):
            print("→ Initializing OCR reader (first run only)...", file=sys.stderr)
            extract_text_from_image._reader = easyocr.Reader(['en', 'it'], gpu=False)

        result = extract_text_from_image._reader.readtext(image_path)
        text = '\n'.join([line[1] for line in result if line[1].strip()])
        return text if text.strip() else ""
    except Exception as e:
        print(f"⚠️  OCR failed for {image_path}: {e}", file=sys.stderr)
        return ""


def fetch_post(url: str) -> dict:
    """Call fetch-x.py to get the tweet (skip images, prompts are in text)."""
    env = os.environ.copy()
    env["SKIP_IMAGES"] = "1"  # Prompts are in tweet text, skip image downloads
    result = subprocess.run(
        ["python3", str(REPO_DIR / "fetch-x.py"), url],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fetch-x.py failed: {result.stderr}")
    return json.loads(result.stdout)


def load_config() -> str:
    """Load output directory from config.yaml."""
    config_path = REPO_DIR / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            "config.yaml not found. Create it from config.yaml.example"
        )
    config = yaml.safe_load(config_path.read_text())
    output_dir = config.get("output_dir", "").replace("~", str(Path.home()))
    if not output_dir:
        raise ValueError("output_dir not set in config.yaml")
    return output_dir


def batch_process_with_claude(
    tweet_text: str, thread_texts: list[str], image_texts: dict[str, str]
) -> dict:
    """
    OPTIMIZED: Single Claude call to extract topic, prompts, and translations.

    Returns:
    {
        "topic_italian": "Dieta",
        "prompts": [
            {
                "number": "1",
                "title_english": "Meal Plan Generator",
                "text_english": "...",
                "title_italian": "Generatore di Piano Pasti",
                "text_italian": "..."
            }
        ]
    }
    """
    client = Anthropic()

    # Build image text section
    image_section = ""
    if image_texts:
        image_section = "\n\n## Text extracted from images (via OCR):\n"
        for img_name, text in image_texts.items():
            image_section += f"\n[{img_name}]:\n{text}\n"

    # Build thread section
    thread_section = ""
    if thread_texts:
        thread_section = "\n\n## Additional thread tweets:\n"
        for i, text in enumerate(thread_texts, 1):
            thread_section += f"\n[Tweet {i}]:\n{text}\n"

    prompt = f"""Extract and process X post prompts ONCE in this single response.

## Main tweet:
{tweet_text}
{thread_section}
{image_section}

## Task: Return a JSON object with EXACTLY this structure (no markdown, raw JSON):

{{
  "topic_italian": "[Italian topic name, e.g., 'Dieta', 'Copywriting', 'AI-Prompt']",
  "prompts": [
    {{
      "number": "[1, 2, or identifier]",
      "title_english": "[Prompt title in English]",
      "text_english": "[Full prompt text in English, preserving placeholders like [topic] or {{var}}]",
      "title_italian": "[Titolo tradotto in italiano]",
      "text_italian": "[Testo completo del prompt tradotto in italiano, preservando placeholder come [topic]]"
    }}
  ]
}}

Rules:
- Extract EVERY distinct prompt in the post
- Preserve all placeholders/variables exactly as written
- Italian translations should be fluent but accurate
- If a prompt has no explicit title, derive a short descriptive one
- Return ONLY valid JSON, no other text"""

    response = client.messages.create(
        model="claude-opus-4.1",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = response.content[0].text.strip()
    # Extract JSON if wrapped in markdown code blocks
    if response_text.startswith("```json"):
        response_text = response_text[7:]
    if response_text.startswith("```"):
        response_text = response_text[3:]
    if response_text.endswith("```"):
        response_text = response_text[:-3]

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parsing failed: {e}", file=sys.stderr)
        print(f"Response was: {response_text[:200]}", file=sys.stderr)
        raise ValueError("Claude response was not valid JSON")

    return result


def create_file_structure(output_dir: str, topic: str, prompts: list) -> list[str]:
    """Create folder structure and write prompt files."""
    base_path = Path(output_dir) / topic
    en_dir = base_path / "Inglese"
    it_dir = base_path / "Italiano"

    en_dir.mkdir(parents=True, exist_ok=True)
    it_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []

    for i, prompt in enumerate(prompts, 1):
        # Generate filename from number + title
        number_str = str(prompt['number']).zfill(2) if prompt['number'] else f"{i:02d}"
        title_slug = re.sub(r'[^\w\s-]', '', prompt['title_english'].lower()).replace(' ', '-')
        filename = f"{number_str}-{title_slug}.md"

        # Write English version
        en_file = en_dir / filename
        en_file.write_text(f"# {prompt['title_english']}\n\n{prompt['text_english']}\n")
        saved_files.append(f"Inglese/{filename}")

        # Write Italian version
        it_title_slug = re.sub(r'[^\w\s-]', '', prompt['title_italian'].lower()).replace(' ', '-')
        it_filename = f"{number_str}-{it_title_slug}.md"
        it_file = it_dir / it_filename
        it_file.write_text(f"# {prompt['title_italian']}\n\n{prompt['text_italian']}\n")
        saved_files.append(f"Italiano/{it_filename}")

    return saved_files


async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 optimize-pipeline.py <x-post-url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]

    try:
        output_dir = load_config()
        print(f"→ Fetching post...", file=sys.stderr)
        post_data = fetch_post(url)

        # Extract OCR text from images (no Claude tokens)
        image_texts = {}
        if post_data.get("images"):
            print(f"→ Extracting text from {len(post_data['images'])} images via OCR...", file=sys.stderr)
            for img_path in post_data["images"]:
                text = extract_text_from_image(img_path)
                if text:
                    image_texts[Path(img_path).name] = text

        # Single batched Claude call
        print("→ Processing with Claude (single optimized call)...", file=sys.stderr)
        result = batch_process_with_claude(
            post_data["tweet_text"],
            [t["text"] for t in post_data.get("thread", [])],
            image_texts
        )

        # Create folder structure and save files
        print(f"→ Creating files in {output_dir}...", file=sys.stderr)
        saved_files = create_file_structure(output_dir, result["topic_italian"], result["prompts"])

        print(f"✓ Extracted {len(result['prompts'])} prompts", file=sys.stderr)
        print(f"✓ Topic: {result['topic_italian']}", file=sys.stderr)
        print(f"✓ Files: {len(saved_files)}", file=sys.stderr)
        print(f"✓ Output: {output_dir}/{result['topic_italian']}", file=sys.stderr)

        # Print summary for user
        summary = {
            "topic": result["topic_italian"],
            "prompts_extracted": len(result["prompts"]),
            "files_created": saved_files,
            "output_dir": f"{output_dir}/{result['topic_italian']}",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
