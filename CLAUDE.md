# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

This project automatically extracts prompts from X (Twitter) posts and organizes them into bilingual Markdown files. Given a post URL, Claude fetches the content, identifies the topic and all prompts in the thread, then saves them in both the original English and Italian translation.

## Configuration

Before saving any files, read `config.yaml` in this directory to determine where to save the output.

`config.yaml` contains:
```yaml
output_dir: "/path/to/your/output/folder"
```

Expand `~` to the home directory if present. All prompt files must be saved under `output_dir`, not in this project directory.

If `config.yaml` is missing, ask the user to create it from `config.yaml.example` before proceeding.

## ⚡ Token Optimization

**Your current usage**: ~10% of a 5-hour session per post (~1,650 tokens)
**After optimization**: ~530 tokens/post (66% reduction)
**Result**: 3x more posts from same token budget

See `OPTIMIZATION.md` and `DEPLOY.md` for details. TL;DR:
```bash
python3 optimize-pipeline.py <url>  # Single call, 66% cheaper
```

---

## Workflow: Processing an X Post URL

When the user provides an X/Twitter URL, follow these steps:

### 1. Read the configuration
Read `config.yaml` and resolve `output_dir` (expand `~` if needed).

### 2. Fetch the post content
Use `/x-prompt <url>` which calls `fetch-x.py`. The script uses `twscrape` to access X without a browser — it stores an auth token in `.twscrape_accounts.db` (SQLite, local to this project).

**First run**: if no accounts are configured, the script prompts interactively for X username + password. Token is then saved permanently and renewed automatically.

**Output**: `fetch-x.py` returns JSON on stdout:
```json
{
  "author": "username",
  "tweet_text": "...",
  "thread": [{"id": "...", "text": "...", "images": [...]}],
  "images": ["/path/to/downloaded.jpg"]
}
```

**Images**: tweets often embed prompts as image carousels. `fetch-x.py` downloads them to `.img_cache/`. Claude reads them via the Read tool (vision) to extract the text.

### 3. Determine the topic
Read the post text and identify the subject matter in Italian (e.g., a post about "diet plans" → `Dieta`, a post about "copywriting prompts" → `Copywriting`). Use Italian nouns for folder names, title-cased, no spaces (use hyphens if needed).

### 4. Create the folder structure under output_dir
```
{output_dir}/
└── {Topic}/          ← Italian topic name, e.g. "Dieta"
    ├── Inglese/      ← original English prompts
    └── Italiano/     ← translated Italian prompts
```

### 5. Extract and split prompts
Identify each distinct prompt in the post. Posts often number them (1., 2., Prompt #1, etc.) or separate them visually. Each prompt becomes its own `.md` file.

File naming: use the prompt's own title/number if present, otherwise generate a short descriptive name. Use kebab-case, e.g. `01-meal-plan-generator.md`.

### 6. Write the English files (`Inglese/`)
Each file structure:
```markdown
# {Prompt Title}

{prompt text, verbatim from the post}
```

### 7. Write the Italian files (`Italiano/`)
Translate both the title and the prompt body into fluent Italian. Preserve any placeholders like `[topic]`, `[your goal]`, `{variable}` without translating them.

File names in Italian (translated): e.g. `01-generatore-piano-pasti.md`.

Each file structure:
```markdown
# {Titolo in Italiano}

{testo del prompt tradotto in italiano}
```

### 8. Confirm to the user
After saving all files, report:
- The `output_dir` used
- The topic folder created
- How many prompts were extracted
- List of files saved in each language subfolder

## Notes

- Posts are typically in English; topic folder names are always in Italian.
- If a post is a thread (multiple tweets), treat the entire thread as one post.
- X blocks all unauthenticated access — always ask for a text paste, never waste retries on alternative fetch URLs.
- Never truncate prompts — save them in full, including any instructions, examples, or context blocks the author included.
- If a prompt has a title/header in the original post, use it. If not, derive a short descriptive title.
