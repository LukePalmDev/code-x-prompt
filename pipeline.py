"""
pipeline.py — turn an X post into bilingual prompt files.

Python owns every deterministic step: fetching, folder choice, duplicates,
numbering, writing and verification. Claude only extracts and translates the
prompts, with tools disabled and a fixed JSON schema, so it cannot touch the
filesystem or claim work it did not do.
"""

import asyncio
import contextlib
import errno
import fcntl
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

import yaml

log = logging.getLogger("promptx")

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / ".cache"
# Tweet ID -> verified confirmation, so a resent post never reaches Claude.
PROCESSED_INDEX = CACHE_DIR / "processed.json"
LOCK_FILE = CACHE_DIR / "pipeline.lock"
# Claude runs outside the project so CLAUDE.md cannot redirect the task.
CLAUDE_WORKDIR = Path(tempfile.gettempdir()) / "promptx-claude"

ENV = {
    # Drop nested-session markers when started from an interactive Claude Code.
    key: value for key, value in os.environ.items()
    if key != "CLAUDECODE" and (not key.startswith("CLAUDE_CODE_") or key == "CLAUDE_CODE_OAUTH_TOKEN")
}
ENV["PATH"] = os.pathsep.join([
    str(PROJECT_DIR / ".venv" / "bin"),
    str(Path.home() / ".local" / "bin"),
    os.environ.get("PATH", ""),
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
])
CLAUDE_CMD = shutil.which("claude", path=ENV["PATH"]) or "claude"

FALLBACK_MODEL = "sonnet"
MAX_EXTRACTION_ATTEMPTS = 3
RETRY_DELAYS = (5, 15)
CLAUDE_TIMEOUT = 600
TRANSIENT_ERROR_MARKERS = (
    "connection closed",
    "connection error",
    "connection lost",
    "connection reset",
    "hit your limit",
    # A retired pinned model is recovered by the Sonnet fallback.
    "issue with the selected model",
    "network error",
    "overloaded",
    "rate limit",
    "request timed out",
    "service unavailable",
    "socket hang up",
    "status code 429",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 529",
    "token has expired",
    "usage limit",
)
# Smallest plausible complete prompt file when iCloud hides its content.
MIN_CLOUD_FILE_BYTES = 64
# A translation much shorter than its original is treated as truncated.
MIN_TRANSLATION_RATIO = 0.4

SYSTEM_PROMPT = """\
Estrai i prompt da un post o thread X e restituiscili tramite l'output strutturato.

Regole:
- Un elemento per ogni prompt, nell'ordine originale. Ignora introduzioni,
  testo promozionale e call to action che non sono prompt.
- prompt_en: il testo originale del prompt, verbatim e completo. Non
  riassumere, non troncare, non aggiungere commenti.
- title_en: titolo inglese breve e descrittivo; title_it: la sua traduzione.
- prompt_it: traduzione italiana completa di prompt_en.
- Mantieni invariati placeholder, variabili e token come [topic], {variable},
  <context> e simili.
- topic: nome descrittivo in italiano in Pascal-Kebab-Case (per esempio
  Ricerca-Lavoro, Prompt-Engineering, ChatGPT-Prompt-Engineering), diverso
  da tutti i nomi elencati in "Cartelle esistenti" e senza suffissi numerici.
- Se il post non contiene prompt, restituisci prompts vuoto.
"""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "prompts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title_en": {"type": "string"},
                    "prompt_en": {"type": "string"},
                    "title_it": {"type": "string"},
                    "prompt_it": {"type": "string"},
                },
                "required": ["title_en", "prompt_en", "title_it", "prompt_it"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topic", "prompts"],
    "additionalProperties": False,
}


