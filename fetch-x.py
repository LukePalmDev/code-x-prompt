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
import os
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

_SAFARI_ACCOUNT = "safari_auto"


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


# ─── account setup via Safari cookies (no login required) ────────────────────

def _get_safari_cookies() -> dict:
    """Extract X auth cookies from Safari — no browser launch needed."""
    try:
        import browser_cookie3
        jar = browser_cookie3.safari(domain_name="x.com")
        cookies = {c.name: c.value for c in jar}
        if "auth_token" not in cookies or "ct0" not in cookies:
            raise RuntimeError("auth_token or ct0 not found in Safari cookies. Make sure you are logged into x.com in Safari.")
        return cookies
    except ImportError:
        raise RuntimeError("browser-cookie3 not installed. Run: pip3 install browser-cookie3")


async def ensure_account(api: API):
    """Ensure an active account exists, auto-loading cookies from Safari if needed."""
    accounts = await api.pool.get_all()
    active = [a for a in accounts if a.active]
    if active:
        return

    print("→ No active X account — loading cookies from Safari...", file=sys.stderr)
    cookies = _get_safari_cookies()
    cookie_str = json.dumps(cookies)

    # Remove stale entry if present
    existing = [a for a in accounts if a.username == _SAFARI_ACCOUNT]
    if existing:
        await api.pool.delete_accounts(_SAFARI_ACCOUNT)

    await api.pool.add_account(
        username=_SAFARI_ACCOUNT,
        password="n/a",
        email="n/a@n/a.com",
        email_password="n/a",
        cookies=cookie_str,
    )
    print("✓ Safari cookies loaded into twscrape", file=sys.stderr)


async def refresh_cookies_if_needed(api: API):
    """Re-sync Safari cookies so twscrape always has fresh tokens."""
    try:
        cookies = _get_safari_cookies()
        cookie_str = json.dumps(cookies)
        accounts = await api.pool.get_all()
        safari_accounts = [a for a in accounts if a.username == _SAFARI_ACCOUNT]
        if safari_accounts:
            await api.pool.delete_accounts(_SAFARI_ACCOUNT)
        await api.pool.add_account(
            username=_SAFARI_ACCOUNT,
            password="n/a",
            email="n/a@n/a.com",
            email_password="n/a",
            cookies=cookie_str,
        )
    except Exception as e:
        print(f"⚠️  Cookie refresh skipped: {e}", file=sys.stderr)


# ─── main fetch ───────────────────────────────────────────────────────────────

async def fetch(url: str, skip_images: bool = False) -> dict:
    tweet_id = extract_tweet_id(url)
    api = API(str(DB_PATH))

    # Always refresh Safari cookies so we have the latest session tokens
    await refresh_cookies_if_needed(api)
    await ensure_account(api)

    print(f"→ Fetching tweet {tweet_id}...", file=sys.stderr)
    tweet = await api.tweet_details(tweet_id)
    if not tweet:
        raise RuntimeError(f"Tweet {tweet_id} not found or not accessible.")

    author_id = tweet.user.id
    main_text = tweet.rawContent
    images = [] if skip_images else collect_images(tweet)

    thread_tweets = []
    print("→ Fetching thread...", file=sys.stderr)
    async for t in api.tweet_thread(tweet_id):
        if t.id != tweet_id and t.user.id == author_id:
            thread_tweets.append({
                "id": str(t.id),
                "text": t.rawContent,
                "images": [] if skip_images else collect_images(t),
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
    skip_images = os.environ.get("SKIP_IMAGES", "").lower() in ("1", "true", "yes")
    result = asyncio.run(fetch(url, skip_images=skip_images))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
