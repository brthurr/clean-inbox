"""Heuristic engine for identifying junk/marketing emails."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from clean_inbox.providers.base import EmailMessage

# ---------------------------------------------------------------------------
# Known marketing ESP domains / patterns
# ---------------------------------------------------------------------------

MARKETING_SENDER_DOMAINS: frozenset[str] = frozenset(
    [
        # ESPs (Email Service Providers)
        "mailchimp.com", "list-manage.com", "mc.tv", "mcsv.net",
        "klaviyo.com", "kmail-lists.com",
        "constantcontact.com", "rsgsv.net",
        "sendgrid.net", "sendgrid.com",
        "sailthru.com",
        "marketo.com", "mktomail.com",
        "hubspot.com", "hs-mail.com", "hsmail.net",
        "exacttarget.com", "salesforce.com", "marketingcloud.com",
        "campaignmonitor.com", "cmail19.com", "cmail20.com",
        "drip.com", "getdrip.com",
        "convertkit.com", "ck.page",
        "aweber.com",
        "activecampaign.com",
        "mailjet.com",
        "postmark.com",
        "brevo.com", "sendinblue.com",
        "moosend.com",
        "omnisend.com",
        "iterable.com",
        "responsys.net",
        "silverpop.com",
        "dotdigital.com",
        "yesmail.com",
        "listrak.com",
        "cheetahmail.com",
        "experian.com",
        "bluehornet.com",
        "icontact.com",
        "mailerlite.com",
        "benchmark.email",
        "getresponse.com",
        "zoho.com",
    ]
)

# ---------------------------------------------------------------------------
# Header signals
# ---------------------------------------------------------------------------

BULK_PRECEDENCE_VALUES = frozenset(["bulk", "list", "junk"])

MARKETING_XMAILER_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"mailchimp", r"sendgrid", r"klaviyo", r"constant\s*contact",
        r"hubspot", r"marketo", r"exacttarget", r"salesforce",
        r"campaign\s*monitor", r"drip", r"convertkit", r"aweber",
        r"activecampaign", r"mailjet", r"brevo", r"iterable",
    ]
]

# ---------------------------------------------------------------------------
# Subject signals
# ---------------------------------------------------------------------------

MARKETING_SUBJECT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(unsubscribe|opt[\s-]?out)\b",
        r"\b(newsletter|weekly\s+digest|monthly\s+digest|digest)\b",
        r"\b(sale|discount|offer|deal|promo|coupon|savings?|% off|off%)\b",
        r"\b(free\s+shipping|limited\s+time|flash\s+sale|today\s+only|last\s+chance)\b",
        r"\b(new\s+arrivals?|just\s+dropped|just\s+launched|now\s+available)\b",
        r"\b(don\'t\s+miss|you\'re\s+invited|exclusive\s+(access|offer|deal))\b",
        r"\b(your\s+(weekly|monthly|daily)\s+(update|roundup|report|summary))\b",
        r"\b(reminder:?|action\s+required:?)\b",
        r"[\U0001F600-\U0001FFFF]",  # Emoji in subject
    ]
]

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class JunkReason(str, Enum):
    LIST_UNSUBSCRIBE_HEADER = "has List-Unsubscribe header"
    BULK_PRECEDENCE = "Precedence: bulk/list/junk"
    MARKETING_ESP_DOMAIN = "sent via marketing ESP"
    MARKETING_XMAILER = "sent by marketing tool (X-Mailer)"
    MARKETING_SUBJECT = "subject matches marketing pattern"
    WHITELIST_OVERRIDE = "whitelisted sender"


@dataclass
class AnalysisResult:
    message: EmailMessage
    is_junk: bool
    score: int  # 0-100 confidence
    reasons: list[JunkReason] = field(default_factory=list)
    whitelisted: bool = False

    @property
    def has_unsubscribe(self) -> bool:
        return bool(self.message.list_unsubscribe)


class EmailAnalyzer:
    """Score emails for junk/marketing probability."""

    def __init__(
        self,
        junk_threshold: int = 30,
        whitelist: list[str] | None = None,
        extra_sender_domains: list[str] | None = None,
        extra_subject_patterns: list[str] | None = None,
    ) -> None:
        self.junk_threshold = junk_threshold
        self._whitelist: set[str] = {addr.lower().strip() for addr in (whitelist or [])}
        self._extra_domains: frozenset[str] = frozenset(extra_sender_domains or [])
        self._extra_subject_patterns = [
            re.compile(p, re.IGNORECASE) for p in (extra_subject_patterns or [])
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, message: EmailMessage) -> AnalysisResult:
        reasons: list[JunkReason] = []
        score = 0

        # Whitelist short-circuits everything
        if message.sender_address in self._whitelist:
            return AnalysisResult(
                message=message,
                is_junk=False,
                score=0,
                reasons=[JunkReason.WHITELIST_OVERRIDE],
                whitelisted=True,
            )

        # RFC 2369 List-Unsubscribe header — strongest single signal
        if message.list_unsubscribe:
            score += 60
            reasons.append(JunkReason.LIST_UNSUBSCRIBE_HEADER)

        # Precedence header
        precedence = message.headers.get("Precedence", "").lower().strip()
        if precedence in BULK_PRECEDENCE_VALUES:
            score += 30
            reasons.append(JunkReason.BULK_PRECEDENCE)

        # Sender domain
        sender_domain = message.sender_address.split("@")[-1] if "@" in message.sender_address else ""
        all_marketing_domains = MARKETING_SENDER_DOMAINS | self._extra_domains
        if sender_domain in all_marketing_domains:
            score += 40
            reasons.append(JunkReason.MARKETING_ESP_DOMAIN)

        # X-Mailer / User-Agent
        xmailer = message.headers.get("X-Mailer", "") + " " + message.headers.get("User-Agent", "")
        if any(p.search(xmailer) for p in MARKETING_XMAILER_PATTERNS):
            score += 20
            reasons.append(JunkReason.MARKETING_XMAILER)

        # Subject keywords
        all_subject_patterns = MARKETING_SUBJECT_PATTERNS + self._extra_subject_patterns
        if any(p.search(message.subject) for p in all_subject_patterns):
            score += 15
            reasons.append(JunkReason.MARKETING_SUBJECT)

        score = min(score, 100)
        return AnalysisResult(
            message=message,
            is_junk=score >= self.junk_threshold,
            score=score,
            reasons=reasons,
        )

    def analyze_batch(self, messages: list[EmailMessage]) -> list[AnalysisResult]:
        return [self.analyze(m) for m in messages]
