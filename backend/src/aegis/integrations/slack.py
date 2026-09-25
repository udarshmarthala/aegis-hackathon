"""Slack notification integration.

Slack is a write-only surface for Aegis: it posts incident updates and approval
requests and never reads a channel back as evidence. ``READ_METHODS`` is
therefore empty and every method is in ``WRITE_METHODS``.

Two rules shape this module.

**A dropped page is an operational failure.** Nothing here swallows a send
failure or returns a fake success. If Slack is unconfigured or unreachable the
caller gets a typed error and can escalate through another channel. A
notification that silently vanished is worse than one that never existed,
because the incident commander believes someone was told.

**Nothing sensitive leaves in a message.** Payloads are scanned for secret-shaped
strings and refused, and raw evidence bodies - anything wrapped as
``UntrustedText`` - are refused outright. A Slack channel is a wider audience
than the console and an attacker-authored log line rendered into a page is both
an exfiltration path and a phishing surface.

A send is not idempotent: retrying produces a second message. Every call uses a
single attempt and the caller owns any decision to try again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Final

import httpx

from aegis.core.config import Settings
from aegis.core.errors import (
    AegisError,
    ConfigError,
    ExternalServiceError,
    ValidationError,
)
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call
from aegis.domain.models import UntrustedText

log = get_logger(__name__)

SLACK_API_URL: Final = "https://slack.com/api"

# Slack's own limits. Exceeding them is a 400, so they are enforced here where
# the error message can say which block was too long.
MAX_BLOCKS: Final = 50
MAX_TEXT_CHARS: Final = 2900

_CHANNEL_RE = re.compile(r"^[#@]?[A-Za-z0-9._-]{1,80}$")
_TS_RE = re.compile(r"^\d{10}\.\d{6}$")

# Deliberately broad. A false positive costs one refused message and a loud log
# line; a false negative posts a credential into a channel that may be shared
# with a vendor.
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"lsv2_(?:pt|sk)_[A-Za-z0-9]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True, slots=True)
class SlackMessageRef:
    """Where a message landed, so a later update can find it again."""

    channel: str
    ts: str
    transport: str  # "bot" | "webhook"
    performed: str


def _check_channel(channel: str) -> str:
    if not _CHANNEL_RE.match(channel):
        raise ValidationError("invalid slack channel", context={"channel": channel[:64]})
    return channel


def _scan(value: Any, path: str) -> None:
    """Walk a payload refusing untrusted bodies and secret-shaped strings."""
    if isinstance(value, UntrustedText):
        raise ValidationError(
            "refusing to post untrusted evidence text to slack",
            context={"field": path},
        )
    if isinstance(value, str):
        if "<untrusted" in value:
            raise ValidationError(
                "refusing to post an untrusted-text envelope to slack",
                context={"field": path},
            )
        for pattern in _SECRET_SHAPES:
            if pattern.search(value):
                raise ValidationError(
                    "refusing to post a secret-shaped string to slack",
                    context={"field": path},
                )
        if len(value) > MAX_TEXT_CHARS:
            raise ValidationError(
                "slack text field is too long",
                context={"field": path, "length": len(value)},
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _scan(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]")


def validate_blocks(blocks_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check a Block Kit payload before it reaches the network."""
    if not isinstance(blocks_payload, list) or not blocks_payload:
        raise ValidationError("slack blocks payload must be a non-empty list")
    if len(blocks_payload) > MAX_BLOCKS:
        raise ValidationError(
            "too many slack blocks",
            context={"blocks": len(blocks_payload), "max": MAX_BLOCKS},
        )
    for index, block in enumerate(blocks_payload):
        if not isinstance(block, dict) or "type" not in block:
            raise ValidationError(
                "each slack block must be an object with a type", context={"index": index}
            )
    _scan(blocks_payload, "blocks")
    return blocks_payload


