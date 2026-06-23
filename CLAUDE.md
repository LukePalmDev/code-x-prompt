# CLAUDE.md

## Scopo
Estrai prompt da post X e salvali come file .md bilingue (EN + IT).

## Config
Leggi `config.yaml` → `output_dir`. Espandi `~`. Crea le cartelle lì, non qui.

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

**Ignora le immagini** — i prompt sono sempre nel testo del thread.

### 3. Topic
Identifica l'argomento in italiano (es. `Dieta`, `Copywriting`, `NotebookLM`). Kebab-case se multi-parola.

### 4. Cartelle
```
{output_dir}/{Topic}/
  Inglese/
  Italiano/
```

**IMPORTANTE**: Se `{output_dir}/{Topic}/` esiste già, crea una sottocartella `{output_dir}/{Topic}/{topic}-{tweet_id}/` per evitare conflitti. Non toccare mai file esistenti.

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
Rispondi con UN SOLO messaggio breve:
```
Fatto. {N} prompt in '{Topic}'.
```
Nient'altro. Nessun elenco file, nessun riepilogo lungo.

## Regole
- Thread = un unico post, trattalo tutto insieme
- Non troncare mai i prompt
- X blocca accessi non autenticati — non fare retry su URL alternativi
