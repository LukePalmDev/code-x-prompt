#!/usr/bin/env python3
"""
fetch-x.py — Fetch a tweet + full thread from X without a browser.
Uses twscrape (manages X auth tokens automatically).

Usage:
  python3 fetch-x.py <x-post-url>

Output (stdout): JSON with keys:
  tweet_text   – text of the main tweet
  thread       – list of {id, text, images} for thread tweets (same author)
  images       – list of local paths to downloaded images
  author       – @username of the post author
"""

import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

import twscrape
from twscrape import API, gather

REPO_DIR = Path(__file__).parent
DB_PATH = REPO_DIR / ".twscrape_accounts.db"
IMG_DIR = REPO_DIR / ".img_cache"
IMG_DIR.mkdir(exist_ok=True)


# ─── helpers ─────────────────────────────────────────────────────────────────

def extract_tweet_id(url: str) -> int:
    m = re.search(r"/status/(\d+)", url)
    if not m:
        raise ValueError(f"Cannot extract tweet ID from: {url}")
    return int(m.group(1))


def download_image(url: str, tweet_id: int, idx: int) -> str:
    fname = IMG_DIR / f"{tweet_id}_{idx}.jpg"
    if not fname.exists():
        req = urllib.request.Request(
            url + "?format=jpg&name=large",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req) as r, open(fname, "wb") as f:
            f.write(r.read())
    return str(fname)


def collect_images(tweet) -> list[str]:
    paths = []
    if tweet.media and tweet.media.photos:
        for i, photo in enumerate(tweet.media.photos):
            try:
                path = download_image(photo.url, tweet.id, i)
                paths.append(path)
            except Exception as e:
                print(f"⚠️  Could not download image {photo.url}: {e}", file=sys.stderr)
    return paths


# ─── account setup ────────────────────────────────────────────────────────────

async def ensure_account(api: API):
    accounts = await api.pool.get_all()
    active = [a for a in accounts if a.active]
    if active:
        return

    print("\n─── X Account Setup (one-time) ───", file=sys.stderr)
    print("twscrape requires an X account to read threads.", file=sys.stderr)
    print("Credentials are saved locally in .twscrape_accounts.db\n", file=sys.stderr)

    username = input("X Username (without @): ").strip()
    password = input("X Password: ").strip()
    email = input("X Email (used for 2FA verification, press Enter to skip): ").strip()
    email_password = ""
    if email:
        email_password = input("Email password (press Enter to skip): ").strip()

    await api.pool.add_account(
        username=username,
        password=password,
        email=email or f"{username}@placeholder.com",
        email_password=email_password or "placeholder",
    )

    print("\n→ Logging in...", file=sys.stderr)
    await api.pool.login_all()

    accounts = await api.pool.get_all()
    active = [a for a in accounts if a.active]
    if not active:
        print("❌ Login failed. Check your credentials and try again.", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Logged in as @{username}\n", file=sys.stderr)


# ─── main fetch ───────────────────────────────────────────────────────────────

async def fetch(url: str) -> dict:
    tweet_id = extract_tweet_id(url)
    api = API(str(DB_PATH))

    await ensure_account(api)

    print(f"→ Fetching tweet {tweet_id}...", file=sys.stderr)
    tweet = await api.tweet_details(tweet_id)
    if not tweet:
        raise RuntimeError(f"Tweet {tweet_id} not found or not accessible.")

    author_id = tweet.user.id
    main_text = tweet.rawContent
    images = collect_images(tweet)

    thread_tweets = []
    print("→ Fetching thread...", file=sys.stderr)
    async for t in api.tweet_thread(tweet_id):
        if t.id != tweet_id and t.user.id == author_id:
            thread_tweets.append({
                "id": str(t.id),
                "text": t.rawContent,
                "images": collect_images(t),
            })

    print(f"✓ Thread: {len(thread_tweets)} additional tweets, {len(images)} images", file=sys.stderr)

    return {
        "author": tweet.user.username,
        "tweet_text": main_text,
        "thread": thread_tweets,
        "images": images,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 fetch-x.py <x-post-url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    result = asyncio.run(fetch(url))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