class PipelineError(Exception):
    """transient=True means retrying later may succeed (network, limits, outages)."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def is_transient_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in TRANSIENT_ERROR_MARKERS)


def output_directory() -> Path:
    with (PROJECT_DIR / "config.yaml").open() as config_file:
        config = yaml.safe_load(config_file) or {}
    directory = Path(config["output_dir"]).expanduser()
    return directory if directory.is_absolute() else PROJECT_DIR / directory


def configured_model() -> str:
    try:
        settings = json.loads((PROJECT_DIR / ".claude" / "settings.json").read_text())
    except (OSError, ValueError):
        return "haiku"
    return settings.get("model") or "haiku"


def tweet_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url)
    if not match:
        raise PipelineError(f"Link X non valido: {url}")
    return match[1]


def confirmation(topic: str, count: int) -> str:
    return f"Fatto. {count} prompt in '{topic}'."


# ─── verification ─────────────────────────────────────────────────────────────

def has_prompt_body(file: Path) -> bool:
    try:
        content = file.read_text().strip()
    except OSError as exc:
        if exc.errno != errno.EDEADLK:
            raise
        # iCloud evicts old files and refuses to download them for background
        # processes (EDEADLK); the logical size is still available.
        return file.stat().st_size >= MIN_CLOUD_FILE_BYTES
    return content.startswith("# ") and bool(content.partition("\n")[2].strip())


def verify_output(output: str) -> str:
    """Accept success only when the declared bilingual files exist."""
    confirmations = list(re.finditer(r"^Fatto\. ([1-9]\d*) prompt in '([^'\n]+)'\.$", output, re.MULTILINE))
    if len(confirmations) != 1:
        raise ValueError("Conferma non valida: " + (output[:1000] or "risposta vuota"))
    match = confirmations[0]
    count, topic = int(match[1]), match[2]
    root = output_directory().resolve()
    directory = (root / topic).resolve()
    if directory.parent != root:
        raise ValueError("Cartella topic non valida.")
    numbers = []
    for language in ("Inglese", "Italiano"):
        files = sorted((directory / language).glob("*.md"))
        if len(files) != count:
            raise ValueError(f"Output incompleto: attesi {count} file in {topic}/{language}, trovati {len(files)}.")
        prefixes = []
        for file in files:
            prefix = re.match(r"(\d+)-.+\.md$", file.name)
            if not prefix or not has_prompt_body(file):
                raise ValueError(f"File vuoto o non valido: {file.name}")
            prefixes.append(int(prefix[1]))
        if len(set(prefixes)) != count:
            raise ValueError(f"Numerazione duplicata in {topic}/{language}.")
        numbers.append(sorted(prefixes))
    if numbers[0] != numbers[1]:
        raise ValueError("Manca la traduzione di uno o più prompt.")
    return match[0]


def load_processed() -> dict:
    try:
        return json.loads(PROCESSED_INDEX.read_text())
    except (OSError, ValueError):
        return {}


def remember_processed(url: str, verified: str) -> None:
    index = load_processed()
    index[tweet_id(url)] = verified
    try:
        write_json(PROCESSED_INDEX, index)
    except OSError as exc:
        log.warning("Could not update %s: %s", PROCESSED_INDEX.name, exc)


def write_json(path: Path, data) -> None:
    """Atomic write: a crash leaves either the old or the new content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


# ─── fetch ────────────────────────────────────────────────────────────────────

_fetch_module = None


def fetch_thread(url: str) -> dict:
    global _fetch_module
    if _fetch_module is None:
        spec = importlib.util.spec_from_file_location("fetch_x", PROJECT_DIR / "fetch-x.py")
        _fetch_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_fetch_module)
    try:
        return asyncio.run(_fetch_module.fetch(url, skip_images=True))
    except Exception as exc:
        message = f"Recupero del post non riuscito: {exc}"
        # Deleted or private posts will not come back; network failures will.
        raise PipelineError(message, transient="not found" not in message.lower()) from exc


