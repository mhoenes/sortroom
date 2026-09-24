# email-sorter

Sorts a Strato IMAP inbox into folders using **Jev** (TypeSafe's "System One"
decision model) through **OpenRouter** (or Vercel AI Gateway). Jev doesn't generate text – it picks one
of your categories and returns probabilities, so it can never invent a folder.

For every new mail it asks Jev two questions in one request:

| Question | Type | Used for |
|---|---|---|
| Which category? | `choice` | moving the mail into its folder |
| Does it need action from me? | `noul` (yes/no probability) | flagging the mail |

Cost is roughly $0.00002 per mail (input tokens only; output is free).

## Setup

```powershell
cd C:\Projekte\email-sorter
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in IMAP_USER, IMAP_PASSWORD, OPENROUTER_API_KEY
```

## Usage

```powershell
.venv\Scripts\python -m email_sorter --check      # test login + Jev, list target folders
.venv\Scripts\python -m email_sorter              # DRY RUN: classify, write reports\dry-run-*.csv, change nothing
.venv\Scripts\python -m email_sorter --live       # move + flag for real
```

Options: `--limit N` (classify at most N mails), `-v` (log every decision),
`--config other.toml`.

**Recommended rollout:** run dry runs for a few days, open the CSV reports
(they open directly in German Excel), sharpen category descriptions in
`config.toml` where Jev got it wrong, then go live.

Dry runs don't record anything, so each one re-classifies the same mails
(a fraction of a cent) – that's intended, so you can compare after tweaking.

## Sorting older mail (manual backfill)

Normal and scheduled runs only look at the last `lookback_days`. Older mail is
sorted only when you start it yourself – the scheduled task never passes
`--since`:

```powershell
.venv\Scripts\python -m email_sorter --since 2026-01-01 --limit 100   # dry run, report only
.venv\Scripts\python -m email_sorter --since 2026-01-01 --live        # sort for real
```

It works month by month, newest first, in batches of `max_per_run`. Live
batches are applied immediately, so you can stop it (Ctrl+C) and start it
again later – it continues where it left off. While it runs, scheduled runs
are skipped (shared lock). Old offers whose deadline has passed go straight
to `Newsletter/Abgelaufen`.

## Running in Docker next to n8n

The sorter can run as its own container with a small HTTP API that n8n calls.
The container publishes no port; only containers on the n8n Docker network
reach it at `http://email-sorter:8765`.

| Endpoint | What |
|---|---|
| `POST /run` `{"live": true}` | normal run (last `lookback_days`), returns a JSON summary |
| `POST /backfill` `{"since": "2024-01-01", "live": false}` | backfill in the background, returns a job (202) |
| `GET /jobs/{id}` | status and summary of a backfill job |
| `POST /recheck-expiry` `{"live": true}` | find expiry dates of already sorted offers |
| `GET /health` | liveness, no auth |

All endpoints except `/health` need `Authorization: Bearer <API_TOKEN>`.
Runs share one lock: while a backfill runs, `/run` answers 409.

### Docker image

GitHub Actions (`.github/workflows/docker.yml`) runs the tests on every push
and publishes the image to GitHub Container Registry for amd64 and arm64:

- `ghcr.io/<owner>/email-sorter:latest` – latest `main`
- `ghcr.io/<owner>/email-sorter:1.2.3` / `:1.2` – version tags `v1.2.3`
- `ghcr.io/<owner>/email-sorter:sha-abc1234` – every published commit

If the repository is private, the package is private too; log in once on the
Docker host with a personal access token that has `read:packages`:
`echo <token> | docker login ghcr.io -u <github-user> --password-stdin`.

### Setup on the Docker host

1. On the host you only need `docker-compose.yml`, `config.toml`, `.env`
   and `data/state.db` – the code comes with the image. Set
   `EMAIL_SORTER_IMAGE=ghcr.io/<owner>/email-sorter:latest` in `.env`.
2. **Move the state over:** copy `data/state.db` from the old machine into
   `data/` on the host – otherwise the sorter doesn't know what it already
   sorted and expiry dates are lost.
3. Create `.env` (see `.env.example`): IMAP, `OPENROUTER_API_KEY`, a new
   `API_TOKEN` and `N8N_NETWORK` (the Docker network of your n8n container:
   `docker inspect <n8n> --format '{{json .NetworkSettings.Networks}}'`).
4. `mkdir -p data logs reports && sudo chown -R 1000:1000 data logs reports`
   (the container runs as uid 1000).
5. `docker compose pull && docker compose up -d`, then check:
   `docker compose logs -f email-sorter` and
   `docker exec email-sorter python -m email_sorter --check`.

