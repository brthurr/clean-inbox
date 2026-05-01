"""Configuration loading from YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class IMAPConfig:
    host: str
    username: str
    password: str
    port: int = 993
    use_ssl: bool = True
    trash_folder: str = "Trash"


@dataclass
class GmailConfig:
    credentials_file: str = "gmail_credentials.json"
    token_file: str = "gmail_token.json"


@dataclass
class O365Config:
    client_id: str = ""
    tenant_id: str = "common"
    token_cache_file: str = "o365_token_cache.json"


@dataclass
class MailtoConfig:
    smtp_host: str = ""
    smtp_port: int = 587
    from_address: str = ""
    password: str = ""


@dataclass
class AppConfig:
    provider: str = "imap"       # "imap" | "gmail" | "o365"
    folder: str = "INBOX"
    max_messages: int = 500
    junk_threshold: int = 30     # 0-100; messages >= this are flagged
    whitelist: list[str] = field(default_factory=list)
    extra_sender_domains: list[str] = field(default_factory=list)
    extra_subject_patterns: list[str] = field(default_factory=list)
    imap: IMAPConfig | None = None
    gmail: GmailConfig = field(default_factory=GmailConfig)
    o365: O365Config = field(default_factory=O365Config)
    mailto: MailtoConfig = field(default_factory=MailtoConfig)
    unsubscribe_timeout: int = 15


_DEFAULT_CONFIG_PATHS = [
    Path("clean-inbox.yaml"),
    Path("clean-inbox.yml"),
    Path.home() / ".config" / "clean-inbox" / "config.yaml",
]

_DEFAULT_WHITELIST_PATHS = [
    Path("clean-inbox.whitelist"),
    Path.home() / ".config" / "clean-inbox" / "whitelist.txt",
]


def _whitelist_path(config_path: Path | None) -> Path:
    """Return the whitelist file path that sits beside the active config."""
    if config_path:
        return config_path.parent / "clean-inbox.whitelist"
    return _DEFAULT_WHITELIST_PATHS[0]


def load_whitelist(config_path: Path | None = None) -> list[str]:
    """Load persisted whitelist entries (one address per line)."""
    wl_path = _whitelist_path(config_path)
    if not wl_path.exists():
        return []
    return [
        line.strip().lower()
        for line in wl_path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def save_whitelist_entry(address: str, config_path: Path | None = None) -> Path:
    """Append a single address to the whitelist file. Returns the file path."""
    wl_path = _whitelist_path(config_path)
    existing = load_whitelist(config_path)
    if address.lower() not in existing:
        with open(wl_path, "a") as fh:
            fh.write(address.lower() + "\n")
    return wl_path


def _processed_path(config_path: Path | None, command: str = "scan") -> Path:
    filename = f"clean-inbox.{command}-processed"
    if config_path:
        return config_path.parent / filename
    return Path(filename)


def load_processed(config_path: Path | None = None, command: str = "scan") -> set[str]:
    """Load the set of sender addresses previously processed by the given command."""
    path = _processed_path(config_path, command)
    # Migrate legacy shared file on first use
    if not path.exists():
        legacy = config_path.parent / "clean-inbox.processed" if config_path else Path("clean-inbox.processed")
        if legacy.exists():
            return {
                line.strip().lower()
                for line in legacy.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
        return set()
    return {
        line.strip().lower()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def save_processed_entry(address: str, config_path: Path | None = None, command: str = "scan") -> None:
    """Append a sender address to the command-specific processed file."""
    path = _processed_path(config_path, command)
    existing = load_processed(config_path, command)
    if address.lower() not in existing:
        with open(path, "a") as fh:
            fh.write(address.lower() + "\n")


def load_config(path: str | Path | None = None) -> tuple["AppConfig", Path | None]:
    """Load config from YAML. Returns (AppConfig, config_path)."""
    if path:
        config_path = Path(path)
    else:
        config_path = next((p for p in _DEFAULT_CONFIG_PATHS if p.exists()), None)

    raw: dict[str, Any] = {}
    if config_path and config_path.exists():
        with open(config_path) as fh:
            raw = yaml.safe_load(fh) or {}

    # Merge whitelist from YAML and from the persisted whitelist file
    yaml_whitelist: list[str] = raw.get("whitelist") or []
    file_whitelist = load_whitelist(config_path) or []
    merged_whitelist = list({*yaml_whitelist, *file_whitelist})

    cfg = AppConfig(
        provider=raw.get("provider", "imap"),
        folder=raw.get("folder", "INBOX"),
        max_messages=int(raw.get("max_messages", 500)),
        junk_threshold=int(raw.get("junk_threshold", 30)),
        whitelist=merged_whitelist,
        extra_sender_domains=raw.get("extra_sender_domains", []),
        extra_subject_patterns=raw.get("extra_subject_patterns", []),
        unsubscribe_timeout=int(raw.get("unsubscribe_timeout", 15)),
    )

    if "imap" in raw:
        i = raw["imap"]
        cfg.imap = IMAPConfig(
            host=i.get("host", ""),
            username=i.get("username", ""),
            password=i.get("password", os.environ.get("IMAP_PASSWORD", "")),
            port=int(i.get("port", 993)),
            use_ssl=bool(i.get("use_ssl", True)),
            trash_folder=i.get("trash_folder", "Trash"),
        )

    if "gmail" in raw:
        g = raw["gmail"]
        cfg.gmail = GmailConfig(
            credentials_file=g.get("credentials_file", "gmail_credentials.json"),
            token_file=g.get("token_file", "gmail_token.json"),
        )

    if "o365" in raw:
        o = raw["o365"]
        cfg.o365 = O365Config(
            client_id=o.get("client_id", ""),
            tenant_id=o.get("tenant_id", "common"),
            token_cache_file=o.get("token_cache_file", "o365_token_cache.json"),
        )

    if "mailto" in raw:
        m = raw["mailto"]
        cfg.mailto = MailtoConfig(
            smtp_host=m.get("smtp_host", ""),
            smtp_port=int(m.get("smtp_port", 587)),
            from_address=m.get("from_address", ""),
            password=m.get("password", os.environ.get("SMTP_PASSWORD", "")),
        )

    return cfg, config_path
