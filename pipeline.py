"""
pipeline.py — turn an X post into bilingual prompt files.

Python owns every deterministic step: fetching, folder choice, duplicates,
numbering, writing and verification. The model only extracts and translates
the prompts. Claude Code is the default: tools disabled, fixed JSON schema,
no filesystem access. Optional local Ollama uses the same schema and the same
validation, and a failure there never calls Claude. Retrieval stays FxTwitter
with the optional twscrape fallback.
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
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
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
# Local generation only. These bounds are per request; nothing here changes the
# Ollama server, its environment, or which models are installed.
# On the Mac mini M4 with 16GB, gemma4:12b-it-q4_K_M at num_ctx 4096 left about
# 14% RAM free while Trader was already sharing that server and tag. The model
# card context (262144) is refused below.
OLLAMA_DEFAULT_BASE = "http://127.0.0.1:11434"
OLLAMA_NUM_CTX_DEFAULT = 4096
OLLAMA_NUM_CTX_MIN = 2048
OLLAMA_NUM_CTX_MAX = 8192
# Model-card context observed for the local tag. Never sent: on 16GB it would
# exhaust the memory the shared Ollama server still has at num_ctx 4096.
OLLAMA_MODEL_CONTEXT_REFUSAL = 262144
OLLAMA_NUM_PREDICT_DEFAULT = 2048
OLLAMA_NUM_PREDICT_MIN = 256
OLLAMA_NUM_PREDICT_MAX = 3584
OLLAMA_TIMEOUT_DEFAULT = 240
OLLAMA_TIMEOUT_MIN = 30
OLLAMA_TIMEOUT_MAX = 600
OLLAMA_KEEP_ALIVE_DEFAULT = "5m"
OLLAMA_RETRY_DELAYS = (5, 20)
OLLAMA_MAX_RESPONSE_BYTES = 1_000_000
# Over-estimate tokens (3 characters each) so a post that may not fit is
# rejected instead of being truncated inside the context window.
OLLAMA_CHARS_PER_TOKEN = 3
OLLAMA_TEMPLATE_TOKENS = 128
# Source tokens per call. Half of num_predict leaves room for the Italian JSON
# without raising the configured generation cap. Calls stay sequential.
OLLAMA_MAX_SEGMENTS = 24
_OLLAMA_LOCK = threading.Lock()
_MODEL_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_KEEP_ALIVE = re.compile(r"^([1-9]\d*)([smh])$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Fixed innocuous text for --smoke-provider. No network and no output files.
SMOKE_THREAD = (
    "Local extraction check.\n\n"
    "Prompt: Write a two-sentence thank-you note to [name] for help with {task}."
)
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

# Used only after a full bilingual call hits num_predict. Smaller JSON, same rules.
ENGLISH_SCHEMA = {
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
                },
                "required": ["title_en", "prompt_en"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topic", "prompts"],
    "additionalProperties": False,
}
TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "title_it": {"type": "string"},
        "prompt_it": {"type": "string"},
    },
    "required": ["title_it", "prompt_it"],
    "additionalProperties": False,
}
ENGLISH_SYSTEM = """\
Estrai i prompt dal segmento e restituiscili tramite l'output strutturato.

Regole:
- Un elemento per ogni prompt completo, nell'ordine originale.
- prompt_en: testo originale verbatim e completo. Non riassumere e non troncare.
- title_en: titolo inglese breve.
- Ignora il testo segnato come contesto già trattato.
- topic: nome descrittivo in italiano in Pascal-Kebab-Case, senza suffissi numerici.
- Se non ci sono prompt completi, restituisci prompts vuoto.
"""
TRANSLATION_SYSTEM = """\
Traduci il prompt in italiano tramite l'output strutturato.