### n8n

Import the workflows from `n8n/` (Workflows → Import from File):

- **`email-sorter-laufend.json`** – every 10 minutes `POST /run`; a failed run
  goes to "Fehlermeldung bauen", where you attach your notification node
  (e-mail, Telegram, ntfy …).
- **`email-sorter-backfill.json`** – manual only: set `since`/`live`/`limit`
  in "Parameter", start it, it polls the job every minute until done.

In both, create one credential of type **Header Auth**: name
`Authorization`, value `Bearer <API_TOKEN>`, and select it in the HTTP nodes.
Then activate the 10-minute workflow.

**Don't run two sorters on the same mailbox.** Once the container is live,
remove the Windows scheduled task (`uninstall-task.ps1`) if you installed it –
each installation has its own `state.db` and they would not know about each
other's work.

## How it decides

1. Looks at mails in `INBOX` from the last `lookback_days` (default 7) that
   aren't in `data/state.db` yet and arrived at least `min_age_hours` ago
   (default 24, by the server's arrival time) – newer mail stays in the
   inbox for a day and is picked up by a later run.
2. Sends sender, recipient, subject, attachment names, whether it's a mailing
   list, and the cleaned body (first 3000 chars, quotes/signatures removed).
3. Confidence ≥ `min_confidence` (0.70) → moved to the category's folder.
   Below that → stays in the inbox.
4. `needs_action` ≥ 0.80, or a category with `flag = true` → flagged (★).
5. Mails are read with `BODY.PEEK` – unread stays unread.

Folders are created automatically on the first live run
(`INBOX/Finanzen` → `INBOX.Finanzen` if Strato uses `.` as separator;
`--check` shows which).

## Expired offers ("abgelaufen" tag)

For categories with `track_expiry = true` (Newsletter), Jev also answers
whether the mail is a time-limited offer and roughly when it ends (same day,
1–2 days, a week, a month). An explicit deadline in the text ("gültig bis
30.09.", "endet am 1. Oktober", "ends October 3rd") takes precedence.

Every live run takes mails whose last valid day has passed, tags them with
the IMAP keyword `abgelaufen` (`expired_keyword`) and moves them to
`INBOX/Newsletter/Abgelaufen` (`expired_folder` in `config.toml`). To clean
up, open that folder, select all, delete. The folder works in every client –
Outlook, for example, does not show IMAP keywords. Mails you deleted or moved
meanwhile are skipped.

Mails sorted before this feature existed can be checked once:

```powershell
.venv\Scripts\python -m email_sorter --recheck-expiry          # find dates only
.venv\Scripts\python -m email_sorter --recheck-expiry --live   # and tag expired ones
```

Remove `expired_folder` from `config.toml` to only tag. In Thunderbird,
create a tag named `abgelaufen` (Settings → General → Tags) to see the tag.

## Categories (`config.toml`)

| Key | Folder | Notes |
|---|---|---|
| finanzen | INBOX/Finanzen | |
| bestellungen | INBOX/Bestellungen | |
| reisen | INBOX/Reisen | |
| vertraege | INBOX/Verträge | |
| newsletter | INBOX/Newsletter | expired offers → Newsletter/Abgelaufen |
| benachrichtigungen | INBOX/Benachrichtigungen | |
| verdacht | INBOX/Verdacht | phishing/scams, never flagged |
| persoenlich | – (inbox) | |
| sicherheit | – (inbox) | always flagged |
| sonstiges | – (inbox) | |

Add, rename or remove categories freely – the `description` is what Jev
reads, so write it like you'd explain the folder to a person.

## Running automatically

After the dry-run phase, register a Windows scheduled task (every 10 minutes
while you're logged in, live mode):

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -IntervalMinutes 10
powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1   # to remove it
```

Overlapping runs are skipped via `data/run.lock`.

## Files

| Path | What |
|---|---|
| `logs/email-sorter.log` | every run (rotating, 5 × 1 MB) |
| `reports/dry-run-*.csv` | dry-run results incl. runner-up category |
| `data/state.db` | SQLite log of every processed mail (category, confidence, cost) |

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

## Caveats

- Jev is in beta. If the API shape changes, only `build_request()` / `parse_response()` in
  `email_sorter/jev.py` need updating.
- Mail content (first 3000 chars) is sent to the gateway (OpenRouter/Vercel) and TypeSafe.
- Switching gateway (OpenRouter ↔ Vercel) only needs `endpoint`, `model`
  and `api_key_env` in `config.toml` – see the commented alternative there.
- Exit codes: `0` ok, `1` some mails failed (retried next run), `2` config or
  API-key/credit problem.