def thread_text(data: dict) -> str:
    parts = [data.get("tweet_text") or ""] + [tweet.get("text") or "" for tweet in data.get("thread") or []]
    return "\n\n---\n\n".join(part.strip() for part in parts if part.strip())


# ─── extraction ───────────────────────────────────────────────────────────────

def validate_extraction(extraction) -> dict:
    if not isinstance(extraction, dict) or not isinstance(extraction.get("prompts"), list):
        raise ValueError("Output strutturato assente.")
    if not str(extraction.get("topic") or "").strip():
        raise ValueError("Topic mancante.")
    for number, prompt in enumerate(extraction["prompts"], 1):
        if not all(str(prompt.get(key) or "").strip() for key in EXTRACTION_SCHEMA["properties"]["prompts"]["items"]["required"]):
            raise ValueError(f"Prompt {number} incompleto.")
        if len(prompt["prompt_it"].strip()) < MIN_TRANSLATION_RATIO * len(prompt["prompt_en"].strip()):
            raise ValueError(f"Traduzione del prompt {number} troncata.")
    return extraction


def run_claude(source: str, existing: list, model: str) -> dict:
    """One extraction call; raises PipelineError on any failure."""
    request = (
        f"Cartelle esistenti: {', '.join(existing) or 'nessuna'}\n\n"
        f"Post e thread X:\n\n{source}"
    )
    command = [
        CLAUDE_CMD, "-p", request,
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT,
        "--output-format", "json",
        "--json-schema", json.dumps(EXTRACTION_SCHEMA),
    ]
    if model != FALLBACK_MODEL:
        # The CLI rejects a fallback equal to the main model.
        command += ["--fallback-model", FALLBACK_MODEL]
    CLAUDE_WORKDIR.mkdir(exist_ok=True)
    try:
        result = subprocess.run(
            command, cwd=CLAUDE_WORKDIR, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT, env=ENV,
        )
    except subprocess.TimeoutExpired:
        raise PipelineError(f"Claude ha superato {CLAUDE_TIMEOUT // 60} minuti.", transient=True)
    except FileNotFoundError:
        raise PipelineError("Claude CLI non trovato. Installa con: npm install -g @anthropic-ai/claude-code")
    try:
        response = json.loads(result.stdout or "{}")
    except ValueError:
        response = None
    if not isinstance(response, dict):
        response = {"is_error": True, "result": result.stdout.strip()}
    if result.returncode or response.get("is_error"):
        message = "\n".join(filter(None, [
            str(response.get("result") or ""), result.stderr.strip(),
        ])) or "Errore sconosciuto di Claude."
        raise PipelineError(message, transient=is_transient_error(message))
    try:
        return validate_extraction(response.get("structured_output"))
    except ValueError as exc:
        # The model answered badly; a retry (with Sonnet) usually fixes it.
        raise PipelineError(f"Risposta di Claude non valida: {exc}", transient=True)


def extract_prompts(source: str, existing: list) -> dict:
    models = [configured_model()] + [FALLBACK_MODEL] * (MAX_EXTRACTION_ATTEMPTS - 1)
    for attempt, model in enumerate(models, 1):
        try:
            extraction = run_claude(source, existing, model)
        except PipelineError as exc:
            log.warning("Extraction attempt %d/%d (%s) failed: %s", attempt, len(models), model, exc)
            if not exc.transient or attempt == len(models):
                raise
            time.sleep(RETRY_DELAYS[attempt - 1])
        else:
            log.info("Extracted %d prompts with %s", len(extraction["prompts"]), model)
            return extraction


def report_non_verbatim(source: str, prompts: list) -> None:
    def normalize(text):
        text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
        return " ".join(text.split()).lower()

    haystack = normalize(source)
    changed = [n for n, p in enumerate(prompts, 1) if normalize(p["prompt_en"]) not in haystack]
    if changed:
        log.warning("Prompts not found verbatim in the post (check them): %s", changed)


# ─── folders and files ────────────────────────────────────────────────────────