- title_it: traduzione del titolo, breve.
- prompt_it: traduzione completa. Non troncare e non aggiungere commenti.
- Lascia invariati placeholder e token come [topic], {variable} e <context>.
"""
SEGMENT_NOTE = (
    "Segmento del post originale, limiti inclusi. Estrai solo i prompt completi "
    "presenti qui; non riassumere e non completare testo assente.\n\n"
)


class PipelineError(Exception):
    """transient=True means retrying later may succeed (network, limits, outages)."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class BudgetStop(PipelineError):
    """Repeating this same request would hit the context or the output cap again."""

    def __init__(self, message: str):
        super().__init__(message, transient=False)


def is_transient_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in TRANSIENT_ERROR_MARKERS)


def read_config_file():
    """Return the config mapping, or None when config.yaml is absent.

    An empty file is an empty mapping. Invalid YAML is a permanent error.
    """
    config_path = PROJECT_DIR / "config.yaml"
    try:
        handle = config_path.open()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PipelineError(f"config.yaml non leggibile: {exc}", transient=True) from exc
    with handle:
        try:
            config = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise PipelineError(f"config.yaml non valido: {exc}", transient=False) from exc
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise PipelineError("config.yaml deve essere un mapping.", transient=False)
    return config


def output_directory() -> Path:
    config = read_config_file()
    if config is None:
        raise PipelineError(
            "config.yaml assente. Copialo da config.yaml.example e imposta output_dir.",
            transient=False,
        )
    raw = config.get("output_dir")
    if not isinstance(raw, str) or not raw.strip():
        raise PipelineError("output_dir mancante in config.yaml.", transient=False)
    directory = Path(raw).expanduser()
    return directory if directory.is_absolute() else PROJECT_DIR / directory


