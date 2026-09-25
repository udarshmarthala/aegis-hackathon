"""The incident map: FLUX draws a resolved incident onto its memory card.

Black Forest Labs' API is asynchronous. A ``POST /v1/{model}`` with the
``x-key`` header returns ``{"id", "polling_url"}``; ``GET polling_url`` reports
a ``status`` until it is ``Ready``, at which point ``result.sample`` is a signed
URL on a ``delivery.*.bfl.ai`` host that expires after ten minutes. The image is
downloaded at once and handed back as bytes for the store to keep; the signed
URL is never shown to a browser (it has no CORS and it expires).

Three properties matter more than the picture.

**One submit, never retried.** A generation is billed per request, and a
submit that timed out on our side may have been accepted on theirs. If the
submit fails the map is unavailable, full stop; polling and the download are
plain reads and may be repeated within the deadline.

**Only BFL hosts are fetched.** ``polling_url`` and ``result.sample`` arrive
in a response body. Following them anywhere else would turn a compromised or
spoofed response into a request from inside our network, so both must be
https on ``bfl.ai`` or a subdomain of it (BFL documents regional hosts and
changing ``delivery.*`` clusters, so the check is a suffix, not a list). The
API key is sent to the API host only - never to the delivery host, which
authenticates by signature.

**The prompt carries no free text from the incident.** Service names, action
types and outcomes are reduced to a closed character set; the root cause is
bounded and stripped likewise. Nothing an attacker wrote into a log line can
become an instruction to an image model.

``render`` never raises. A missing key or any failure is
``IncidentMapResult(status="unavailable", source=system, reason=...)`` and the
memory card says "FLUX unavailable" with the reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from aegis.agents.horizon.ports import IncidentMapResult
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import TimeoutExceeded
from aegis.core.logging import get_logger
from aegis.core.resilience import with_timeout
from aegis.domain.horizon import HorizonState, MemoryCard, Source

log = get_logger(__name__)

WIDTH: Final = 1024
HEIGHT: Final = 768  # both multiples of 32, as the API requires
OUTPUT_FORMAT: Final = "jpeg"
MAX_IMAGE_BYTES: Final = 12 * 1024 * 1024
MAX_POLLS: Final = 120
ALLOWED_HOST_SUFFIX: Final = ".bfl.ai"

_FAILED_STATUSES: Final = frozenset(
    {"error", "failed", "request moderated", "content moderated", "task not found"}
)
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAFE_PHRASE_RE = re.compile(r"[^A-Za-z0-9 _.,:/()-]+")


class _Unavailable(Exception):  # noqa: N818 - internal control flow, never escapes
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_bfl_url(url: str) -> bool:
    """https on bfl.ai or a subdomain of it. Everything else is refused."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and (host == "bfl.ai" or host.endswith(ALLOWED_HOST_SUFFIX))
    )


def _token(value: str, limit: int = 40) -> str:
    return _SAFE_TOKEN_RE.sub("-", value).strip("-")[:limit] or "unknown"


def _phrase(value: str, limit: int) -> str:
    return " ".join(_SAFE_PHRASE_RE.sub(" ", value).split())[:limit]


def build_prompt(state: HorizonState, card: MemoryCard) -> str:
    """A deterministic diagram prompt from structured state only."""
    affected: list[str] = []
    for name in [state.service, *(a.target for a in state.actions)]:
        tok = _token(name)
        if tok not in affected:
            affected.append(tok)
    timeline = ["detected"]
    for a in state.actions[:6]:
        timeline.append(f"{_token(a.action_type)} on {_token(a.target)}: {_token(a.outcome)}")
    timeline.append(_token(state.phase.value).lower())
    root = _phrase(card.root_cause, 120) or "undetermined"
    fixed = _token(card.successful_action) if card.successful_action else "none"
    return (
        "Clean technical incident map diagram, dark navy background, flat vector style, "
        "thin neon cyan connector lines, rounded service nodes with crisp sans-serif labels, "
        "no people, no photographs, no logos. "
        f"Service nodes: {', '.join(affected[:6])}. "
        f"Highlight {_token(state.service)} in red as the origin of the blast radius, "
        "with an amber halo on directly affected neighbours. "
        f"Root cause label: {root}. "
        f"Bottom timeline strip, left to right: {' -> '.join(timeline)}. "
        f"Resolution badge in green: {fixed}. "
        f"Title text: {_token(state.incident_id)} resolved."
    )


def _seed(incident_id: str) -> int:
    """Stable per incident, so a re-render of the same state draws the same map."""
    return int.from_bytes(hashlib.sha256(incident_id.encode()).digest()[:4], "big")


