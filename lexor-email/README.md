# lexor-email

A small, production-ready Gmail email sender written in Python.

It sends a markdown-rendered email body with one or more file attachments
(PDF CV, cover letter, etc.) to a list of recipients defined in a CSV
file, with per-recipient placeholder substitution.

## Features

- Gmail SMTP over TLS (SSL on 465 by default; STARTTLS on 587 supported)
- Markdown body rendered to HTML with a plain-text fallback part
- Multiple file attachments (auto-detected MIME types)
- Bulk send from a CSV with `{name}`, `{company}`, `{role}`, ... placeholders
- Subject-line templating using the same placeholders
- Configurable per-message throttle and automatic retries with backoff
- Rotating log file plus colorless console logging
- Three report files written after every run:
  `reports/sent_successful.txt`, `reports/failed_to_send.txt`,
  `reports/run_summary.json`
- `--dry-run` mode that renders and validates everything without sending
- Credentials can come from `config.yaml`, environment variables, or a
  local `.env` file (env wins, so secrets never need to live on disk)

## Quick start (one click)

If you just want to send mail and skip the setup ceremony, use the
included one-click runner. It picks a Python 3, creates a `.venv`,
installs deps, prompts to create `.env` if missing, validates your
config and attachments, and then launches the script:

```bash
cd fastrack/lexor-email
./run.sh --dry-run    # preview the message that would go out
./run.sh              # real send
./run.sh --to a@b.com # ad-hoc one-off recipient
```

On Windows: `run.bat --dry-run`, `run.bat`, etc.

The runner is idempotent — subsequent runs hit the dependency cache
(via a hash of `requirements.txt`) and start instantly.

## 1. Install (manual path)

```bash
cd fastrack/lexor-email
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Generate a Gmail App Password

Google blocks "less secure app" sign-in, so you must use an
**App Password**. This requires 2-Step Verification on the account.

1. Visit <https://myaccount.google.com/security> and turn on
   **2-Step Verification** if it is not already on.
2. Visit <https://myaccount.google.com/apppasswords>.
3. Pick app = **Mail**, device = **Other** ("lexor-email"), then **Generate**.
4. Copy the 16-character password (Google shows it with spaces; remove
   them, or paste as-is - the script trims).

> Workspace / corporate Gmail accounts may have App Passwords disabled.
> In that case ask your admin to allow them, or switch to OAuth2.

## 3. Configure

Two equivalent options - pick one:

**Option A — `.env` file (recommended for local dev):**

```bash
cp .env.example .env
# then edit .env and put in your address + 16-char app password
```

**Option B — directly in `config.yaml`:**

Edit `config.yaml` and fill in `gmail.sender_email` and `gmail.app_password`.

Then customize the rest of `config.yaml`:

- `gmail.sender_name` — what recipients see in the From header
- `email.subject` — supports `{placeholders}` from the CSV
- `email.body_markdown_path` — defaults to `email_body.md`
- `email.attachments` — list of files to attach (drop them in `attachments/`)
- `email.recipients_csv` — CSV with at minimum an `email` column
- `email.send_delay_seconds` — pause between messages (default 1.5s)

## 4. Edit your message

- `email_body.md` — your markdown body. Use `{name}`, `{company}`,
  `{role}`, `{sender_name}`, and any other column header from your CSV
  as a placeholder. Unknown placeholders are left untouched (they will
  not crash the send).
- `recipients.csv` — must have an `email` column. Any other columns
  (`name`, `company`, `role`, `sender_name`, ...) are available as
  per-recipient placeholders in both the body and the subject line.

## 5. Drop your attachments

Place your CV (and anything else) under `attachments/`, then list each
file under `email.attachments` in `config.yaml`. Default config expects
`attachments/CV.pdf`.

## 6. Send

```bash
# preview the message that would be sent to the first recipient,
# without ever connecting to Gmail:
python send_email.py --dry-run

# real send:
python send_email.py

# one-off send overriding the CSV:
python send_email.py --to someone@example.com --to other@example.com

# verbose (DEBUG) logging:
python send_email.py -v
```

## 7. Read the output

Every run writes:

| File | Purpose |
| --- | --- |
| `logs/lexor_email.log` | Full chronological log (rotated, 5 x 2 MB) |
| `reports/sent_successful.txt` | TSV of recipients that received the mail |
| `reports/failed_to_send.txt` | TSV of recipients that failed, with reasons |
| `reports/run_summary.json` | Machine-readable summary of the whole run |

Exit codes:

- `0` — all recipients sent successfully (or dry-run completed)
- `1` — one or more recipients failed, or a fatal SMTP error occurred mid-batch
- `2` — configuration / input error before any send was attempted

## Gmail sending limits

- **Free Gmail account:** ~500 messages per rolling 24h.
- **Google Workspace account:** ~2000 messages per rolling 24h.
- Exceeding these gets the account temporarily blocked. The default
  1.5s delay keeps a single batch well under the per-second limits.
  For larger batches, increase the delay or split across days.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `SMTPAuthenticationError: 535 ... Username and Password not accepted` | Used the regular Google password instead of an App Password, or 2-Step Verification is off |
| `SMTPSenderRefused: ... Daily user sending limit exceeded` | You hit Google's 500/2000 daily cap; wait 24h |
| `SMTPRecipientsRefused` | The recipient address is invalid / mailbox full |
| Connection times out | Corporate firewall blocking outbound 465/587; flip `gmail.use_ssl` to `false` to try STARTTLS on 587 |
| `SSLCertVerificationError: certificate verify failed: unable to get local issuer certificate` | macOS python.org Python ships without trusted CA roots. Either (a) `pip install -r requirements.txt` (the script auto-uses `certifi`) or (b) run once: `/Applications/Python\ 3.x/Install\ Certificates.command` |
| `Missing dependency 'pyyaml'` | Run `pip install -r requirements.txt` inside your venv |

## Project layout

```
lexor-email/
├── send_email.py        # the script (single-file, stdlib-first)
├── run.sh / run.bat     # one-click runner (venv + deps + preflight + send)
├── config.yaml          # user-editable settings
├── email_body.md        # markdown email body
├── recipients.csv       # recipient list (email + placeholders)
├── attachments/         # drop your CV / files here
├── logs/                # auto-created, rotating log files
├── reports/             # auto-created, per-run reports
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
