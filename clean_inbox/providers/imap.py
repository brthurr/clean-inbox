"""IMAP provider — works with any standard IMAP server."""

from __future__ import annotations

import email
import email.header
import imaplib
import re
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Iterator

from clean_inbox.providers.base import EmailMessage, EmailProvider


def _decode_header(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    parts = email.header.decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        return None


def _normalize_address(raw: str) -> str:
    _, addr = parseaddr(raw)
    return addr.lower().strip()


class IMAPProvider(EmailProvider):
    """Generic IMAP provider."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 993,
        use_ssl: bool = True,
        trash_folder: str = "Trash",
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_ssl = use_ssl
        self.trash_folder = trash_folder
        self._conn: imaplib.IMAP4 | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self.use_ssl:
            self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            self._conn = imaplib.IMAP4(self.host, self.port)
        self._conn.login(self.username, self.password)

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_messages(
        self,
        folder: str = "INBOX",
        max_messages: int = 500,
    ) -> Iterator[EmailMessage]:
        assert self._conn, "Call connect() first"
        self._conn.select(f'"{folder}"', readonly=True)
        _, data = self._conn.search(None, "ALL")
        all_ids = data[0].split()
        # Most recent first
        uid_list = all_ids[-max_messages:][::-1]

        for uid in uid_list:
            _, msg_data = self._conn.fetch(uid, "(RFC822.HEADER)")
            if not msg_data or not msg_data[0]:
                continue
            raw_headers = msg_data[0][1]
            msg = email.message_from_bytes(raw_headers)

            sender_raw = _decode_header(msg.get("From", ""))
            sender_addr = _normalize_address(sender_raw)

            yield EmailMessage(
                message_id=_decode_header(msg.get("Message-ID", "")),
                subject=_decode_header(msg.get("Subject", "(no subject)")),
                sender=sender_raw,
                sender_address=sender_addr,
                date=_parse_date(msg.get("Date")),
                list_unsubscribe=msg.get("List-Unsubscribe"),
                list_unsubscribe_post=msg.get("List-Unsubscribe-Post"),
                headers={k: v for k, v in msg.items()},
                raw_id=uid,
            )

    def fetch_body(self, message: EmailMessage) -> EmailMessage:
        assert self._conn, "Call connect() first"
        _, msg_data = self._conn.fetch(message.raw_id, "(RFC822)")
        if not msg_data or not msg_data[0]:
            return message
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        html_parts: list[str] = []
        text_parts: list[str] = []
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            elif ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    text_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        message.body_html = "\n".join(html_parts) or None
        message.body_text = "\n".join(text_parts) or None
        return message

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def move_to_trash(self, message: EmailMessage) -> None:
        assert self._conn, "Call connect() first"
        self._conn.select("INBOX")
        self._conn.copy(message.raw_id, self.trash_folder)
        self._conn.store(message.raw_id, "+FLAGS", "\\Deleted")
        self._conn.expunge()

    def delete_permanently(self, message: EmailMessage) -> None:
        assert self._conn, "Call connect() first"
        self._conn.select("INBOX")
        self._conn.store(message.raw_id, "+FLAGS", "\\Deleted")
        self._conn.expunge()