class FluxIncidentMap:
    """``IncidentMapRenderer`` over the BFL API."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = SYSTEM_CLOCK,
        *,
        poll_initial_s: float = 0.5,
        poll_max_s: float = 3.0,
    ) -> None:
        self._key = settings.bfl_api_key.get_secret_value()
        self._api_url = settings.bfl_api_url.rstrip("/")
        self._model = _token(settings.bfl_model, 64)
        self._timeout_s = settings.bfl_timeout_s
        self._transport = transport
        self._clock = clock
        self._poll_initial_s = poll_initial_s
        self._poll_max_s = poll_max_s

    def __repr__(self) -> str:
        return f"FluxIncidentMap(model={self._model!r}, configured={self.configured})"

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def status(self) -> dict[str, Any]:
        """For the war-room integrations badge. No secrets."""
        if not self._key:
            return {"configured": False, "reason": "BFL_API_KEY is not set"}
        if not is_bfl_url(self._api_url):
            return {"configured": False, "reason": "BFL_API_URL is not a bfl.ai https URL"}
        return {"configured": True, "reason": ""}

    async def render(self, state: HorizonState, card: MemoryCard) -> IncidentMapResult:
        prompt = build_prompt(state, card)
        ready = self.status()
        if not ready["configured"]:
            return self._unavailable(str(ready["reason"]), prompt)
        started = self._clock.monotonic()
        try:
            image, mime = await with_timeout(
                self._generate(prompt, _seed(state.incident_id)),
                self._timeout_s, what="flux.render",
            )
        except _Unavailable as exc:
            return self._unavailable(exc.reason, prompt)
        except TimeoutExceeded:
            return self._unavailable(f"timed out after {self._timeout_s:.0f}s", prompt)
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            return self._unavailable(f"{type(exc).__name__}", prompt)
        log.info(
            "flux incident map ready", incident_id=state.incident_id, model=self._model,
            bytes=len(image), duration_s=round(self._clock.monotonic() - started, 2),
        )
        return IncidentMapResult(
            status="ready", source=Source.FLUX, image_bytes=image, mime=mime, prompt=prompt,
        )

    # -- the flow ------------------------------------------------------------

    async def _generate(self, prompt: str, seed: int) -> tuple[bytes, str]:
        timeout = httpx.Timeout(min(20.0, self._timeout_s), connect=min(10.0, self._timeout_s))
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, follow_redirects=False
        ) as http:
            polling_url = await self._submit(http, prompt, seed)
            sample_url = await self._poll(http, polling_url)
            return await self._download(http, sample_url)

    async def _submit(self, http: httpx.AsyncClient, prompt: str, seed: int) -> str:
        """Exactly one POST. Any failure here ends the render - never retried."""
        body = {
            "prompt": prompt,
            "width": WIDTH,
            "height": HEIGHT,
            "output_format": OUTPUT_FORMAT,
            "seed": seed,
            "prompt_upsampling": False,
            "safety_tolerance": 2,
        }
        try:
            resp = await http.post(
                f"{self._api_url}/{self._model}",
                json=body,
                headers={"x-key": self._key, "accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise _Unavailable(f"submit failed ({type(exc).__name__}); not retried") from None
        if resp.status_code != 200:
            raise _Unavailable(f"submit rejected: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise _Unavailable("submit returned non-JSON") from None
        polling_url = str(data.get("polling_url") or "") if isinstance(data, dict) else ""
        if not polling_url:
            raise _Unavailable("submit response had no polling_url")
        if not is_bfl_url(polling_url):
            raise _Unavailable("polling_url is not on a bfl.ai host; refused")
        return polling_url

    async def _poll(self, http: httpx.AsyncClient, polling_url: str) -> str:
        delay = self._poll_initial_s
        last = "unknown"
        for _ in range(MAX_POLLS):
            await asyncio.sleep(delay)
            delay = min(self._poll_max_s, max(delay, 0.1) * 1.5)
            try:
                resp = await http.get(
                    polling_url, headers={"x-key": self._key, "accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # a read: keep polling inside the deadline
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise _Unavailable(f"poll rejected: HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError:
                last = "non-JSON poll response"
                continue
            status = str(data.get("status", "")) if isinstance(data, dict) else ""
            last = status or "no status"
            if status == "Ready":
                result = data.get("result")
                sample = str(result.get("sample") or "") if isinstance(result, dict) else ""
                if not sample:
                    raise _Unavailable("Ready without result.sample")
                if not is_bfl_url(sample):
                    raise _Unavailable("result.sample is not on a bfl.ai host; refused")
                return sample
            if status.lower() in _FAILED_STATUSES:
                raise _Unavailable(f"generation {_phrase(status, 40)}")
        raise _Unavailable(f"polling gave up after {MAX_POLLS} polls (last: {_phrase(last, 40)})")

    async def _download(self, http: httpx.AsyncClient, sample_url: str) -> tuple[bytes, str]:
        # No x-key: the delivery host authenticates by signature, and the key
        # has no business travelling to a second host.
        try:
            async with http.stream("GET", sample_url) as resp:
                if resp.status_code != 200:
                    raise _Unavailable(f"image download: HTTP {resp.status_code}")
                mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if not mime.startswith("image/"):
                    raise _Unavailable("image download was not an image")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_IMAGE_BYTES:
                        raise _Unavailable("image exceeds size bound")
        except httpx.HTTPError as exc:
            raise _Unavailable(f"image download failed ({type(exc).__name__})") from None
        return bytes(buf), mime

    def _unavailable(self, reason: str, prompt: str) -> IncidentMapResult:
        log.info("flux incident map unavailable", reason=reason)
        return IncidentMapResult(
            status="unavailable", source=Source.SYSTEM, reason=reason[:300], prompt=prompt,
        )


__all__ = [
    "ALLOWED_HOST_SUFFIX",
    "FluxIncidentMap",
    "build_prompt",
    "is_bfl_url",
]