def _bounded_int(value, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise PipelineError(f"{name} deve essere un intero fra {low} e {high}.", transient=False)
    if not low <= value <= high:
        extra = ""
        if name == "ollama.num_ctx":
            extra = f" Il contesto di scheda {OLLAMA_MODEL_CONTEXT_REFUSAL} non viene mai inviato."
        raise PipelineError(
            f"{name}={value} non è accettato (minimo {low}, massimo {high}).{extra}",
            transient=False,
        )
    return value


def _keep_alive(value) -> str:
    if value is None:
        return OLLAMA_KEEP_ALIVE_DEFAULT
    if isinstance(value, bool):
        raise PipelineError("ollama.keep_alive non è un booleano.", transient=False)
    if isinstance(value, int):
        if not 30 <= value <= 1800:
            raise PipelineError(
                "ollama.keep_alive in secondi deve essere fra 30 e 1800. "
                "0 scaricherebbe il modello condiviso con Trader e non è accettato.",
                transient=False,
            )
        return f"{value}s"
    if not isinstance(value, str) or not _KEEP_ALIVE.fullmatch(value.strip()):
        raise PipelineError(
            "ollama.keep_alive deve essere una durata positiva, per esempio 5m. "
            "0 non è accettato: scaricherebbe il modello già usato da Trader.",
            transient=False,
        )
    amount, unit = _KEEP_ALIVE.fullmatch(value.strip()).groups()
    seconds = int(amount) * {"s": 1, "m": 60, "h": 3600}[unit]
    if not 30 <= seconds <= 1800:
        raise PipelineError("ollama.keep_alive deve durare fra 30 secondi e 30 minuti.", transient=False)
    return value.strip()


def _ollama_base_url(value) -> str:
    raw = OLLAMA_DEFAULT_BASE if value is None else value
    if not isinstance(raw, str) or not raw.strip():
        raise PipelineError("ollama.base_url deve essere un URL http locale.", transient=False)
    parsed = urllib.parse.urlparse(raw.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in _LOOPBACK_HOSTS:
        raise PipelineError(
            "ollama.base_url deve essere http su localhost "
            f"(predefinito {OLLAMA_DEFAULT_BASE}). Un host remoto non è un provider locale.",
            transient=False,
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise PipelineError("ollama.base_url non deve avere credenziali, path, query o fragment.", transient=False)
    return raw.strip().rstrip("/")


def _ollama_block(block) -> dict:
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise PipelineError("ollama in config.yaml deve essere un mapping.", transient=False)
    model = block.get("model")
    if not isinstance(model, str) or not _MODEL_TAG.fullmatch(model) or ".." in model:
        raise PipelineError(
            "Con provider ollama serve ollama.model: il tag esatto già installato "
            "(quello di `ollama list`). Nessun tag è predefinito e nessun modello viene scaricato.",
            transient=False,
        )
    think = block.get("think", False)
    if not isinstance(think, bool):
        raise PipelineError(
            "ollama.think deve essere true o false. Se la chiave manca si invia comunque false: "
            "ommettere il campo think farebbe usare il default del modello, e per il tag locale "
            "verificato il thinking è attivo.",
            transient=False,
        )
    temperature = block.get("temperature", 0)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= float(temperature) <= 1:
        raise PipelineError("ollama.temperature deve essere un numero fra 0 e 1.", transient=False)
    num_ctx = _bounded_int(block.get("num_ctx"), "ollama.num_ctx", OLLAMA_NUM_CTX_DEFAULT, OLLAMA_NUM_CTX_MIN, OLLAMA_NUM_CTX_MAX)
    num_predict = _bounded_int(
        block.get("num_predict"), "ollama.num_predict",
        OLLAMA_NUM_PREDICT_DEFAULT, OLLAMA_NUM_PREDICT_MIN, OLLAMA_NUM_PREDICT_MAX,
    )
    if num_predict > num_ctx - 512:
        raise PipelineError(
            f"ollama.num_predict={num_predict} non lascia almeno 512 token di input in num_ctx={num_ctx}.",
            transient=False,
        )
    if num_ctx > OLLAMA_NUM_CTX_DEFAULT:
        log.warning(
            "ollama.num_ctx=%s is above the measured %s on a 16GB Mac mini sharing this model",
            num_ctx, OLLAMA_NUM_CTX_DEFAULT,
        )
    return {
        "provider": "ollama",
        "base_url": _ollama_base_url(block.get("base_url")),
        "model": model,
        "timeout_seconds": _bounded_int(
            block.get("timeout_seconds"), "ollama.timeout_seconds",
            OLLAMA_TIMEOUT_DEFAULT, OLLAMA_TIMEOUT_MIN, OLLAMA_TIMEOUT_MAX,
        ),
        "num_ctx": num_ctx,
        "num_predict": num_predict,
        "temperature": float(temperature),
        "think": think,
        "keep_alive": _keep_alive(block.get("keep_alive")),
    }


def provider_settings() -> dict:
    """Claude unless config.yaml sets provider to ollama. Extra keys are ignored."""
    config = read_config_file() or {}
    raw = config.get("provider", "claude")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = "claude"
    if not isinstance(raw, str):
        raise PipelineError("provider in config.yaml deve essere una stringa.", transient=False)
    provider = raw.strip().lower()
    if provider == "claude":
        return {"provider": "claude"}
    if provider == "ollama":
        return _ollama_block(config.get("ollama"))
    raise PipelineError(
        f"provider '{provider}' non supportato. Usa claude (predefinito) oppure ollama. "
        "Non esiste un fallback silenzioso fra i due.",
        transient=False,
    )


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


def extraction_request(source: str, existing: list, note: str = "") -> str:
    return (
        f"Cartelle esistenti: {', '.join(existing) or 'nessuna'}\n\n"
        f"{note}"
        f"Post e thread X:\n\n{source}"
    )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + OLLAMA_CHARS_PER_TOKEN - 1) // OLLAMA_CHARS_PER_TOKEN)


def _clip(text: str, limit: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def ollama_segment_budget(settings: dict, existing: list) -> int:
    """Tokens of source text one call may carry, at the configured num_ctx and num_predict."""
    overhead = (
        _estimate_tokens(SYSTEM_PROMPT)
        + _estimate_tokens(extraction_request("", existing, SEGMENT_NOTE))
        + OLLAMA_TEMPLATE_TOKENS
    )
    room = settings["num_ctx"] - settings["num_predict"] - overhead
    if room < 128:
        raise PipelineError(
            f"num_ctx={settings['num_ctx']} con num_predict={settings['num_predict']} "
            "non lascia spazio al testo. Nessun troncamento e nessun fallback su Claude.",
            transient=False,
        )
    # Keep the source smaller than the JSON budget so a bilingual echo can finish
    # inside the configured num_predict. This does not raise that cap.
    return min(room, max(128, settings["num_predict"] // 2))


def split_pieces(source: str) -> list:
    """Cover source exactly once, breaking on tweet, paragraph, then line boundaries."""
    if not source:
        return [(0, 0)]
    pieces = []
    last = 0
    for match in re.finditer(r"\n\n---\n\n|\n\n|\n", source):
        end = match.end()
        if end > last:
            pieces.append((last, end))
        last = end
    if last < len(source):
        pieces.append((last, len(source)))
    return pieces or [(0, len(source))]


def plan_segments(source: str, existing: list, settings: dict) -> list:
    """Slices of the original text. Joining them reproduces the source."""
    budget = ollama_segment_budget(settings, existing)
    pieces = split_pieces(source)
    for start, end in pieces:
        if _estimate_tokens(source[start:end]) > budget:
            raise PipelineError(
                "Un blocco senza un confine di riga non entra nel budget "
                f"(num_ctx={settings['num_ctx']}, num_predict={settings['num_predict']}, "
                f"budget {budget} token). Nessun troncamento e nessun fallback su Claude. "
                f"Non alzare il contesto fino a {OLLAMA_MODEL_CONTEXT_REFUSAL}.",
                transient=False,
            )
    segments = []
    buf_start = None
    buf_end = None
    for start, end in pieces:
        if buf_start is None:
            buf_start, buf_end = start, end
            continue
        if _estimate_tokens(source[buf_start:end]) <= budget:
            buf_end = end
            continue
        segments.append((buf_start, buf_end))
        buf_start, buf_end = start, end
    if buf_start is not None:
        segments.append((buf_start, buf_end))
    if len(segments) > OLLAMA_MAX_SEGMENTS:
        raise PipelineError(
            f"Il post richiederebbe più di {OLLAMA_MAX_SEGMENTS} chiamate locali. "
            "Nessun troncamento e nessun fallback su Claude.",
            transient=False,
        )
    if not segments:
        return [(0, 0)]
    if segments[0][0] != 0 or segments[-1][1] != len(source):
        raise PipelineError("Segmenti non allineati al testo originale.", transient=False)
    for left, right in zip(segments, segments[1:]):
        if left[1] != right[0]:
            raise PipelineError("Segmenti non contigui.", transient=False)
    return segments


def _normalize_prompt(text: str) -> str:
    return " ".join(text.split()).lower()


def prefer_complete_prompts(prompts: list) -> list:
    """Drop an exact duplicate or a boundary fragment of a longer verbatim prompt."""
    seen = set()
    unique = []
    for prompt in prompts:
        key = _normalize_prompt(prompt["prompt_en"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(prompt)
    drop = set()
    keys = [_normalize_prompt(prompt["prompt_en"]) for prompt in unique]
    for index, key in enumerate(keys):
        for other in keys:
            if other != key and (other.startswith(key) or other.endswith(key)) and len(other) >= len(key) + 20:
                drop.add(index)
                break
    return [prompt for index, prompt in enumerate(unique) if index not in drop]


def merge_extractions(base: dict, extra: dict) -> dict:
    topic = str(base.get("topic") or "").strip() or str(extra.get("topic") or "").strip()
    return {"topic": topic, "prompts": prefer_complete_prompts(list(base.get("prompts") or []) + list(extra.get("prompts") or []))}


def halve_text(text: str):
    pieces = split_pieces(text)
    if len(pieces) < 2:
        return None
    half = len(text) / 2
    cut = min(pieces[:-1], key=lambda item: abs(item[1] - half))[1]
    if cut <= 0 or cut >= len(text):
        return None
    return text[:cut], text[cut:]


def predict_for_single_prompt(settings: dict, prompt_en: str) -> int:
    """Configured num_predict, or a higher value only when one prompt needs it.

    The ceiling is OLLAMA_NUM_PREDICT_MAX and the room left in num_ctx. Never
    the model-card context.
    """
    needed = _estimate_tokens(prompt_en) + 48
    instruction = "Traduci in italiano il prompt seguente, senza troncare.\n\n"
    incoming = _estimate_tokens(TRANSLATION_SYSTEM) + _estimate_tokens(instruction + prompt_en) + OLLAMA_TEMPLATE_TOKENS
    room = settings["num_ctx"] - incoming - 64
    ceiling = min(OLLAMA_NUM_PREDICT_MAX, room)
    chosen = min(max(settings["num_predict"], needed), ceiling)
    if chosen < needed or chosen < OLLAMA_NUM_PREDICT_MIN:
        raise PipelineError(
            f"Un singolo prompt non entra in num_predict massimo {OLLAMA_NUM_PREDICT_MAX} "
            f"dentro num_ctx={settings['num_ctx']} (stima {needed} token). "
            "Nessun troncamento e nessun fallback su Claude. "
            f"Non usare {OLLAMA_MODEL_CONTEXT_REFUSAL}.",
            transient=False,
        )
    return chosen


def ollama_chat_body(source: str, existing: list, settings: dict, schema=None, system: str = None, num_predict: int = None, note: str = "") -> dict:
    """POST /api/chat body. tools is omitted on purpose; format carries the schema."""
    return {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT if system is None else system},
            {"role": "user", "content": extraction_request(source, existing, note)},
        ],
        "stream": False,
        "think": settings["think"],
        "format": EXTRACTION_SCHEMA if schema is None else schema,
        "keep_alive": settings["keep_alive"],
        "options": {
            "temperature": settings["temperature"],
            "num_ctx": settings["num_ctx"],
            "num_predict": settings["num_predict"] if num_predict is None else num_predict,
        },
    }


def ollama_http(settings: dict, method: str, path: str, payload=None, timeout: int = None) -> dict:
    url = settings["base_url"] + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    limit = settings["timeout_seconds"] if timeout is None else timeout
    try:
        with urllib.request.urlopen(request, timeout=limit) as response:
            raw = response.read(OLLAMA_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _clip(exc.read(2000).decode("utf-8", "replace") or exc.reason)
        lowered = detail.lower()
        transient = exc.code in (408, 429, 500, 502, 503, 504) or is_transient_error(lowered)
        if any(marker in lowered for marker in ("out of memory", "cannot allocate", "model is loading", "server busy", "runner")):
            transient = True
        if exc.code in (400, 404) or any(marker in lowered for marker in ("not found", "invalid format", "unknown field", "does not support")):
            transient = False
        raise PipelineError(
            f"Ollama HTTP {exc.code} su {path}: {detail}. Nessun fallback su Claude.",
            transient=transient,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise PipelineError(
            f"Ollama non ha risposto entro {limit}s su {settings['base_url']}. Nessun fallback su Claude.",
            transient=True,
        ) from exc
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
        if timed_out:
            message = f"Ollama non ha risposto entro {limit}s su {settings['base_url']}."
        else:
            message = f"Ollama non raggiungibile su {settings['base_url']}: {exc.reason}"
        raise PipelineError(f"{message} Nessun fallback su Claude.", transient=True) from exc
    if len(raw) > OLLAMA_MAX_RESPONSE_BYTES:
        raise PipelineError("Risposta Ollama troppo grande.", transient=False)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise PipelineError("Ollama ha restituito un corpo che non è JSON.", transient=True) from exc
    if not isinstance(parsed, dict):
        raise PipelineError("Ollama ha restituito un JSON inatteso.", transient=True)
    return parsed


def parse_structured_text(content: str) -> dict:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("JSON assente.")
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise ValueError("JSON non parsabile.") from exc
    return validate_extraction(parsed)


def _load_model_json(content: str) -> dict:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("JSON assente.")
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise ValueError("JSON non parsabile.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON non è un oggetto.")
    return parsed


def parse_ollama_chat(payload: dict, settings: dict, kind: str = "full", prompt_en: str = "") -> dict:
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("message assente.")
    if message.get("tool_calls"):
        raise ValueError("tool_calls presenti; la richiesta non invia tools.")
    predict = payload.get("num_predict_sent") or settings["num_predict"]
    if payload.get("done_reason") == "length":
        raise BudgetStop(f"generazione interrotta da num_predict={predict}.")
    evaluated = payload.get("prompt_eval_count")
    if isinstance(evaluated, int) and evaluated >= settings["num_ctx"]:
        raise BudgetStop(f"prompt_eval_count={evaluated} ha riempito num_ctx={settings['num_ctx']}.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        if message.get("thinking"):
            raise ValueError("solo thinking e nessun JSON; ollama.think deve restare false.")
        raise ValueError("contenuto vuoto.")
    if kind == "full":
        return parse_structured_text(content)
    parsed = _load_model_json(content)
    if kind == "english":
        prompts = parsed.get("prompts")
        if not isinstance(prompts, list):
            raise ValueError("Output strutturato assente.")
        cleaned = []
        for number, prompt in enumerate(prompts, 1):
            if not isinstance(prompt, dict) or not str(prompt.get("title_en") or "").strip() or not str(prompt.get("prompt_en") or "").strip():
                raise ValueError(f"Prompt {number} incompleto.")
            cleaned.append({"title_en": prompt["title_en"], "prompt_en": prompt["prompt_en"]})
        return {"topic": str(parsed.get("topic") or "").strip(), "prompts": cleaned}
    title = str(parsed.get("title_it") or "").strip()
    translated = str(parsed.get("prompt_it") or "").strip()
    if not title or not translated:
        raise ValueError("Traduzione incompleta.")
    if len(translated) < MIN_TRANSLATION_RATIO * len(prompt_en.strip()):
        raise ValueError("Traduzione troncata.")
    return {"title_it": title, "prompt_it": translated}


def run_ollama(source: str, existing: list, settings: dict, schema=None, system: str = None, num_predict: int = None, note: str = "", kind: str = "full", prompt_en: str = "") -> dict:
    """One local call. The lock keeps this process to a single Ollama request."""
    body = ollama_chat_body(source, existing, settings, schema=schema, system=system, num_predict=num_predict, note=note)
    with _OLLAMA_LOCK:
        payload = ollama_http(settings, "POST", "/api/chat", body)
    payload["num_predict_sent"] = body["options"]["num_predict"]
    try:
        return parse_ollama_chat(payload, settings, kind=kind, prompt_en=prompt_en)
    except BudgetStop:
        raise
    except ValueError as exc:
        raise PipelineError(f"Risposta di Ollama non valida: {exc}", transient=True) from exc


def run_ollama_attempts(source: str, existing: list, settings: dict, **kwargs) -> dict:
    """Retry a transient answer. A budget stop is not sent again."""
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        try:
            return run_ollama(source, existing, settings, **kwargs)
        except BudgetStop:
            raise
        except PipelineError as exc:
            log.warning(
                "Ollama attempt %d/%d (%s, num_ctx=%s) failed: %s",
                attempt, MAX_EXTRACTION_ATTEMPTS, settings["model"], settings["num_ctx"], exc,
            )
            if not exc.transient or attempt == MAX_EXTRACTION_ATTEMPTS:
                if exc.transient and attempt == MAX_EXTRACTION_ATTEMPTS and "non valida" in str(exc):
                    raise PipelineError(str(exc), transient=False) from exc
                raise
            time.sleep(OLLAMA_RETRY_DELAYS[attempt - 1])


def extract_piecewise(text: str, existing: list, settings: dict) -> dict:
    """English list, then one translation per prompt. Used when one block still overflows."""
    try:
        english = run_ollama_attempts(
            text, existing, settings, schema=ENGLISH_SCHEMA, system=ENGLISH_SYSTEM, kind="english", note=SEGMENT_NOTE,
        )
    except BudgetStop as exc:
        raise PipelineError(
            f"{exc} Nessun troncamento e nessun fallback su Claude.",
            transient=False,
        ) from exc
    prompts = []
    for item in english["prompts"]:
        predict = predict_for_single_prompt(settings, item["prompt_en"])
        try:
            translated = run_ollama_attempts(
                item["prompt_en"], [], settings,
                schema=TRANSLATION_SCHEMA, system=TRANSLATION_SYSTEM,
                num_predict=predict, kind="translation", prompt_en=item["prompt_en"],
                note="Traduci in italiano il prompt seguente, senza troncare.\n\n",
            )
        except BudgetStop as exc:
            raise PipelineError(
                f"{exc} Nessun troncamento e nessun fallback su Claude.",
                transient=False,
            ) from exc
        prompts.append({
            "title_en": item["title_en"], "prompt_en": item["prompt_en"],
            "title_it": translated["title_it"], "prompt_it": translated["prompt_it"],
        })
    return {"topic": english.get("topic") or "", "prompts": prompts}


def extract_text(text: str, existing: list, settings: dict, note: str = "") -> dict:
    try:
        return run_ollama_attempts(text, existing, settings, note=note)
    except BudgetStop:
        parts = halve_text(text)
        if parts is None:
            return extract_piecewise(text, existing, settings)
        return merge_extractions(
            extract_text(parts[0], existing, settings, note),
            extract_text(parts[1], existing, settings, note),
        )


def extract_with_ollama(source: str, existing: list, settings: dict) -> dict:
    segments = plan_segments(source, existing, settings)
    log.info(
        "Ollama segments=%d num_ctx=%s num_predict=%s chars=%d",
        len(segments), settings["num_ctx"], settings["num_predict"], len(source),
    )
    merged = {"topic": "", "prompts": []}
    previous = ""
    for index, (start, end) in enumerate(segments, 1):
        text = source[start:end]
        note = ""
        if len(segments) > 1:
            overlap = ""
            if previous:
                candidate = f"Contesto già trattato, non estrarre di nuovo:\n{previous}\n\n"
                headed = f"Segmento {index}/{len(segments)} caratteri [{start}:{end}).\n{candidate}{SEGMENT_NOTE}"
                incoming = _estimate_tokens(SYSTEM_PROMPT) + _estimate_tokens(extraction_request(text, existing, headed)) + OLLAMA_TEMPLATE_TOKENS
                if incoming <= settings["num_ctx"] - settings["num_predict"]:
                    overlap = candidate
            note = f"Segmento {index}/{len(segments)} caratteri [{start}:{end}).\n{overlap}{SEGMENT_NOTE}"
        log.info("Ollama segment %d/%d chars [%d:%d)", index, len(segments), start, end)
        merged = merge_extractions(merged, extract_text(text, existing, settings, note))
        previous = text[-400:] if len(text) > 400 else text
        newline = previous.find("\n")
        if newline != -1:
            previous = previous[newline + 1:]
    if not str(merged["topic"]).strip():
        merged["topic"] = "Prompt"
    extraction = validate_extraction(merged)
    log.info(
        "Extracted %d prompts with ollama model %s (num_ctx=%s, think=%s, segments=%d)",
        len(extraction["prompts"]), settings["model"], settings["num_ctx"], settings["think"], len(segments),
    )
    return extraction


def check_ollama(settings: dict = None) -> str:
    """GET /api/tags only. Does not generate, pull a model, or call Claude."""
    settings = settings or provider_settings()
    if settings.get("provider") != "ollama":
        raise PipelineError("provider non è ollama.", transient=False)
    payload = ollama_http(settings, "GET", "/api/tags", timeout=10)
    names = []
    for model in payload.get("models") or []:
        if not isinstance(model, dict):
            continue
        for key in ("name", "model"):
            value = model.get(key)
            if isinstance(value, str) and value not in names:
                names.append(value)
    tag = settings["model"]
    if tag not in names:
        shown = ", ".join(names[:8]) or "nessuno"
        raise PipelineError(
            f"tag '{tag}' assente in GET /api/tags ({shown}). "
            "Imposta ollama.model con un tag già installato. Nessun modello viene scaricato.",
            transient=False,
        )
    return f"locale {settings['base_url']}, modello {tag}, num_ctx {settings['num_ctx']}, think {str(settings['think']).lower()}"


def smoke_long_source():
    """Synthetic thread long enough to need more than one segment at 4096 and at 8192.

    The planted lines are the only prompts. No personal data and no network.
    """
    planted = [
        "Prompt: Write a two-sentence thank-you note to [name] for help with {task} one.",
        "Prompt: Write a two-sentence packing list for [name] before {trip} two.",
        "Prompt: Write a two-sentence reminder for [name] about {meeting} three.",
        "Prompt: Write a two-sentence summary of {book} for [name] four.",
    ]
    filler = "This sentence is context and is not a prompt. " * 50
    parts = [f"{filler}\n\n{prompt}" for prompt in planted]
    return "\n\n---\n\n".join(parts), planted


def smoke_long_extraction() -> dict:
    """Segmented local extraction of the synthetic thread. No fetch, files, or Telegram."""
    settings = provider_settings()
    if settings["provider"] != "ollama":
        raise PipelineError("provider claude: smoke locale non eseguito.", transient=False)
    source, planted = smoke_long_source()
    segments = plan_segments(source, [], settings)
    if len(segments) < 2:
        raise PipelineError(
            f"Lo smoke lungo non richiede segmenti con num_ctx={settings['num_ctx']}.",
            transient=False,
        )
    with exclusive_lock():
        extraction = extract_with_ollama(source, [], settings)
    missing = [prompt for prompt in planted if not any(prompt in item["prompt_en"] for item in extraction["prompts"])]
    if missing:
        raise PipelineError(f"Smoke lungo incompleto: {len(missing)} prompt non estratti.", transient=False)
    for item in extraction["prompts"]:
        if _normalize_prompt(item["prompt_en"]) not in _normalize_prompt(source):
            raise PipelineError("Smoke lungo: un prompt non è verbatim nella fonte.", transient=False)
    return {
        "segments": len(segments),
        "prompts": len(extraction["prompts"]),
        "topic": extraction["topic"],
        "model": settings["model"],
        "num_ctx": settings["num_ctx"],
        "num_predict": settings["num_predict"],
    }


def smoke_local_extraction() -> dict:
    """One bounded local extraction. No fetch, no files, no Telegram.

    Waits on the pipeline lock so it does not run beside another PromptX job.
    It cannot see Trader's requests; Ollama queues those on the shared server.
    """
    settings = provider_settings()
    if settings["provider"] != "ollama":
        raise PipelineError("provider claude: smoke locale non eseguito.", transient=False)
    with exclusive_lock():
        extraction = extract_with_ollama(SMOKE_THREAD, [], settings)
    if not extraction["prompts"]:
        raise PipelineError("Smoke Ollama senza prompt.", transient=False)
    return extraction


def run_claude(source: str, existing: list, model: str) -> dict:
    """One extraction call; raises PipelineError on any failure."""
    request = extraction_request(source, existing)
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


def extract_with_claude(source: str, existing: list) -> dict:
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


def extract_prompts(source: str, existing: list) -> dict:
    settings = provider_settings()
    if settings["provider"] == "ollama":
        return extract_with_ollama(source, existing, settings)
    return extract_with_claude(source, existing)


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
            if not root.is_dir():
                raise PipelineError(f"Cartella output assente: {root}", transient=False)
            existing = sorted(path.name for path in root.iterdir() if path.is_dir())
        except PipelineError:
            raise
        except OSError as exc:
            raise PipelineError(f"Cartella output non accessibile: {exc}", transient=True) from exc

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
