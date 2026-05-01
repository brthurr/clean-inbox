# clean-inbox

Identify, unsubscribe from, and delete junk and marketing emails — interactively,
one sender at a time.

Supports **Gmail**, **Microsoft 365 / Outlook**, and any **IMAP** server.

## Features

- **Multi-provider** — Gmail (OAuth2), O365 (MSAL device-code), generic IMAP
- **Heuristic analyzer** — scores each message using RFC 2369 headers,
  sender domain databases, `Precedence: bulk`, X-Mailer fingerprints, and
  subject-line patterns
- **Automatic unsubscribe** — prefers RFC 8058 one-click POST, falls back to
  HTTP GET, `mailto:`, and HTML body link extraction as a last resort.
  Gmail users: mailto unsubscribes are sent via the Gmail API — no SMTP config required.
- **Two-phase cleanup** — `scan` handles marketing mail (unsubscribe + delete);
  `cleanup` handles transactional leftovers (delete only, no unsubscribe)
- **Interactive sender review** — inspect each sender and choose to act, skip,
  whitelist, or (in `scan`) clean without unsubscribing
- **Dry-run mode** — preview everything before any changes are made
- **Persistent state** — reviewed senders are remembered per-command so you
  only see new senders on subsequent runs

## Installation

```bash
pip install -e .
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv pip install -e .
```

## Quick start

```bash
# Copy the example config
cp clean-inbox.example.yaml clean-inbox.yaml
# Edit it with your provider credentials
$EDITOR clean-inbox.yaml

# 1. Dry run — see what scan would flag without touching anything
clean-inbox scan --dry-run

# 2. Interactive review: unsubscribe from marketing senders, then trash their messages
clean-inbox scan --trash

# 3. Clean up transactional leftovers (notifications, receipts, alerts)
clean-inbox cleanup --trash

# Repeat scan + cleanup until the inbox is empty.
# Only new senders appear each run — already-reviewed ones are skipped.
```

## Commands

### `scan`

```
clean-inbox scan [OPTIONS]
```

Fetch → analyze → interactive review → unsubscribe → (optional) trash or delete.

Only messages scoring at or above `--threshold` are presented. Senders reviewed
in a previous `scan` run are auto-approved and skipped in the review step.

**Interactive choices:**

| Key | Action |
|---|---|
| `y` | Unsubscribe + trash/delete (per flags) |
| `c` | Clean only — trash/delete but skip unsubscribe (useful for transactional false-positives) |
| `n` | Skip this sender for now |
| `w` | Whitelist — never flag again, trash existing messages |
| `q` | Stop reviewing (already-approved senders are still acted on) |

**Options:**

| Option | Default | Description |
|---|---|---|
| `--config`, `-c` | auto-detect | Path to YAML config |
| `--dry-run`, `-n` | off | Preview only, no changes |
| `--folder`, `-f` | `INBOX` | Mailbox folder to scan |
| `--all` | off | Fetch every message, ignoring `--max` |
| `--max`, `-m` | 500 | Max messages to fetch |
| `--threshold`, `-t` | 30 | Junk score threshold (0–100) |
| `--interactive/--no-interactive` | on | Review each sender interactively |
| `--unsubscribe/--no-unsubscribe` | on | Follow unsubscribe links |
| `--trash` | off | Move approved senders' messages to trash |
| `--delete` | off | Permanently delete (cannot be undone) |
| `--reprocess` | off | Ignore the processed list and re-review all senders |
| `--log-file` | `clean-inbox.log` | Path to the run log |

### `cleanup`

```
clean-inbox cleanup [OPTIONS]
```

Delete transactional or notification emails by sender — no unsubscribe step.
Run this after `scan` has handled marketing mail to clear out receipts,
alerts, account notifications, and other non-marketing clutter.

Uses its own processed and whitelist files, independent of `scan`, so senders
reviewed in `scan` still appear here for cleanup review.

**Interactive choices:**

| Key | Action |
|---|---|
| `y` | Trash/delete this sender's messages |
| `n` | Skip (default) |
| `w` | Whitelist — never show in cleanup again |
| `q` | Stop reviewing |

