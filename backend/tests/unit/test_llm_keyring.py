"""Key-level failover: the ring, and the router built on it.

Aegis has one model vendor and four keys, so redundancy lives at the key. The
property these tests defend is the distinction the ring exists to make:

* a fault another key would survive (429, rejected credential, 5xx) parks the
  key and advances;
* a fault every key shares (a bad model id, an unparseable answer) stops at the
  first key.

Remove that distinction in either direction and something breaks quietly. Fail
over on a 400 and all four keys burn on an identical rejection, and the operator
is told the provider is down when the real cause is a typo in a model name. Fail
to fail over on a 429 and one exhausted free-tier key takes the whole
investigation with it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from aegis.agents.llm import (
    LLMUnavailable,
    ModelRouter,
    TaskClass,
    unavailable_reason,
)
from aegis.core.clock import FrozenClock
from aegis.core.config import Settings
from aegis.core.keyring import KeyFault, KeyRing, classify
from aegis.core.resilience import reset_breakers

KEY1 = "AIzaTESTKEY-1"
KEY2 = "AIzaTESTKEY-2"
KEY3 = "AIzaTESTKEY-3"
KEY4 = "AIzaTESTKEY-4"


class Answer(BaseModel):
    verdict: str


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "google_api_key": "",
        "google_api_key_2": "",
        "google_api_key_3": "",
        "google_api_key_4": "",
        "llm_model_fast": "gemini-2.5-flash",
        "llm_model_reasoning": "gemini-2.5-flash",
        "llm_model_code": "gemini-2.5-flash",
        "llm_max_retries": 0,
        "llm_request_timeout_s": 2.0,
    }
    base.update(over)
    return Settings(**base)


def four_keys(**over: Any) -> Settings:
    return settings(
        google_api_key=KEY1,
        google_api_key_2=KEY2,
        google_api_key_3=KEY3,
        google_api_key_4=KEY4,
        **over,
    )


class HttpFault(Exception):
    """Stands in for an SDK error carrying an HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.status_code = status


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()


def fake_transport(router: ModelRouter, outcomes: list[Any]) -> list[str]:
    """Replace the network with a scripted sequence, recording which key ran.

    The router's own per-(key, model) client cache is seeded, so the real
    selection path runs unchanged - only the transport at the end of it is
    scripted. Each outcome is either an exception to raise or a value to return,
    and the recorded labels are what the assertions are really about: *which key
    was asked*, in what order, and how many times.
    """
    used: list[str] = []
    remaining = list(outcomes)

    class _Structured:
        def __init__(self, label: str) -> None:
            self._label = label

        async def ainvoke(self, _messages: Any) -> Any:
            used.append(self._label)
            outcome = remaining.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class _Client:
        def __init__(self, label: str) -> None:
            self._label = label

        def with_structured_output(self, _schema: Any) -> Any:
            return _Structured(self._label)

    models = {router.model_for(task) for task in TaskClass}
    for index in range(4):
        label = f"key{index + 1}"
        for model in models:
            router._clients[(label, model)] = _Client(label)
    return used


# --------------------------------------------------------------------------- #
# classification                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, KeyFault.QUOTA),
        (401, KeyFault.AUTH),
        (403, KeyFault.AUTH),
        (500, KeyFault.TRANSIENT),
        (503, KeyFault.TRANSIENT),
        (400, KeyFault.REQUEST),
        (404, KeyFault.REQUEST),
        (422, KeyFault.REQUEST),
    ],
)
def test_http_status_decides_whether_another_key_is_worth_trying(
    status: int, expected: KeyFault
) -> None:
    assert classify(HttpFault(status)) is expected


def test_an_unrecognised_failure_is_transient_not_terminal() -> None:
    """Trying the next key costs one call; aborting costs the investigation."""
    assert classify(RuntimeError("who knows")) is KeyFault.TRANSIENT


def test_a_status_carried_in_an_error_context_is_still_read() -> None:
    """Aegis's own ExternalServiceError puts the status in ``context``."""
    from aegis.core.errors import ExternalServiceError

    exc = ExternalServiceError("upstream said no", context={"status": 429})
    assert classify(exc) is KeyFault.QUOTA


# --------------------------------------------------------------------------- #
# the ring                                                                     #
# --------------------------------------------------------------------------- #


def test_keys_are_ordered_and_deduplicated() -> None:
    """The same key pasted twice is redundancy that does not exist.

    Left in, one exhausted quota would look like two healthy accounts.
    """
    ring = KeyRing(
        settings(
            google_api_key=KEY1,
            google_api_key_2=KEY2,
            google_api_key_3=KEY1,
            google_api_key_4="",
        ),
        purpose="llm",
    )
    assert ring.size == 2
    assert [s.secret for s in ring.slots()] == [KEY1, KEY2]