def ascii_words(text: str) -> list:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[A-Za-z0-9]+", folded)


def slugify(title: str) -> str:
    slug = "-".join(ascii_words(title)).lower()[:60].rstrip("-")
    return slug or "prompt"


def choose_topic(proposed: str, author: str, post_id: str, existing: list) -> str:
    """Pascal-Kebab-Case name that never collides with an existing folder."""
    base = "-".join(word[0].upper() + word[1:] for word in ascii_words(proposed))[:60].rstrip("-") or "Prompt"
    owner = "".join(word[0].upper() + word[1:] for word in ascii_words(author)) or "Autore"
    taken = {name.lower() for name in existing}  # APFS is case-insensitive
    for candidate in (base, f"{base}-{owner}", f"{base}-{owner}-Post-{post_id}"):
        if candidate.lower() not in taken:
            return candidate
    raise PipelineError(f"Nessun nome libero per la cartella {base}.")


def write_prompts(directory: Path, prompts: list) -> None:
    """Create missing files only; complete files are never overwritten."""
    for language, title_key, text_key in (("Inglese", "title_en", "prompt_en"), ("Italiano", "title_it", "prompt_it")):
        folder = directory / language
        folder.mkdir(parents=True, exist_ok=True)
        for number, prompt in enumerate(prompts, 1):
            existing = sorted(folder.glob(f"{number:02d}-*.md"))
            if any(has_prompt_body(file) for file in existing):
                continue
            # Reuse the name of an incomplete file so numbering stays unique.
            target = existing[0] if existing else folder / f"{number:02d}-{slugify(prompt[title_key])}.md"
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_text(f"# {prompt[title_key].strip()}\n\n{prompt[text_key].strip()}\n")
            os.replace(temporary, target)


@contextlib.contextmanager
def exclusive_lock():
    """Serialize the Telegram service and manual runs on the same output."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def process(job: dict, save=lambda job: None) -> str:
    """Run (or resume) one job and return the verified confirmation.

    Progress is stored in job and persisted with save(), so a restart resumes
    from the last completed step instead of calling Claude again.
    """
    url = job["url"]
    post_id = tweet_id(url)
    with exclusive_lock():
        previous = load_processed().get(post_id)
        if previous and not job.get("topic"):
            try:
                verified = verify_output(previous)
            except (ValueError, OSError) as exc:
                log.warning("Previous output for %s no longer valid: %s", post_id, exc)
            else:
                log.info("Already processed %s: %s", post_id, verified)
                return verified

        try:
            root = output_directory()
            existing = sorted(path.name for path in root.iterdir() if path.is_dir())
        except (OSError, KeyError, yaml.YAMLError) as exc:
            raise PipelineError(f"Cartella output non accessibile: {exc}", transient=isinstance(exc, OSError))

        if "extraction" not in job:
            data = fetch_thread(url)
            source = thread_text(data)
            if not source:
                raise PipelineError("Il post non contiene testo.")
            extraction = extract_prompts(source, existing)
            report_non_verbatim(source, extraction["prompts"])
            job.update(author=data.get("author") or "", extraction=extraction)
            save(job)

        prompts = job["extraction"]["prompts"]
        if not prompts:
            raise PipelineError("Nessun prompt trovato nel post.")

        if not job.get("topic"):
            job["topic"] = choose_topic(job["extraction"]["topic"], job.get("author", ""), post_id, existing)
            save(job)

        try:
            write_prompts(root / job["topic"], prompts)
            verified = verify_output(confirmation(job["topic"], len(prompts)))
        except OSError as exc:
            raise PipelineError(f"Scrittura dei file non riuscita: {exc}", transient=True)
        except ValueError as exc:
            raise PipelineError(f"Verifica dei file non riuscita: {exc}")
        remember_processed(url, verified)
        log.info("Completed %s: %s", post_id, verified)
        return verified
