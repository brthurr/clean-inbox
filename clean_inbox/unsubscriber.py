"""Unsubscribe engine: parse List-Unsubscribe headers and HTML body links."""

from __future__ import annotations

import re
import smtplib
import urllib.parse
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from enum import Enum
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from clean_inbox.providers.base import EmailMessage


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class UnsubscribeMethod(str, Enum):
    ONE_CLICK_POST = "one-click POST (RFC 8058)"
    HTTP_GET = "HTTP GET"
    MAILTO = "mailto"
    BODY_LINK = "link found in email body"
    NONE = "no unsubscribe mechanism found"


@dataclass
class UnsubscribeResult:
    message_id: str
    sender: str
    method: UnsubscribeMethod
    target: str | None   # URL or mailto address
    success: bool
    error: str | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

_ANGLE_RE = re.compile(r"<([^>]+)>")


def _parse_list_unsubscribe(header: str) -> list[str]:
    """Extract all URIs from a List-Unsubscribe header value."""
    return _ANGLE_RE.findall(header)


def has_actionable_unsubscribe(message: "EmailMessage") -> bool:
    """Return True only if the message has at least one parseable unsubscribe URI."""
    uris = _parse_list_unsubscribe(message.list_unsubscribe or "")
    return any(u.startswith("http") or u.startswith("mailto") for u in uris)


# ---------------------------------------------------------------------------
# Body link extraction
# ---------------------------------------------------------------------------

_UNSUBSCRIBE_LINK_RE = re.compile(
    r'href=["\']([^"\']*unsubscri[^"\']*)["\']',
    re.IGNORECASE,
)


def _extract_body_links(body_html: str | None, body_text: str | None) -> list[str]:
    """Find unsubscribe-like links in email body."""
    links: list[str] = []
    if body_html:
        links.extend(_UNSUBSCRIBE_LINK_RE.findall(body_html))
    if body_text:
        # Plain text often has bare URLs on their own line after "unsubscribe"
        for line in body_text.splitlines():
            if "unsubscri" in line.lower():
                urls = re.findall(r"https?://\S+", line)
                links.extend(urls)
    return links


# ---------------------------------------------------------------------------
# Core unsubscriber
# ---------------------------------------------------------------------------


class Unsubscriber:
    """Attempt to unsubscribe from marketing emails."""

    def __init__(
        self,
        timeout: int = 15,
        mailto_smtp_host: str | None = None,
        mailto_smtp_port: int = 587,
        mailto_from: str | None = None,
        mailto_password: str | None = None,
        user_agent: str = "clean-inbox/0.1 (inbox cleaner)",
    ) -> None:
        self.timeout = timeout
        self.mailto_smtp_host = mailto_smtp_host
        self.mailto_smtp_port = mailto_smtp_port
        self.mailto_from = mailto_from
        self.mailto_password = mailto_password
        self.user_agent = user_agent
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def unsubscribe(
        self,
        message: "EmailMessage",
        dry_run: bool = False,
    ) -> UnsubscribeResult:
        """Choose the best unsubscribe method and execute it."""

        uris = _parse_list_unsubscribe(message.list_unsubscribe or "")
        post_enabled = bool(
            message.list_unsubscribe_post
            and "list-unsubscribe=one-click" in message.list_unsubscribe_post.lower()
        )

        http_uris = [u for u in uris if u.startswith("http")]
        mailto_uris = [u for u in uris if u.startswith("mailto")]

        # Priority: RFC 8058 one-click POST > HTTP GET > mailto > body link
        if post_enabled and http_uris:
            return self._do_post(message, http_uris[0], dry_run)
        if http_uris:
            return self._do_get(message, http_uris[0], dry_run)
        if mailto_uris:
            return self._do_mailto(message, mailto_uris[0], dry_run)

        # Fall back to body link (requires body to be pre-fetched)
        body_links = _extract_body_links(message.body_html, message.body_text)
        if body_links:
            return self._do_get(message, body_links[0], dry_run, method=UnsubscribeMethod.BODY_LINK)

        return UnsubscribeResult(
            message_id=message.message_id,
            sender=message.sender_address,
            method=UnsubscribeMethod.NONE,
            target=None,
            success=False,
            error="No unsubscribe mechanism found",
            dry_run=dry_run,
        )

    def unsubscribe_batch(
        self,
        messages: list["EmailMessage"],
        dry_run: bool = False,
    ) -> list[UnsubscribeResult]:
        results = []
        for msg in messages:
            results.append(self.unsubscribe(msg, dry_run=dry_run))
        return results

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    def _do_post(
        self,
        message: "EmailMessage",
        url: str,
        dry_run: bool,
    ) -> UnsubscribeResult:
        if dry_run:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.ONE_CLICK_POST,
                target=url,
                success=True,
                dry_run=True,
            )
        try:
            resp = self._session.post(
                url,
                data="List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
                allow_redirects=True,
            )
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.ONE_CLICK_POST,
                target=url,
                success=resp.ok,
                error=None if resp.ok else f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.ONE_CLICK_POST,
                target=url,
                success=False,
                error=str(exc),
            )

    def _do_get(
        self,
        message: "EmailMessage",
        url: str,
        dry_run: bool,
        method: UnsubscribeMethod = UnsubscribeMethod.HTTP_GET,
    ) -> UnsubscribeResult:
        if dry_run:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=method,
                target=url,
                success=True,
                dry_run=True,
            )
        try:
            resp = self._session.get(url, timeout=self.timeout, allow_redirects=True)
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=method,
                target=url,
                success=resp.ok,
                error=None if resp.ok else f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=method,
                target=url,
                success=False,
                error=str(exc),
            )

    def _do_mailto(
        self,
        message: "EmailMessage",
        uri: str,
        dry_run: bool,
    ) -> UnsubscribeResult:
        parsed = urllib.parse.urlparse(uri)
        to_addr = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        subject = params.get("subject", ["Unsubscribe"])[0]
        body = params.get("body", ["Please unsubscribe me from this list."])[0]

        if dry_run:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.MAILTO,
                target=to_addr,
                success=True,
                dry_run=True,
            )

        if not self.mailto_from or not self.mailto_smtp_host:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.MAILTO,
                target=to_addr,
                success=False,
                error="SMTP not configured; set mailto_smtp_host and mailto_from in config",
            )

        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = self.mailto_from
            msg["To"] = to_addr
            with smtplib.SMTP(self.mailto_smtp_host, self.mailto_smtp_port) as smtp:
                smtp.starttls()
                if self.mailto_password:
                    smtp.login(self.mailto_from, self.mailto_password)
                smtp.sendmail(self.mailto_from, [to_addr], msg.as_string())
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.MAILTO,
                target=to_addr,
                success=True,
            )
        except Exception as exc:
            return UnsubscribeResult(
                message_id=message.message_id,
                sender=message.sender_address,
                method=UnsubscribeMethod.MAILTO,
                target=to_addr,
                success=False,
                error=str(exc),
            )