def test_each_key_gets_its_own_circuit_breaker() -> None:
    """A shared breaker would let one exhausted key open the circuit for three
    healthy ones."""
    ring = KeyRing(four_keys(), purpose="llm")
    names = [s.dependency for s in ring.slots()]
    assert names == [
        "llm:google:key1",
        "llm:google:key2",
        "llm:google:key3",
        "llm:google:key4",
    ]
    assert len(set(names)) == 4


def test_a_parked_key_returns_on_its_own() -> None:
    clock = FrozenClock()
    ring = KeyRing(four_keys(), purpose="llm", clock=clock)
    slot = ring.slots()[0]

    ring.park(slot, KeyFault.QUOTA)
    assert [s.label for s in ring.slots()] == ["key2", "key3", "key4"]

    clock.advance(61.0)
    assert [s.label for s in ring.slots()][0] == "key1"


def test_a_rejected_credential_is_parked_far_longer_than_a_quota_blip() -> None:
    """A typo'd key must not consume a slot on every call; a rotated key must
    still recover without a restart."""
    clock = FrozenClock()
    ring = KeyRing(four_keys(), purpose="llm", clock=clock)
    ring.park(ring.slots()[0], KeyFault.AUTH)

    clock.advance(61.0)
    assert "key1" not in [s.label for s in ring.slots()]
    clock.advance(900.0)
    assert [s.label for s in ring.slots()][0] == "key1"


def test_a_request_fault_never_parks_a_key() -> None:
    ring = KeyRing(four_keys(), purpose="llm")
    ring.park(ring.slots()[0], KeyFault.REQUEST)
    assert ring.ready_count == 4


def test_the_ring_never_goes_empty() -> None:
    """All four parked is a bad minute, not an outage.

    An empty ring would turn a transient quota window into a hard failure; one
    more rejected request is cheaper than an investigation that never ran.
    """
    ring = KeyRing(four_keys(), purpose="llm")
    for slot in ring.slots():
        ring.park(slot, KeyFault.QUOTA)
    assert ring.ready_count == 0
    assert len(ring.slots()) == 4


def test_status_carries_no_key_material() -> None:
    ring = KeyRing(four_keys(), purpose="llm")
    ring.park(ring.slots()[0], KeyFault.QUOTA)
    rendered = str(ring.status())
    for key in (KEY1, KEY2, KEY3, KEY4):
        assert key not in rendered
    assert "key1" in rendered


# --------------------------------------------------------------------------- #
# the router                                                                   #
# --------------------------------------------------------------------------- #


def test_an_aggregator_style_model_id_is_stripped() -> None:
    """``google/gemini-2.5-flash`` is a 404 on the native endpoint.

    A 404 during failover is indistinguishable from an outage, so the prefix is
    removed before it can become one.
    """
    router = ModelRouter(settings(llm_model_fast="google/gemini-2.5-flash"))
    assert router.model_for(TaskClass.FAST) == "gemini-2.5-flash"


async def test_no_key_is_an_abstention_not_a_crash() -> None:
    router = ModelRouter(settings())
    assert router.configured is False
    with pytest.raises(LLMUnavailable) as exc:
        await router.structured(schema=Answer, system="s", user="u")
    assert "GOOGLE_API_KEY" in exc.value.message


async def test_the_first_key_is_used_when_it_works() -> None:
    router = ModelRouter(four_keys())
    used = fake_transport(router, [Answer(verdict="ok")])

    result, meta = await router.structured(schema=Answer, system="s", user="u")

    assert result.verdict == "ok"
    assert used == ["key1"]
    assert meta["key"] == "key1"
    assert meta["fallback_used"] is False
    assert meta["provider"] == "google"
    assert meta["model"] == "gemini-2.5-flash"


async def test_an_exhausted_key_fails_over_and_the_answer_still_arrives() -> None:
    router = ModelRouter(four_keys())
    used = fake_transport(router, [HttpFault(429), Answer(verdict="ok")])

    result, meta = await router.structured(schema=Answer, system="s", user="u")

    assert result.verdict == "ok"
    assert used == ["key1", "key2"]
    assert meta["key"] == "key2"
    assert meta["fallback_used"] is True
    assert meta["keys_tried"] == 2


async def test_a_parked_primary_still_counts_as_a_fallback() -> None:
    """``fallback_used`` means "not the primary key", not "not the first key
    attempted".

    Once key1 is parked the ring starts at key2, so a call that never touched
    key1 would otherwise report a healthy primary quota while key1 is down.
    """
    clock = FrozenClock()
    router = ModelRouter(four_keys(), clock)
    used = fake_transport(
        router,
        [HttpFault(429), Answer(verdict="first"), Answer(verdict="second")],
    )

    await router.structured(schema=Answer, system="s", user="u")
    _, meta = await router.structured(schema=Answer, system="s", user="u")

    assert used == ["key1", "key2", "key2"]
    assert meta["key"] == "key2"
    assert meta["keys_tried"] == 1  # key1 was skipped, not tried and failed
    assert meta["fallback_used"] is True