**Options:**

| Option | Default | Description |
|---|---|---|
| `--config`, `-c` | auto-detect | Path to YAML config |
| `--dry-run`, `-n` | off | Preview only, no changes |
| `--folder`, `-f` | `INBOX` | Mailbox folder |
| `--all` | off | Fetch every message, ignoring `--max` |
| `--max`, `-m` | 500 | Max messages to fetch |
| `--trash` | off | Move chosen senders' messages to trash |
| `--delete` | off | Permanently delete (cannot be undone) |
| `--reprocess` | off | Ignore the processed list and re-review all senders |
| `--log-file` | `clean-inbox.log` | Path to the run log |

### `senders`

```
clean-inbox senders [OPTIONS]
```

Read-only scan — list all identified junk senders in a table. No changes made.

| Option | Default | Description |
|---|---|---|
| `--min-count` | 1 | Only show senders with ≥ N messages |
| `--threshold`, `-t` | 30 | Junk score threshold |
| `--folder`, `-f` | `INBOX` | Mailbox folder |
| `--max`, `-m` | 500 | Max messages to fetch |

### `unsubscribe-sender`

```
clean-inbox unsubscribe-sender ADDRESS [OPTIONS]
```

Unsubscribe from a specific known sender without scanning the full inbox.
Useful for one-off unsubscribes or testing.

## Configuration

Copy `clean-inbox.example.yaml` to `clean-inbox.yaml` (or
`~/.config/clean-inbox/config.yaml`) and fill in your credentials.

Config file locations are checked in this order:

1. `--config` flag (if provided)
2. `./clean-inbox.yaml`
3. `./clean-inbox.yml`
4. `~/.config/clean-inbox/config.yaml`

### Gmail setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable the **Gmail API**
3. Create **OAuth 2.0 credentials** (Desktop app type)
4. Download the credentials JSON
5. Point `gmail.credentials_file` at it — the app opens a browser on first run
   and caches the token in `gmail_token.json`

### O365 / Microsoft 365 setup

1. Open [Azure Portal → App registrations](https://portal.azure.com/)
2. Register a new app (public client / mobile and desktop)
3. Add API permission: **Microsoft Graph → Mail.ReadWrite** (delegated)
4. Copy the **Application (client) ID** into `o365.client_id`
5. On first run the app prints a device-code URL — open it, sign in, and the
   token is cached for future runs

### IMAP setup

Set `imap.host`, `imap.username`, and either `imap.password` or the
`IMAP_PASSWORD` environment variable.

## Junk scoring

Each message receives a score 0–100 based on:

| Signal | Points |
|---|---|
| `List-Unsubscribe` header present (RFC 2369) | +60 |
| `Precedence: bulk/list/junk` | +30 |
| Sender domain is a known marketing ESP | +40 |
| `X-Mailer` matches a marketing tool | +20 |
| Subject matches a marketing keyword pattern | +15 |

Scores are capped at 100. Messages at or above `junk_threshold` (default 30)
are flagged. Whitelisted senders are always skipped regardless of score.

## State files

The app stores persistent state in files alongside your config:

| File | Command | Purpose |
|---|---|---|
| `clean-inbox.scan-processed` | `scan` | Senders already reviewed; auto-approved on future runs |
| `clean-inbox.cleanup-processed` | `cleanup` | Senders already reviewed in cleanup |
| `clean-inbox.scan-whitelist` | `scan` | Senders never flagged as junk |
| `clean-inbox.cleanup-whitelist` | `cleanup` | Senders never shown in cleanup |

The whitelist in your YAML config applies to both commands. State files from
`scan` and `cleanup` are independent — a sender reviewed in `scan` still
appears in `cleanup` for message deletion.

Use `--reprocess` on either command to ignore the processed list and re-review
all senders from scratch.

## Privacy & security

- Credentials are stored only in your local config file and token cache files.
- No data is sent to any third party beyond the unsubscribe requests themselves.
- Use `--dry-run` to audit what the tool would do before granting it write access.