class SlackClient:
    """Notification transport. Every public method is a write.

    Credentials come from the validated ``Settings`` object - never from
    ``os.environ`` read here - so the whole of Aegis's configuration is visible
    in one typed place. ``pydantic-settings`` still populates those fields from
    ``SLACK_BOT_TOKEN`` / ``SLACK_WEBHOOK_URL``, so an operator's environment
    keeps working while the process has exactly one source of configuration.
    Explicit constructor arguments win over both, which is what lets a test
    build a configured client without mutating the process environment.
    """

    READ_METHODS: ClassVar[frozenset[str]] = frozenset()
    WRITE_METHODS: ClassVar[frozenset[str]] = frozenset(
        {"post_incident_update", "post_approval_request", "update_message"}
    )

    __slots__ = ("_settings", "_bulkhead", "_client", "_webhook_url", "_bot_token")

    def __init__(
        self,
        settings: Settings,
        *,
        webhook_url: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._settings = settings
        self._bulkhead = Bulkhead("slack", limit=4)
        self._client: httpx.AsyncClient | None = None
        # Explicit arguments win, then configuration. Nothing is read from the
        # process environment here: configuration has exactly one source.
        self._webhook_url = (
            webhook_url
            if webhook_url is not None
            else settings.slack_webhook_url.get_secret_value()
        )
        self._bot_token = (
            bot_token
            if bot_token is not None
            else settings.slack_bot_token.get_secret_value()
        )

    @property
    def configured(self) -> bool:
        return bool(self._bot_token or self._webhook_url)

    @property
    def can_update(self) -> bool:
        """Editing a posted message needs the Web API; a webhook cannot do it."""
        return bool(self._bot_token)

    @property
    def unconfigured_reason(self) -> str:
        if self.configured:
            return ""
        return "neither slack_bot_token nor slack_webhook_url is configured"

    def _require_configured(self, operation: str) -> None:
        if not self.configured:
            raise ConfigError(
                f"slack not configured: {self.unconfigured_reason}",
                context={"dependency": "slack", "operation": operation},
            )

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self._bot_token:
                headers["Authorization"] = f"Bearer {self._bot_token}"
            self._client = httpx.AsyncClient(
                timeout=self._settings.source_timeout_s,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                headers=headers,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _send(
        self, url: str, body: dict[str, Any], *, operation: str
    ) -> dict[str, Any]:
        """One guarded, single-attempt POST.

        ``attempts=1`` is not a timid default: a retried post is a duplicate
        page, and Slack has no idempotency key to deduplicate with.
        """

        async def _call() -> httpx.Response:
            client = await self._http()
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp

        try:
            resp = await guarded_call(
                _call,
                dependency="slack",
                timeout_s=self._settings.source_timeout_s,
                attempts=1,
                bulkhead=self._bulkhead,
            )
        except AegisError:
            raise
        except Exception as exc:
            raise ExternalServiceError(
                f"slack notification failed: {type(exc).__name__}",
                context={"dependency": "slack", "operation": operation},
                retryable=False,
            ) from exc

        # A webhook answers "ok" in the body; the Web API answers JSON with an
        # ``ok`` flag and a 200 even when it refused the call.
        text = resp.text.strip()
        if text == "ok":
            return {"ok": True}
        try:
            payload: dict[str, Any] = dict(resp.json())
        except (ValueError, TypeError) as exc:
            raise ExternalServiceError(
                "slack returned an unreadable response",
                context={"dependency": "slack", "operation": operation},
                retryable=False,
            ) from exc
        if not payload.get("ok", False):
            raise ExternalServiceError(
                "slack rejected the message",
                context={
                    "dependency": "slack",
                    "operation": operation,
                    "error": str(payload.get("error", ""))[:120],
                },
                retryable=False,
            )
        return payload

    async def post_incident_update(
        self, channel: str, blocks_payload: list[dict[str, Any]], *, fallback_text: str = ""
    ) -> SlackMessageRef:
        """WRITE. Post an incident update. Raises rather than dropping it."""
        self._require_configured("post_incident_update")
        channel = _check_channel(channel)
        blocks = validate_blocks(blocks_payload)
        _scan(fallback_text, "fallback_text")
        return await self._post(channel, blocks, fallback_text, operation="post_incident_update")

    async def post_approval_request(
        self,
        channel: str,
        *,
        incident_id: str,
        action_type: str,
        summary: str,
        approval_url: str,
        expires_at: datetime,
    ) -> SlackMessageRef:
        """WRITE. Ask a human to approve an action.

        The message carries a link, never an approval control. Slack identity is
        not Aegis authority: approval is recorded server side against an
        authenticated principal, so a button here would be a second, weaker path
        to the same decision.
        """
        self._require_configured("post_approval_request")
        channel = _check_channel(channel)
        if not approval_url.startswith(("https://", "http://localhost")):
            raise ValidationError(
                "approval_url must be an absolute https url",
                context={"url": approval_url[:96]},
            )
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Approval required: {action_type}"[:150]},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary[:MAX_TEXT_CHARS]},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"incident `{incident_id}` | expires "
                            f"{expires_at.isoformat(timespec='seconds')} | "
                            f"<{approval_url}|review and decide in Aegis>"
                        ),
                    }
                ],
            },
        ]
        validate_blocks(blocks)
        return await self._post(
            channel,
            blocks,
            f"Approval required for {action_type} on incident {incident_id}",
            operation="post_approval_request",
        )

    async def update_message(
        self, channel: str, ts: str, blocks_payload: list[dict[str, Any]]
    ) -> SlackMessageRef:
        """WRITE. Replace a posted message in place.

        Idempotent in the useful sense - the message ends in the state described
        - but still never auto-retried, for the same reason as a post.
        """
        self._require_configured("update_message")
        if not self.can_update:
            raise ConfigError(
                "slack updates require SLACK_BOT_TOKEN; a webhook cannot edit a message",
                context={"dependency": "slack", "operation": "update_message"},
            )
        channel = _check_channel(channel)
        if not _TS_RE.match(ts):
            raise ValidationError("invalid slack message ts", context={"ts": ts[:32]})
        blocks = validate_blocks(blocks_payload)
        payload = await self._send(
            f"{SLACK_API_URL}/chat.update",
            {"channel": channel, "ts": ts, "blocks": blocks},
            operation="update_message",
        )
        return SlackMessageRef(
            channel=str(payload.get("channel", channel)),
            ts=str(payload.get("ts", ts)),
            transport="bot",
            performed=f"POST {SLACK_API_URL}/chat.update channel={channel} ts={ts}",
        )

    async def _post(
        self,
        channel: str,
        blocks: list[dict[str, Any]],
        fallback_text: str,
        *,
        operation: str,
    ) -> SlackMessageRef:
        """Prefer the Web API; fall back to the webhook when that is all there is."""
        if self._bot_token:
            body = {"channel": channel, "blocks": blocks}
            if fallback_text:
                body["text"] = fallback_text[:MAX_TEXT_CHARS]
            payload = await self._send(
                f"{SLACK_API_URL}/chat.postMessage", body, operation=operation
            )
            ref = SlackMessageRef(
                channel=str(payload.get("channel", channel)),
                ts=str(payload.get("ts", "")),
                transport="bot",
                performed=f"POST {SLACK_API_URL}/chat.postMessage channel={channel}",
            )
        else:
            body = {"blocks": blocks}
            if fallback_text:
                body["text"] = fallback_text[:MAX_TEXT_CHARS]
            await self._send(self._webhook_url, body, operation=operation)
            # A webhook returns no timestamp, so the message cannot be edited
            # later. Reporting an empty ts is honest; inventing one is not.
            ref = SlackMessageRef(
                channel=channel,
                ts="",
                transport="webhook",
                performed="POST <slack incoming webhook>",
            )
        log.info(
            "slack message sent", operation=operation, channel=channel, transport=ref.transport
        )
        return ref


__all__ = [
    "MAX_BLOCKS",
    "MAX_TEXT_CHARS",
    "SLACK_API_URL",
    "SlackClient",
    "SlackMessageRef",
    "validate_blocks",
]