async def test_failover_does_not_change_the_model() -> None:
    """Degrading the key may change which quota paid.

    It must not change the answer's shape, because every safety gate downstream
    is schema-bound.
    """
    router = ModelRouter(four_keys())
    fake_transport(router, [HttpFault(429), Answer(verdict="ok")])

    _, meta = await router.structured(
        schema=Answer, system="s", user="u", task=TaskClass.CODE
    )
    assert meta["model"] == router.model_for(TaskClass.CODE)


async def test_every_key_failing_is_reported_with_what_was_tried() -> None:
    router = ModelRouter(four_keys())
    used = fake_transport(router, [HttpFault(429)] * 4)

    with pytest.raises(LLMUnavailable) as exc:
        await router.structured(schema=Answer, system="s", user="u")

    assert used == ["key1", "key2", "key3", "key4"]
    assert exc.value.context["keys_tried"] == ["key1", "key2", "key3", "key4"]
    assert exc.value.context["fault"] == "quota"


async def test_a_malformed_request_stops_at_the_first_key() -> None:
    router = ModelRouter(four_keys())
    used = fake_transport(router, [HttpFault(400)] * 4)

    with pytest.raises(LLMUnavailable) as exc:
        await router.structured(schema=Answer, system="s", user="u")

    assert used == ["key1"]
    assert exc.value.context["fault"] == "request"
    # The healthy keys were never at fault, so they are still in rotation.
    assert all(row["state"] == "ready" for row in router.key_status())


async def test_an_unparseable_answer_is_surfaced_not_retried_elsewhere() -> None:
    """Another key would produce the same malformed output.

    The orchestrator must be free to abstain rather than proceed on a result it
    could not read.
    """
    from pydantic import ValidationError

    parse_failure: Exception
    try:
        Answer.model_validate({"wrong": 1})
    except ValidationError as exc:
        parse_failure = exc

    router = ModelRouter(four_keys())
    used = fake_transport(router, [parse_failure] * 4)

    with pytest.raises(LLMUnavailable) as exc_info:
        await router.structured(schema=Answer, system="s", user="u")

    assert used == ["key1"]
    assert exc_info.value.context["fault"] == "request"


# --------------------------------------------------------------------------- #
# abstention reasons                                                           #
# --------------------------------------------------------------------------- #


async def test_unconfigured_and_unreachable_are_different_reasons() -> None:
    """"No key configured" and "every key 5xx-ing" are distinct states.

    Collapsing them sends an operator to edit .env when the real problem is that
    Gemini is having a bad minute - the retrieval-side mistake of confusing
    "found nothing" with "could not look", moved into the agent layer.
    """
    unconfigured = ModelRouter(settings())
    with pytest.raises(LLMUnavailable) as absent:
        await unconfigured.structured(schema=Answer, system="s", user="u")

    router = ModelRouter(four_keys())
    fake_transport(router, [HttpFault(503)] * 4)
    with pytest.raises(LLMUnavailable) as unreachable:
        await router.structured(schema=Answer, system="s", user="u")

    absent_reason = unavailable_reason(absent.value)
    unreachable_reason = unavailable_reason(unreachable.value)

    assert absent_reason != unreachable_reason
    assert "configured" in absent_reason
    assert "unreachable" in unreachable_reason


@pytest.mark.parametrize(
    ("status", "expected_fragment"),
    [
        (429, "rate limited"),
        (403, "rejected"),
        (503, "unreachable"),
        (400, "rejected the request"),
    ],
)
async def test_the_abstention_reason_names_the_actual_fault(
    status: int, expected_fragment: str
) -> None:
    router = ModelRouter(four_keys())
    fake_transport(router, [HttpFault(status)] * 4)

    with pytest.raises(LLMUnavailable) as exc:
        await router.structured(schema=Answer, system="s", user="u")

    assert expected_fragment in unavailable_reason(exc.value)


async def test_no_key_material_reaches_the_failure_report() -> None:
    router = ModelRouter(four_keys())
    fake_transport(router, [HttpFault(403)] * 4)

    with pytest.raises(LLMUnavailable) as exc:
        await router.structured(schema=Answer, system="s", user="u")

    rendered = f"{exc.value.message} {exc.value.context}"
    for key in (KEY1, KEY2, KEY3, KEY4):
        assert key not in rendered
