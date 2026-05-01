"""Gmail provider via the Gmail REST API (OAuth2)."""

from __future__ import annotations

import base64
import email
import email.header
import os
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Iterator

from clean_inbox.providers.base import EmailMessage, EmailProvider

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",  # required for permanent deletion
]


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def _normalize_address(raw: str) -> str:
    _, addr = parseaddr(raw)
    return addr.lower().strip()


class GmailProvider(EmailProvider):
    """Gmail provider using the Gmail API."""

    def __init__(
        self,
        credentials_file: str = "gmail_credentials.json",
        token_file: str = "gmail_token.json",
    ) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        # Lazy import so the library is only required when Gmail is used
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)

    def disconnect(self) -> None:
        self._service = None

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_messages(
        self,
        folder: str = "INBOX",
        max_messages: int = 500,
    ) -> Iterator[EmailMessage]:
        assert self._service, "Call connect() first"
        # Map common folder names to Gmail label IDs
        label = folder.upper() if folder.upper() in ("INBOX", "SPAM", "TRASH") else folder
        # Gmail API maxResults is capped at 500 per page; pagination handles the rest
        page_size = min(max_messages, 500)
        results = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=[label], maxResults=page_size)
            .execute()
        )
        messages = results.get("messages", [])
        next_page = results.get("nextPageToken")

        collected = list(messages)
        while next_page and len(collected) < max_messages:
            page = (
                self._service.users()
                .messages()
                .list(userId="me", labelIds=[label], maxResults=page_size, pageToken=next_page)
                .execute()
            )
            collected.extend(page.get("messages", []))
            next_page = page.get("nextPageToken")

        for stub in collected[:max_messages]:
            full = (
                self._service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date",
                                       "List-Unsubscribe", "List-Unsubscribe-Post",
                                       "Precedence", "X-Mailer", "X-Bulk-Mail"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            sender_raw = _decode_header(headers.get("From", ""))
            sender_addr = _normalize_address(sender_raw)
            raw_date = headers.get("Date")
            date: datetime | None = None
            if raw_date:
                from email.utils import parsedate_to_datetime
                try:
                    date = parsedate_to_datetime(raw_date)
                except Exception:
                    pass
            if date and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)

            yield EmailMessage(
                message_id=full.get("id", ""),
                subject=_decode_header(headers.get("Subject", "(no subject)")),
                sender=sender_raw,
                sender_address=sender_addr,
                date=date,
                list_unsubscribe=headers.get("List-Unsubscribe"),
                list_unsubscribe_post=headers.get("List-Unsubscribe-Post"),
                headers=headers,
                raw_id=full["id"],
            )

    def fetch_body(self, message: EmailMessage) -> EmailMessage:
        assert self._service, "Call connect() first"
        full = (
            self._service.users()
            .messages()
            .get(userId="me", id=message.raw_id, format="full")
            .execute()
        )
        html_parts: list[str] = []
        text_parts: list[str] = []
        self._extract_parts(full.get("payload", {}), html_parts, text_parts)
        message.body_html = "\n".join(html_parts) or None
        message.body_text = "\n".join(text_parts) or None
        return message

    def _extract_parts(self, payload: dict, html: list, text: list) -> None:
        mime = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")
        if body_data:
            decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
            if mime == "text/html":
                html.append(decoded)
            elif mime == "text/plain":
                text.append(decoded)
        for part in payload.get("parts", []):
            self._extract_parts(part, html, text)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def move_to_trash(self, message: EmailMessage) -> None:
        assert self._service, "Call connect() first"
        self._service.users().messages().trash(userId="me", id=message.raw_id).execute()

    def delete_permanently(self, message: EmailMessage) -> None:
        assert self._service, "Call connect() first"
        self._service.users().messages().delete(userId="me", id=message.raw_id).execute()

    def send_message(self, to: str, subject: str, body: str) -> None:
        """Send an email via the Gmail API (used for mailto unsubscribe)."""
        assert self._service, "Call connect() first"
        import email.mime.text
        msg = email.mime.text.MIMEText(body)
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
