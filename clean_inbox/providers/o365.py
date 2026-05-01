"""O365 / Microsoft 365 provider via Microsoft Graph API (MSAL OAuth2)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Iterator

from clean_inbox.providers.base import EmailMessage, EmailProvider

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/Mail.ReadWrite"]


class O365Provider(EmailProvider):
    """Microsoft 365 / Outlook provider using MSAL device-code flow."""

    def __init__(
        self,
        client_id: str,
        tenant_id: str = "common",
        token_cache_file: str = "o365_token_cache.json",
    ) -> None:
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.token_cache_file = token_cache_file
        self._token: str | None = None
        self._session = None

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def connect(self) -> None:
        import msal
        import requests

        cache = msal.SerializableTokenCache()
        if os.path.exists(self.token_cache_file):
            with open(self.token_cache_file) as fh:
                cache.deserialize(fh.read())

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        app = msal.PublicClientApplication(
            self.client_id, authority=authority, token_cache=cache
        )

        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])

        if not result:
            flow = app.initiate_device_flow(scopes=SCOPES)
            print(flow["message"])  # Instruct user to open browser
            result = app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            raise RuntimeError(f"O365 auth failed: {result.get('error_description')}")

        self._token = result["access_token"]
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        )

        if cache.has_state_changed:
            with open(self.token_cache_file, "w") as fh:
                fh.write(cache.serialize())

    def disconnect(self) -> None:
        if self._session:
            self._session.close()
        self._session = None
        self._token = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **params) -> dict:
        assert self._session
        resp = self._session.get(f"{GRAPH_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        assert self._session
        resp = self._session.post(f"{GRAPH_BASE}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        assert self._session
        resp = self._session.delete(f"{GRAPH_BASE}{path}")
        resp.raise_for_status()

    @staticmethod
    def _normalize_address(raw: str) -> str:
        _, addr = parseaddr(raw)
        return addr.lower().strip()

    @staticmethod
    def _parse_date(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_messages(
        self,
        folder: str = "INBOX",
        max_messages: int = 500,
    ) -> Iterator[EmailMessage]:
        # Graph folder names differ slightly from IMAP names
        folder_map = {"INBOX": "inbox", "SENT": "sentitems", "TRASH": "deleteditems", "JUNK": "junkemail"}
        graph_folder = folder_map.get(folder.upper(), folder)

        select_fields = (
            "id,subject,from,receivedDateTime,"
            "internetMessageHeaders"
        )
        top = min(max_messages, 100)
        path = f"/me/mailFolders/{graph_folder}/messages"
        params = {
            "$select": select_fields,
            "$top": top,
            "$orderby": "receivedDateTime desc",
        }

        collected = 0
        while True:
            page = self._get(path, **params)
            for item in page.get("value", []):
                if collected >= max_messages:
                    return
                headers_list: list[dict] = item.get("internetMessageHeaders") or []
                headers = {h["name"]: h["value"] for h in headers_list}

                from_info = item.get("from", {}).get("emailAddress", {})
                sender_name = from_info.get("name", "")
                sender_addr_raw = from_info.get("address", "")
                sender_display = f"{sender_name} <{sender_addr_raw}>" if sender_name else sender_addr_raw

                yield EmailMessage(
                    message_id=item.get("id", ""),
                    subject=item.get("subject", "(no subject)"),
                    sender=sender_display,
                    sender_address=sender_addr_raw.lower().strip(),
                    date=self._parse_date(item.get("receivedDateTime")),
                    list_unsubscribe=headers.get("List-Unsubscribe"),
                    list_unsubscribe_post=headers.get("List-Unsubscribe-Post"),
                    headers=headers,
                    raw_id=item["id"],
                )
                collected += 1

            next_link = page.get("@odata.nextLink")
            if not next_link or collected >= max_messages:
                break
            # nextLink is a full URL; extract path+params
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(next_link)
            path = parsed.path.replace("/v1.0", "")
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def fetch_body(self, message: EmailMessage) -> EmailMessage:
        item = self._get(
            f"/me/messages/{message.raw_id}",
            **{"$select": "body,uniqueBody"}
        )
        body = item.get("body", {})
        content = body.get("content", "")
        content_type = body.get("contentType", "text")
        if content_type == "html":
            message.body_html = content
        else:
            message.body_text = content
        return message

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def move_to_trash(self, message: EmailMessage) -> None:
        self._post(f"/me/messages/{message.raw_id}/move", {"destinationId": "deleteditems"})

    def delete_permanently(self, message: EmailMessage) -> None:
        self._delete(f"/me/messages/{message.raw_id}")
