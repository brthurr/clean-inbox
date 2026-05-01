# clean-inbox

Automatically identify and unsubscribe from junk/marketing emails.

Supports **Gmail**, **Microsoft 365 / Outlook**, and any **IMAP** server.

## Features

- **Multi-provider** — Gmail (OAuth2), O365 (MSAL device-code), generic IMAP
- **Heuristic analyzer** — scores each message using RFC 2369 headers,
  sender domain databases, `Precedence: bulk`, X-Mailer fingerprints, and
  subject-line patterns
- **Automatic unsubscribe** — prefers RFC 8058 one-click POST, falls back to
  HTTP GET and `mailto:` links, with HTML body link extraction as a last resort
- **Dry-run / test mode** — preview everything before any changes are made
- **Interactive sender review** — inspect each identified sender and choose
  which ones to act on
- **Whitelist** — never flag specific trusted senders

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

# Dry run — see what would be flagged without touching anything
clean-inbox scan --dry-run

# Full interactive scan: review each sender, then unsubscribe
clean-inbox scan

# Just list junk senders (no action)
clean-inbox senders

# Unsubscribe from a specific address
clean-inbox unsubscribe-sender newsletters@somecompany.com --dry-run
clean-inbox unsubscribe-sender newsletters@somecompany.com
```

## Commands

### `scan`

```
clean-inbox scan [OPTIONS]
```

Full workflow: fetch messages → analyze → interactive sender review →
unsubscribe → (optional) trash.

| Option | Default | Description |
|---|---|---|
| `--config`, `-c` | auto-detect | Path to YAML config |
| `--dry-run`, `-n` | `false` | Preview only, no changes |
| `--folder`, `-f` | `INBOX` | Mailbox folder to scan |
| `--max`, `-m` | 500 | Max messages to fetch |
| `--threshold`, `-t` | 30 | Junk score threshold (0–100) |
| `--interactive/--no-interactive` | interactive | Review each sender |
| `--unsubscribe/--no-unsubscribe` | unsubscribe | Follow unsubscribe links |
| `--trash/--no-trash` | no-trash | Move flagged messages to trash |

### `senders`

```
clean-inbox senders [OPTIONS]
```

Read-only scan that lists all identified junk senders in a table.

| Option | Default | Description |
|---|---|---|
| `--min-count` | 1 | Only show senders with ≥ N messages |

### `unsubscribe-sender`

```
clean-inbox unsubscribe-sender ADDRESS [OPTIONS]
```

Unsubscribe from a specific known sender without scanning the full inbox.

## Configuration

Copy `clean-inbox.example.yaml` to `clean-inbox.yaml` (or
`~/.config/clean-inbox/config.yaml`) and fill in your credentials.

### Gmail setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable the **Gmail API**
3. Create **OAuth 2.0 credentials** (Desktop app type)
4. Download the credentials JSON
5. Point `gmail.credentials_file` at it — the app will open a browser for
   the first-time login and cache the token

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
| `List-Unsubscribe` header present | +60 |
| `Precedence: bulk/list/junk` | +30 |
| Sender domain is a known marketing ESP | +40 |
| `X-Mailer` matches marketing tool | +20 |
| Subject matches marketing keyword pattern | +15 |

Scores are capped at 100. Messages at or above `junk_threshold` (default 30)
are flagged.

## Privacy & security

- Credentials are stored only in your local config file and token cache files.
- No data is sent to any third party beyond the unsubscribe requests themselves.
- Use `--dry-run` to audit what the tool would do before granting it write access.
