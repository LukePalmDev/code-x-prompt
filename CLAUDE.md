# CLAUDE.md

## Scopo
Estrai prompt da post X e salvali come file .md bilingue (EN + IT).

## Config
Leggi `config.yaml` → `output_dir`. Espandi `~`.

## Workflow: X Post URL

### 1. Leggi config
```yaml
output_dir: "/percorso/cartella"
```

### 2. Fetch post
```bash
python3 fetch-x.py <url>
```
Output JSON: `author`, `tweet_text`, `thread[]`, `images[]`

Estrai il `tweet_id` dall'URL (i numeri dopo `/status/`).

**Ignora le immagini** — i prompt sono sempre nel testo del thread.

### 3. Topic
Identifica l'argomento in italiano (es. `Dieta`, `Copywriting`, `NotebookLM`). Kebab-case se multi-parola.

### 4. Cartelle — REGOLA ASSOLUTA
**Usa SEMPRE questa struttura, senza eccezioni:**
```
{output_dir}/{Topic}/{tweet_id}/
  Inglese/
  Italiano/
```

Esempio: se tweet_id è `1234567890` e topic è `Dieta`:
```
/output/Dieta/1234567890/Inglese/01-file.md
/output/Dieta/1234567890/Italiano/01-file.md
```

**Non scrivere MAI direttamente in `{Topic}/`. Sempre e solo in `{Topic}/{tweet_id}/`.**
**Non controllare se la cartella esiste. Creala e basta con `mkdir -p`.**

### 5. Estrai prompt
Ogni prompt → file separato. Numerati `01-`, `02-`, ecc. Nomi kebab-case.

### 6. File Inglese
```markdown
# {Title}

{testo originale verbatim}
```

### 7. File Italiano
Traduci titolo e corpo. Mantieni placeholder `[topic]`, `{var}` invariati.
```markdown
# {Titolo IT}

{testo tradotto}
```

### 8. Risposta finale
```
Fatto. {N} prompt in '{Topic}/{tweet_id}'.
```
Nient'altro.

## Regole
- Thread = un unico post, trattalo tutto insieme
- Non troncare mai i prompt
- X blocca accessi non autenticati — non fare retry su URL alternativi
