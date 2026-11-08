# CLAUDE.md

## Scopo

Estrai i prompt da un post o thread X/Twitter e salvali come file Markdown
bilingui: originale inglese e traduzione italiana.

## Quando ricevi un URL X

Non eseguire il workflow a mano: usa la stessa pipeline del bot Telegram.

```bash
~/Developer/code-x-prompt/.venv/bin/python ~/Developer/code-x-prompt/prompt-listener.py --process "<url>"
```

Si lancia dal clone di servizio (vedi "Stato operativo"): coda, `processed.json`,
`.env` e `config.yaml` stanno lì, non nella cartella sorgente.

Rispondi con l'ultima riga stampata (`Fatto. {N} prompt in '{Topic}'.`) oppure
con l'errore. Non creare, spostare o modificare file nell'output a mano. Non
invocare `/x-prompt`: quel comando non esiste.

## Architettura

```text
Link X su Telegram (@xpromptbot)
  -> prompt-listener.py   coda persistente .cache/queue/, notifiche, /stato
  -> pipeline.py          tutti i passi deterministici
     -> fetch-x.py        FxTwitter API (primaria) / twscrape (fallback)
     -> estrazione + traduzione, nessuno strumento, schema JSON fisso
        Claude CLI        predefinito; cartella di lavoro fuori dal progetto
                          (questo file non viene letto). Riserva Sonnet solo qui.
        Ollama            opzionale (`provider: ollama`): POST /api/chat senza
                          tools, stesso schema, nessun fallback su Claude.
     -> scrittura e verifica dei file in {output_dir}/{Topic}/Inglese|Italiano
```

Regole garantite dal codice in `pipeline.py`:

- un file per prompt, `NN-titolo-kebab-case.md`, contenuto `# Titolo` + prompt;
- topic in Pascal-Kebab-Case; una cartella esistente non viene mai riusata per
  un post diverso (si aggiunge l'autore al nome, mai suffissi numerici);
- un post già elaborato (`.cache/processed.json`) non richiama Claude;
- un lavoro interrotto riprende dal punto salvato nel job, senza duplicati;
- i file completi non vengono mai sovrascritti;
- successo solo dopo la verifica dei file in entrambe le lingue (i file iCloud
  solo-cloud sono verificati per dimensione);
- modello Claude: quello di `.claude/settings.json`, poi Sonnet come riserva;
- provider predefinito: Claude. `provider: ollama` è opt-in nel `config.yaml`
  del clone di servizio e non va attivato finché `--smoke-provider` non è
  stato accettato sulla mini. Il tag non è implicito: l'esempio verificato è
  `gemma4:12b-it-q4_K_M`. Ogni richiesta invia `num_ctx` (default 4096, massimo
  8192, mai 262144) e `think: false`. Trader condivide lo stesso Ollama: non
  cambiare la configurazione globale del server e non scaricare altri modelli.

Le istruzioni di estrazione per Claude sono `SYSTEM_PROMPT` in `pipeline.py`.

## Stato operativo

- Sorgente (modifiche e git):
  `/Users/luke/Documents/Documenti Principali/Programmazione/code-x-prompt`,
  clone di `github.com/LukePalmDev/code-x-prompt`; qui non ci sono `.venv`,
  `.env`, `config.yaml`, coda né log
- Servizio (esecuzione): `/Users/luke/Developer/code-x-prompt`, altro clone di
  GitHub fuori da iCloud, con `.venv`, `.env`, `config.yaml`, `.cache/`, `logs/`
- Flusso: modifica nella cartella sorgente, test, commit e push; poi
  `~/Developer/code-x-prompt/.venv/bin/python ~/Developer/code-x-prompt/install-service.py --update`
  (pull, dipendenze, `--check`, riavvio). Non modificare il codice nel clone di
  servizio: bloccherebbe il pull
- Output: `config.yaml` → `output_dir` (cartella iCloud `prompt-x`)
- Ambiente Python: `.venv`
- Servizio macOS: `com.promptx.listener` (LaunchAgent, `KeepAlive`)
- Log permanente: `logs/listener.log` (stdout anche in `/tmp/promptx-listener.log`)
- Coda: `.cache/queue/`; lavori falliti definitivamente: `.cache/failed/`
- Stato e ultimo esito: `.cache/state.json`

Errori temporanei (rete, sovraccarico, limiti d'uso, Mac in stop): 3 tentativi
immediati, poi nuovi tentativi automatici dopo 15 minuti, 1 ora e 3 ore. Ogni
esito, i rinvii e i riavvii imprevisti vengono notificati su Telegram.

Se Telegram resta irraggiungibile per 15 minuti con la rete attiva, il servizio
esce e launchd lo riavvia (`Telegram unusable` nel log).

## Diagnostica

Su Telegram: `/stato`.

Dal clone di servizio (`cd ~/Developer/code-x-prompt`):

```bash
.venv/bin/python prompt-listener.py --check
launchctl print gui/$(id -u)/com.promptx.listener
launchctl kickstart -k gui/$(id -u)/com.promptx.listener
tail -n 100 logs/listener.log
```

Smoke del provider locale, senza Telegram, senza fetch e senza scrivere file
(exit 2 finché il provider resta Claude; non chiama né Claude né Ollama):

```bash
.venv/bin/python prompt-listener.py --smoke-provider
```

Test, dalla cartella sorgente (usano l'ambiente Python del servizio):

```bash
PYTHONPYCACHEPREFIX=/private/tmp/promptx-pycache \
  ~/Developer/code-x-prompt/.venv/bin/python -m unittest discover -s tests
```

Il servizio gira in background con permessi diversi dal Terminale (per esempio
non può scaricare i file iCloud solo-cloud): per riprodurre un problema avvia
lo script come job launchd temporaneo, non da shell.

## Vincoli

- Non installare né avviare il servizio dalla cartella sorgente in `~/Documents`
  e non spostare lì `.venv`, log o stato: macOS espelle i file delle cartelle
  iCloud e il servizio in background non può rileggerli (EDEADLK), quindi il
  bot resta attivo ma smette di rispondere. Solo la cartella di output sta in
  iCloud. `--check` e `/stato` verificano la voce Runtime.
- Non mostrare, modificare o versionare token, cookie o credenziali.
- Non cancellare o sovrascrivere prompt esistenti.
- Non usare l'accesso diretto non autenticato a `x.com`.
- Non dichiarare successo se fetch, scrittura o verifica non sono completati.
- Preserva le modifiche locali non correlate.
