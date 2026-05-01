"""Base abstractions shared by all email providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator


@dataclass
class EmailMessage:
    """Normalized representation of an email message."""

    message_id: str
    subject: str
    sender: str          # "Display Name <address@example.com>"
    sender_address: str  # address@example.com (normalized, lower-cased)
    date: datetime | None
    # RFC 2369 / RFC 8058 unsubscribe headers
    list_unsubscribe: str | None       # raw List-Unsubscribe header value
    list_unsubscribe_post: str | None  # raw List-Unsubscribe-Post header value
    # Additional marketing signals from headers
    headers: dict[str, str] = field(default_factory=dict)
    # Body snippets for link extraction (populated lazily by provider)
    body_html: str | None = None
    body_text: str | None = None
    # Provider-specific opaque handle (used for move/delete operations)
    raw_id: object = None

    @property
    def display_sender(self) -> str:
        return self.sender or self.sender_address


@dataclass
class ScanResult:
    """Summary of a scan over a mailbox."""

    messages: list[EmailMessage] = field(default_factory=list)

    @property
    def senders(self) -> dict[str, list[EmailMessage]]:
        """Messages grouped by normalized sender address."""
        result: dict[str, list[EmailMessage]] = {}
        for msg in self.messages:
            result.setdefault(msg.sender_address, []).append(msg)
        return result


class EmailProvider(ABC):
    """Abstract base class all provider adapters must implement."""

    @abstractmethod
    def connect(self) -> None:
        """Authenticate and open the connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection gracefully."""

    @abstractmethod
    def fetch_messages(
        self,
        folder: str = "INBOX",
        max_messages: int = 500,
    ) -> Iterator[EmailMessage]:
        """Yield EmailMessage objects from the given folder."""

    @abstractmethod
    def fetch_body(self, message: EmailMessage) -> EmailMessage:
        """Populate message.body_html / body_text in-place and return it."""

    @abstractmethod
    def move_to_trash(self, message: EmailMessage) -> None:
        """Move a single message to the provider's trash/deleted folder."""

    @abstractmethod
    def delete_permanently(self, message: EmailMessage) -> None:
        """Permanently delete a single message."""

    def __enter__(self) -> "EmailProvider":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()
