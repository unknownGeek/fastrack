"""
lexor-email
===========

Production-ready Gmail email sender that:

    * Reads a markdown file as the email body (rendered to HTML, with
      a graceful plain-text fallback for clients that cannot display HTML).
    * Attaches one or more local files (PDF CV, etc.).
    * Sends to one or many recipients listed in a CSV file.
    * Supports per-recipient personalization via {placeholders} in
      the markdown body and the subject line.
    * Uses Gmail SMTP over TLS with an "App Password" (the only
      sane way Google still allows third-party SMTP after May 2022).
    * Logs every step to a rotating log file AND the console.
    * Writes three human-friendly report files after each run:

        - reports/sent_successful.txt  (recipients that received the mail)
        - reports/failed_to_send.txt   (recipients that errored, with reason)
        - reports/run_summary.json     (machine-readable run summary)

    * Supports a --dry-run mode so you can preview the message
      without actually sending anything.

Usage
-----

    python send_email.py                       # real send
    python send_email.py --dry-run             # render & validate only
    python send_email.py --config my.yaml      # custom config path
    python send_email.py --to user@example.com # one-off send overriding CSV

The script is intentionally a single file with stdlib-first
dependencies so it can be dropped into any environment with
nothing more than `pip install -r requirements.txt`.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import smtplib
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "Missing dependency 'pyyaml'. Install with: pip install -r requirements.txt"
    ) from exc

try:
    import markdown as md
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "Missing dependency 'markdown'. Install with: pip install -r requirements.txt"
    ) from exc

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional but recommended
    load_dotenv = None  # type: ignore[assignment]

try:
    import certifi  # ships a current Mozilla CA bundle, fixes SSL on macOS python.org builds
except ImportError:
    certifi = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"
LOGS_DIR = SCRIPT_DIR / "logs"
REPORTS_DIR = SCRIPT_DIR / "reports"

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_SSL_PORT = 465
GMAIL_SMTP_TLS_PORT = 587

EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure root logger with console + rotating file handlers."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "lexor_email.log"

    logger = logging.getLogger("lexor_email")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class GmailConfig:
    sender_email: str
    sender_name: str
    app_password: str
    use_ssl: bool = True
    reply_to: Optional[str] = None


@dataclass
class EmailRunConfig:
    subject: str
    body_markdown_path: Path
    attachments: List[Path] = field(default_factory=list)
    recipients_csv: Optional[Path] = None
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    send_delay_seconds: float = 1.5
    max_retries: int = 3
    retry_backoff_seconds: float = 5.0


def _expand(path_value: str) -> Path:
    """Resolve config paths relative to the script directory if not absolute."""
    p = Path(os.path.expanduser(os.path.expandvars(path_value)))
    if not p.is_absolute():
        p = (SCRIPT_DIR / p).resolve()
    return p


def load_config(
    config_path: Path,
    logger: logging.Logger,
) -> Tuple[GmailConfig, EmailRunConfig]:
    """Load YAML config and merge with environment variables.

    Environment variables override the config file for credentials,
    so the password never has to live on disk:

        GMAIL_SENDER_EMAIL
        GMAIL_APP_PASSWORD
    """
    if load_dotenv is not None:
        env_file = SCRIPT_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            logger.debug("Loaded environment from %s", env_file)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            "Copy config.yaml.example to config.yaml and edit it."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: Dict[str, Any] = yaml.safe_load(fh) or {}

    gmail_raw = raw.get("gmail", {}) or {}
    email_raw = raw.get("email", {}) or {}

    sender_email = (
        os.getenv("GMAIL_SENDER_EMAIL")
        or gmail_raw.get("sender_email", "")
    ).strip()
    app_password = (
        os.getenv("GMAIL_APP_PASSWORD")
        or gmail_raw.get("app_password", "")
    ).strip()

    if not sender_email:
        raise ValueError(
            "Gmail sender_email is not configured. "
            "Set it in config.yaml under gmail.sender_email or "
            "export GMAIL_SENDER_EMAIL."
        )
    if not app_password:
        raise ValueError(
            "Gmail app_password is not configured. "
            "Set it in config.yaml under gmail.app_password or "
            "export GMAIL_APP_PASSWORD. See README.md for how to "
            "generate an App Password."
        )
    if not EMAIL_REGEX.match(sender_email):
        raise ValueError(f"sender_email '{sender_email}' is not a valid email")

    gmail_cfg = GmailConfig(
        sender_email=sender_email,
        sender_name=str(gmail_raw.get("sender_name") or sender_email),
        app_password=app_password,
        use_ssl=bool(gmail_raw.get("use_ssl", True)),
        reply_to=gmail_raw.get("reply_to") or None,
    )

    body_path = _expand(str(email_raw.get("body_markdown_path", "email_body.md")))
    if not body_path.exists():
        raise FileNotFoundError(f"Email body markdown not found: {body_path}")

    attachments_raw = email_raw.get("attachments") or []
    if isinstance(attachments_raw, str):
        attachments_raw = [attachments_raw]
    attachments: List[Path] = []
    for item in attachments_raw:
        p = _expand(str(item))
        if not p.exists():
            raise FileNotFoundError(f"Attachment not found: {p}")
        if not p.is_file():
            raise ValueError(f"Attachment is not a file: {p}")
        attachments.append(p)

    recipients_csv = email_raw.get("recipients_csv")
    recipients_path = _expand(str(recipients_csv)) if recipients_csv else None

    run_cfg = EmailRunConfig(
        subject=str(email_raw.get("subject") or "").strip(),
        body_markdown_path=body_path,
        attachments=attachments,
        recipients_csv=recipients_path,
        cc=[c.strip() for c in (email_raw.get("cc") or []) if c and c.strip()],
        bcc=[b.strip() for b in (email_raw.get("bcc") or []) if b and b.strip()],
        send_delay_seconds=float(email_raw.get("send_delay_seconds", 1.5)),
        max_retries=int(email_raw.get("max_retries", 3)),
        retry_backoff_seconds=float(email_raw.get("retry_backoff_seconds", 5.0)),
    )

    if not run_cfg.subject:
        raise ValueError("email.subject is required in config.yaml")

    return gmail_cfg, run_cfg


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #


@dataclass
class Recipient:
    email: str
    fields: Dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.fields.get("name") or self.email.split("@", 1)[0]

    @property
    def display(self) -> str:
        return formataddr((self.name, self.email))


def load_recipients(
    csv_path: Optional[Path],
    inline_recipients: Iterable[str],
    logger: logging.Logger,
) -> List[Recipient]:
    """Load recipients from CSV and/or CLI overrides, deduped, validated."""
    recipients: List[Recipient] = []
    seen: set[str] = set()

    if csv_path:
        if not csv_path.exists():
            raise FileNotFoundError(f"Recipients CSV not found: {csv_path}")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "email" not in [
                (h or "").strip().lower() for h in reader.fieldnames
            ]:
                raise ValueError(
                    f"Recipients CSV must have an 'email' column. "
                    f"Found columns: {reader.fieldnames}"
                )
            normalized_headers = {
                (h or "").strip().lower(): (h or "") for h in reader.fieldnames
            }
            email_key = normalized_headers["email"]
            for line_no, row in enumerate(reader, start=2):
                email_val = (row.get(email_key) or "").strip()
                if not email_val:
                    logger.warning("CSV line %d: empty email, skipping", line_no)
                    continue
                if not EMAIL_REGEX.match(email_val):
                    logger.warning(
                        "CSV line %d: invalid email '%s', skipping",
                        line_no,
                        email_val,
                    )
                    continue
                lower = email_val.lower()
                if lower in seen:
                    logger.debug("CSV line %d: duplicate %s, skipping", line_no, email_val)
                    continue
                seen.add(lower)
                clean_fields = {
                    (k or "").strip().lower(): (v or "").strip()
                    for k, v in row.items()
                    if k
                }
                recipients.append(Recipient(email=email_val, fields=clean_fields))

    for inline in inline_recipients:
        addr = inline.strip()
        if not addr:
            continue
        if not EMAIL_REGEX.match(addr):
            logger.warning("Inline recipient '%s' is not a valid email, skipping", addr)
            continue
        lower = addr.lower()
        if lower in seen:
            continue
        seen.add(lower)
        recipients.append(Recipient(email=addr, fields={"email": addr}))

    return recipients


# --------------------------------------------------------------------------- #
# Body rendering
# --------------------------------------------------------------------------- #


def render_body(
    markdown_text: str,
    context: Dict[str, str],
) -> Tuple[str, str]:
    """Substitute {placeholders} and return (plain_text, html) variants."""

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return "{" + key + "}"

    safe_ctx = _SafeDict({k: (v or "") for k, v in context.items()})
    try:
        rendered_md = markdown_text.format_map(safe_ctx)
    except (IndexError, ValueError):
        rendered_md = markdown_text

    html_body = md.markdown(
        rendered_md,
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html5",
    )
    html_doc = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        "Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#222;"
        "line-height:1.5;}"
        "a{color:#0a66c2;text-decoration:none;}"
        "pre,code{font-family:Menlo,Consolas,monospace;background:#f5f5f5;"
        "padding:2px 4px;border-radius:3px;}"
        "blockquote{border-left:3px solid #ccc;margin:0;padding-left:10px;"
        "color:#555;}"
        "</style></head><body>"
        f"{html_body}"
        "</body></html>"
    )
    return rendered_md, html_doc


# --------------------------------------------------------------------------- #
# Message building
# --------------------------------------------------------------------------- #


def build_message(
    gmail: GmailConfig,
    run_cfg: EmailRunConfig,
    recipient: Recipient,
    plain_body: str,
    html_body: str,
    rendered_subject: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((gmail.sender_name, gmail.sender_email))
    msg["To"] = recipient.display
    if run_cfg.cc:
        msg["Cc"] = ", ".join(run_cfg.cc)
    if gmail.reply_to:
        msg["Reply-To"] = gmail.reply_to
    msg["Subject"] = rendered_subject
    msg["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = make_msgid(domain=gmail.sender_email.split("@", 1)[1])

    msg.set_content(plain_body or "This email requires an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    for attachment in run_cfg.attachments:
        ctype, encoding = mimetypes.guess_type(str(attachment))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with attachment.open("rb") as fh:
            data = fh.read()
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )
    return msg


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #


class GmailSender:
    """Thin wrapper that opens a single SMTP session and reuses it."""

    def __init__(self, gmail: GmailConfig, logger: logging.Logger) -> None:
        self.gmail = gmail
        self.logger = logger
        self.smtp: Optional[smtplib.SMTP] = None

    def __enter__(self) -> "GmailSender":
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _build_ssl_context(self) -> ssl.SSLContext:
        """Prefer certifi's CA bundle; macOS python.org builds otherwise
        ship without trusted roots and fail with CERTIFICATE_VERIFY_FAILED."""
        if certifi is not None:
            try:
                ca_path = certifi.where()
                self.logger.debug("Using certifi CA bundle at %s", ca_path)
                return ssl.create_default_context(cafile=ca_path)
            except (OSError, ssl.SSLError) as exc:
                self.logger.warning(
                    "Failed to load certifi bundle (%s); falling back to system trust store.",
                    exc,
                )
        return ssl.create_default_context()

    def _connect(self) -> None:
        ctx = self._build_ssl_context()
        if self.gmail.use_ssl:
            self.logger.info(
                "Connecting to %s:%d (SMTP_SSL)",
                GMAIL_SMTP_HOST,
                GMAIL_SMTP_SSL_PORT,
            )
            self.smtp = smtplib.SMTP_SSL(
                GMAIL_SMTP_HOST, GMAIL_SMTP_SSL_PORT, context=ctx, timeout=30
            )
        else:
            self.logger.info(
                "Connecting to %s:%d (STARTTLS)",
                GMAIL_SMTP_HOST,
                GMAIL_SMTP_TLS_PORT,
            )
            self.smtp = smtplib.SMTP(
                GMAIL_SMTP_HOST, GMAIL_SMTP_TLS_PORT, timeout=30
            )
            self.smtp.ehlo()
            self.smtp.starttls(context=ctx)
            self.smtp.ehlo()
        self.logger.info("Authenticating as %s", self.gmail.sender_email)
        self.smtp.login(self.gmail.sender_email, self.gmail.app_password)
        self.logger.info("SMTP authenticated successfully")

    def close(self) -> None:
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except (smtplib.SMTPException, OSError):
                try:
                    self.smtp.close()
                except OSError:
                    pass
            finally:
                self.smtp = None

    def send(
        self,
        msg: EmailMessage,
        to_addrs: List[str],
        max_retries: int,
        backoff: float,
    ) -> None:
        assert self.smtp is not None, "SMTP session not open"
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                self.smtp.send_message(msg, to_addrs=to_addrs)
                return
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
                last_exc = exc
                self.logger.warning(
                    "SMTP disconnected on attempt %d/%d: %s. Reconnecting...",
                    attempt,
                    max_retries,
                    exc,
                )
                self.close()
                time.sleep(backoff * attempt)
                self._connect()
            except smtplib.SMTPResponseException as exc:
                last_exc = exc
                if 400 <= exc.smtp_code < 500 and attempt < max_retries:
                    self.logger.warning(
                        "Transient SMTP %d on attempt %d/%d: %s. Retrying...",
                        exc.smtp_code,
                        attempt,
                        max_retries,
                        exc.smtp_error,
                    )
                    time.sleep(backoff * attempt)
                    continue
                raise
            except (socket.timeout, OSError) as exc:
                last_exc = exc
                self.logger.warning(
                    "Network error on attempt %d/%d: %s. Retrying...",
                    attempt,
                    max_retries,
                    exc,
                )
                time.sleep(backoff * attempt)
                self.close()
                self._connect()
        if last_exc is not None:
            raise last_exc


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_reports(
    sent: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    run_meta: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sent_path = REPORTS_DIR / "sent_successful.txt"
    failed_path = REPORTS_DIR / "failed_to_send.txt"
    summary_path = REPORTS_DIR / "run_summary.json"

    with sent_path.open("w", encoding="utf-8") as fh:
        fh.write(f"# lexor-email - sent successfully\n")
        fh.write(f"# generated: {_ts()}\n")
        fh.write(f"# total: {len(sent)}\n\n")
        for entry in sent:
            fh.write(
                f"{entry['timestamp']}\t{entry['email']}\t"
                f"message_id={entry.get('message_id', '')}\n"
            )

    with failed_path.open("w", encoding="utf-8") as fh:
        fh.write(f"# lexor-email - failed to send\n")
        fh.write(f"# generated: {_ts()}\n")
        fh.write(f"# total: {len(failed)}\n\n")
        for entry in failed:
            fh.write(
                f"{entry['timestamp']}\t{entry['email']}\t"
                f"reason={entry['reason']}\n"
            )
        if skipped:
            fh.write("\n# skipped (validation / dedupe)\n")
            for entry in skipped:
                fh.write(
                    f"{entry['timestamp']}\t{entry['email']}\t"
                    f"reason={entry['reason']}\n"
                )

    summary = {
        "run": run_meta,
        "totals": {
            "sent": len(sent),
            "failed": len(failed),
            "skipped": len(skipped),
        },
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)

    logger.info("Reports written:")
    logger.info("  %s", sent_path)
    logger.info("  %s", failed_path)
    logger.info("  %s", summary_path)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send Gmail emails with markdown bodies and attachments.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH.name})",
    )
    parser.add_argument(
        "--to",
        action="append",
        default=[],
        help="Override recipients with one or more --to addresses (repeatable).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render & validate everything but do not connect to Gmail.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose (DEBUG) logging.",
    )
    return parser.parse_args(argv)


def run(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(verbose=args.verbose)
    logger.info("=" * 70)
    logger.info("lexor-email run started at %s", _ts())
    logger.info("=" * 70)

    sent: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    run_meta: Dict[str, Any] = {
        "started_at": _ts(),
        "dry_run": args.dry_run,
        "config_path": str(args.config),
    }

    try:
        gmail_cfg, run_cfg = load_config(args.config, logger)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Configuration error: %s", exc)
        run_meta["finished_at"] = _ts()
        run_meta["fatal_error"] = str(exc)
        write_reports(sent, failed, skipped, run_meta, logger)
        return 2

    logger.info("Sender: %s <%s>", gmail_cfg.sender_name, gmail_cfg.sender_email)
    logger.info("Subject template: %s", run_cfg.subject)
    logger.info("Body file: %s", run_cfg.body_markdown_path)
    if run_cfg.attachments:
        logger.info("Attachments (%d):", len(run_cfg.attachments))
        for a in run_cfg.attachments:
            size_kb = a.stat().st_size / 1024
            logger.info("  - %s (%.1f KB)", a, size_kb)
    else:
        logger.info("No attachments configured.")

    try:
        recipients = load_recipients(run_cfg.recipients_csv, args.to, logger)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Recipients error: %s", exc)
        run_meta["finished_at"] = _ts()
        run_meta["fatal_error"] = str(exc)
        write_reports(sent, failed, skipped, run_meta, logger)
        return 2

    if not recipients:
        logger.error("No valid recipients found. Aborting.")
        run_meta["finished_at"] = _ts()
        run_meta["fatal_error"] = "no recipients"
        write_reports(sent, failed, skipped, run_meta, logger)
        return 2

    logger.info("Loaded %d recipients", len(recipients))

    body_text = run_cfg.body_markdown_path.read_text(encoding="utf-8")

    if args.dry_run:
        sample = recipients[0]
        ctx = {**sample.fields, "name": sample.name, "email": sample.email}
        plain, html = render_body(body_text, ctx)
        rendered_subject = run_cfg.subject.format_map(
            {**ctx, **{"name": sample.name}}
        )
        logger.info("--- DRY RUN preview for %s ---", sample.display)
        logger.info("Subject: %s", rendered_subject)
        logger.info("Plain body:\n%s", plain)
        logger.debug("HTML body:\n%s", html)
        logger.info("--- end preview ---")
        logger.info("DRY RUN: would send to %d recipients", len(recipients))
        run_meta["finished_at"] = _ts()
        run_meta["recipient_count"] = len(recipients)
        write_reports(sent, failed, skipped, run_meta, logger)
        return 0

    try:
        with GmailSender(gmail_cfg, logger) as sender:
            for idx, rcpt in enumerate(recipients, start=1):
                ctx = {**rcpt.fields, "name": rcpt.name, "email": rcpt.email}
                try:
                    plain, html = render_body(body_text, ctx)
                    rendered_subject = run_cfg.subject.format_map(
                        _SafeFormatDict(ctx)
                    )
                    msg = build_message(
                        gmail_cfg, run_cfg, rcpt, plain, html, rendered_subject
                    )
                    to_addrs = [rcpt.email] + run_cfg.cc + run_cfg.bcc
                    logger.info(
                        "[%d/%d] sending to %s ...",
                        idx,
                        len(recipients),
                        rcpt.display,
                    )
                    sender.send(
                        msg,
                        to_addrs=to_addrs,
                        max_retries=run_cfg.max_retries,
                        backoff=run_cfg.retry_backoff_seconds,
                    )
                    sent.append(
                        {
                            "timestamp": _ts(),
                            "email": rcpt.email,
                            "name": rcpt.name,
                            "message_id": msg["Message-ID"],
                        }
                    )
                    logger.info("[%d/%d] OK -> %s", idx, len(recipients), rcpt.email)
                except smtplib.SMTPRecipientsRefused as exc:
                    reason = f"recipient refused: {exc.recipients}"
                    logger.error("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})
                except smtplib.SMTPSenderRefused as exc:
                    reason = f"sender refused (code={exc.smtp_code}): {exc.smtp_error!r}"
                    logger.error("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})
                    raise
                except smtplib.SMTPAuthenticationError as exc:
                    reason = f"auth error (code={exc.smtp_code}): {exc.smtp_error!r}"
                    logger.error("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})
                    raise
                except smtplib.SMTPDataError as exc:
                    reason = f"data error (code={exc.smtp_code}): {exc.smtp_error!r}"
                    logger.error("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})
                except smtplib.SMTPException as exc:
                    reason = f"smtp error: {exc}"
                    logger.error("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})
                except OSError as exc:
                    reason = f"network error: {exc}"
                    logger.error("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})
                except Exception as exc:  # last-resort safety net
                    reason = f"unexpected error: {exc.__class__.__name__}: {exc}"
                    logger.exception("[%d/%d] FAIL %s -> %s", idx, len(recipients), rcpt.email, reason)
                    failed.append({"timestamp": _ts(), "email": rcpt.email, "reason": reason})

                if idx < len(recipients) and run_cfg.send_delay_seconds > 0:
                    time.sleep(run_cfg.send_delay_seconds)

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "Gmail authentication failed (%s). "
            "Make sure you used a 16-character App Password, NOT your "
            "regular Google account password. See README.md.",
            exc,
        )
        run_meta["fatal_error"] = "auth_failed"
    except smtplib.SMTPSenderRefused as exc:
        logger.error("Gmail refused sender (%s). Aborting batch.", exc)
        run_meta["fatal_error"] = "sender_refused"
    except ssl.SSLCertVerificationError as exc:
        logger.error(
            "SSL certificate verification failed: %s. "
            "Your Python installation has no trusted CA roots. Fix it by either:\n"
            "  1. Running once: /Applications/Python\\ 3.x/Install\\ Certificates.command\n"
            "  2. Or ensuring 'certifi' is installed (pip install -r requirements.txt).",
            exc,
        )
        run_meta["fatal_error"] = "ssl_cert_verify_failed"
    except (OSError, smtplib.SMTPException) as exc:
        logger.exception("Fatal SMTP/network error: %s", exc)
        run_meta["fatal_error"] = str(exc)

    run_meta["finished_at"] = _ts()
    run_meta["recipient_count"] = len(recipients)
    write_reports(sent, failed, skipped, run_meta, logger)

    logger.info("=" * 70)
    logger.info(
        "Done. sent=%d failed=%d skipped=%d",
        len(sent),
        len(failed),
        len(skipped),
    )
    logger.info("=" * 70)

    return 0 if not failed and "fatal_error" not in run_meta else 1


class _SafeFormatDict(dict):
    """dict that returns '{key}' for unknown keys instead of raising."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


if __name__ == "__main__":
    sys.exit(run())
