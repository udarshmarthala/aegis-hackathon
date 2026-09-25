"""Runtime adapters: the factory fails closed, writes are separated and bounded.

``ComposeAdapter`` is driven through a fake Docker client that implements exactly
the Engine API surface the adapter touches, so these tests cover the real code
path without a daemon.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from aegis.core.config import Settings
from aegis.core.errors import (
    ConfigError,
    ExternalServiceError,
    NotFoundError,
    SourceUnavailable,
    ValidationError,
)
from aegis.core.resilience import reset_breakers
from aegis.domain.enums import ServiceHealth
from aegis.domain.models import UntrustedText
from aegis.integrations.runtime import (
    AEGIS_MANAGED_LABEL,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    ComposeAdapter,
    EcsAdapter,
    KubernetesAdapter,
    RuntimeAdapter,
    get_adapter,
    service_name_of,
)

PROJECT = "aegis-2-0"


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_breakers()
    yield
    reset_breakers()


def settings(**over) -> Settings:
    base = {
        "_env_file": None,
        "aegis_environment_name": "local-docker",
        "workload_namespace": "aegis-workload",
    }
    return Settings(**{**base, **over})


# --- fake docker engine -----------------------------------------------------


class FakeContainer:
    def __init__(
        self,
        cid: str,
        service: str,
        *,
        status: str = "running",
        image: str = "aegis/workload:1.4.2",
        health: str | None = None,
        managed: bool = False,
        networks: tuple[str, ...] = ("aegis-net",),
    ) -> None:
        self.id = cid
        self.name = f"{PROJECT}-{service}-1"
        self.status = status
        self.labels = {
            COMPOSE_PROJECT_LABEL: PROJECT,
            COMPOSE_SERVICE_LABEL: service,
        }
        if managed:
            self.labels[AEGIS_MANAGED_LABEL] = "true"
        state: dict = {"Status": status, "StartedAt": "2026-09-20T09:00:00Z"}
        if health is not None:
            state["Health"] = {"Status": health}
        self.attrs = {
            "State": state,
            "Config": {"Image": image, "Env": ["SERVICE_NAME=" + service]},
            "NetworkSettings": {"Networks": dict.fromkeys(networks, {})},
            "RestartCount": 2,
        }
        self.calls: list[tuple] = []

    def restart(self, timeout: int = 10) -> None:
        self.calls.append(("restart", timeout))
        self.status = "running"

    def stop(self, timeout: int = 10) -> None:
        self.calls.append(("stop", timeout))
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.calls.append(("remove", force))

    def start(self) -> None:
        self.calls.append(("start",))

    def reload(self) -> None:
        self.calls.append(("reload",))

    def logs(self, **kwargs) -> bytes:
        self.calls.append(("logs", kwargs))
        return b"starting up\nuser 7 not found\nready\n"


class FakeNetwork:
    def __init__(self, name: str, log: list) -> None:
        self.name = name
        self._log = log

    def disconnect(self, container) -> None:
        self._log.append((self.name, container.id))

    def connect(self, container, aliases=None) -> None:
        self._log.append((self.name, container.id, "connect", tuple(aliases or ())))


class FakeDocker:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self._containers = containers
        self.disconnects: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.closed = False
        outer = self

        class _Containers:
            def list(self, **kwargs):
                # The SDK takes an ``all=`` flag; it is irrelevant to the fake,
                # which always holds every container it was given.
                wanted = (kwargs.get("filters") or {}).get("label", [])
                return [
                    container
                    for container in outer._containers
                    if all(
                        container.labels.get(item.split("=", 1)[0]) == item.split("=", 1)[1]
                        for item in wanted
                    )
                ]

            def get(self, ident):
                for container in outer._containers:
                    if container.id.startswith(ident) or container.name == ident:
                        return container
                raise KeyError(ident)

            def create(self, image, **kwargs):
                outer.created.append({"image": image, **kwargs})
                created = FakeContainer(
                    f"new{len(outer.created)}00000000000",
                    kwargs["labels"][COMPOSE_SERVICE_LABEL],
                    image=image,
                )
                created.labels.update(kwargs["labels"])
                outer._containers.append(created)
                return created

            run = create

        class _Networks:
            def get(self, name):
                return FakeNetwork(name, outer.disconnects)

        self.containers = _Containers()
        self.networks = _Networks()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def compose(containers: list[FakeContainer], **over) -> ComposeAdapter:
    return ComposeAdapter(settings(**over), project=PROJECT, client=FakeDocker(containers))


# --- the boundary itself ----------------------------------------------------


def test_read_and_write_method_sets_are_disjoint():
    assert not (RuntimeAdapter.READ_METHODS & RuntimeAdapter.WRITE_METHODS)
    assert sorted(RuntimeAdapter.WRITE_METHODS) == [
        "drain_instance",
        "restart_instance",
        "rollback_deployment",
        "scale",
    ]


@pytest.mark.parametrize("adapter_cls", [ComposeAdapter, KubernetesAdapter, EcsAdapter])
def test_every_adapter_implements_the_whole_surface(adapter_cls):
    for name in RuntimeAdapter.READ_METHODS | RuntimeAdapter.WRITE_METHODS:
        member = getattr(adapter_cls, name)
        assert inspect.iscoroutinefunction(member), f"{adapter_cls.__name__}.{name}"
        # Concrete adapters must override the abstract declaration.
        assert member is not getattr(RuntimeAdapter, name)


def test_write_methods_all_require_an_idempotency_key():
    for name in RuntimeAdapter.WRITE_METHODS:
        signature = inspect.signature(getattr(RuntimeAdapter, name))
        param = signature.parameters["idempotency_key"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


# --- factory ----------------------------------------------------------------


def test_factory_selects_the_configured_adapter():
    assert isinstance(get_adapter(settings(workload_adapter="compose")), ComposeAdapter)
    assert isinstance(get_adapter(settings(workload_adapter="kubernetes")), KubernetesAdapter)
    assert isinstance(get_adapter(settings(workload_adapter="ecs")), EcsAdapter)


@pytest.mark.parametrize("value", ["nomad", "", "COMPOSE", None])
def test_factory_fails_closed_on_an_unknown_adapter(value):
    """Never fall back to compose - that would point writes at the wrong system."""
    with pytest.raises(ConfigError) as exc:
        get_adapter(SimpleNamespace(workload_adapter=value))
    assert "unknown workload_adapter" in exc.value.message


# --- identifiers ------------------------------------------------------------


def test_service_name_accepts_canonical_ids_and_bare_names():
    assert service_name_of("local-docker:aegis-workload:checkout") == "checkout"
    assert service_name_of("checkout") == "checkout"


@pytest.mark.parametrize("bad", ["../etc", "a b", "", "svc;rm -rf /", "$(id)"])
def test_service_name_rejects_anything_else(bad):
    with pytest.raises(ValidationError):
        service_name_of(bad)


# --- unavailable backends ---------------------------------------------------


def test_kubernetes_reports_itself_unavailable_without_a_client():
    adapter = KubernetesAdapter(settings(workload_adapter="kubernetes"))
    if adapter.available:  # pragma: no cover - only when a real kubeconfig exists
        pytest.skip("a usable kubeconfig is present on this machine")
    assert "kubernetes" in adapter.unavailable_reason or "kubeconfig" in adapter.unavailable_reason


async def test_unavailable_kubernetes_raises_instead_of_returning_empty():
    adapter = KubernetesAdapter(settings(workload_adapter="kubernetes"))
    if adapter.available:  # pragma: no cover
        pytest.skip("a usable kubeconfig is present on this machine")
    with pytest.raises(SourceUnavailable) as exc:
        await adapter.list_services()
    assert "kubernetes adapter not configured" in exc.value.message
    assert await adapter.ping() is False


async def test_unconfigured_ecs_is_unavailable_for_reads_and_writes():
    adapter = EcsAdapter(settings(workload_adapter="ecs", ecs_cluster=""))
    assert adapter.available is False
    assert adapter.unavailable_reason == "ecs_cluster is not set"
    with pytest.raises(SourceUnavailable):
        await adapter.list_services()
    with pytest.raises(SourceUnavailable):
        await adapter.scale("checkout", 2, idempotency_key="k1")


# --- compose reads ----------------------------------------------------------


async def test_list_services_groups_containers_and_normalises_health():
    adapter = compose(
        [
            FakeContainer("aaaaaaaaaaaa11", "checkout"),
            FakeContainer("bbbbbbbbbbbb22", "checkout", status="exited"),
            FakeContainer("cccccccccccc33", "payment", health="unhealthy"),
        ]
    )

    states = await adapter.list_services()

    by_name = {s.ref.name: s for s in states}
    assert by_name["checkout"].desired_instances == 2
    assert by_name["checkout"].ready_instances == 1
    assert by_name["checkout"].health is ServiceHealth.DEGRADED
    assert by_name["checkout"].version == "1.4.2"
    assert by_name["payment"].health is ServiceHealth.CRITICAL
    # Identity is canonical and adapter-independent.
    assert by_name["payment"].ref.service_id == "local-docker:aegis-workload:payment"


async def test_get_service_raises_not_found_for_an_absent_service():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    with pytest.raises(NotFoundError):
        await adapter.get_service("ghost")
    # health() degrades to UNKNOWN rather than reporting a service as broken.
    assert await adapter.health("ghost") is ServiceHealth.UNKNOWN


async def test_list_instances_reports_raw_and_normalised_status():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout", health="starting")])
    instances = await adapter.list_instances("local-docker:aegis-workload:checkout")
    assert instances[0].instance_id == "aaaaaaaaaaaa"
    assert instances[0].raw_status == "running (starting)"
    assert instances[0].health is ServiceHealth.DEGRADED
    assert instances[0].restart_count == 2
    assert instances[0].version == "1.4.2"


async def test_logs_are_untrusted_and_bounded():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    chunk = await adapter.get_logs("aaaaaaaaaaaa", lines=2)
    assert len(chunk.lines) == 2
    assert all(isinstance(line, UntrustedText) for line in chunk.lines)
    assert chunk.truncated is True


async def test_log_line_count_must_be_positive():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    with pytest.raises(ValidationError):
        await adapter.get_logs("aaaaaaaaaaaa", lines=0)


# --- compose writes ---------------------------------------------------------


async def test_restart_records_the_exact_call_and_is_replay_safe():
    container = FakeContainer("aaaaaaaaaaaa11", "checkout")
    adapter = compose([container])

    first = await adapter.restart_instance("aaaaaaaaaaaa", idempotency_key="act_1")
    second = await adapter.restart_instance("aaaaaaaaaaaa", idempotency_key="act_1")

    assert first.succeeded is True
    assert first.performed == "POST /containers/aaaaaaaaaaaa/restart?t=10"
    assert first.idempotency_key == "act_1"
    assert first.adapter == "compose"
    # Replaying the key must not restart the container a second time.
    assert second is first
    assert [c for c in container.calls if c[0] == "restart"] == [("restart", 10)]


async def test_every_write_demands_an_idempotency_key():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    with pytest.raises(ValidationError):
        await adapter.restart_instance("aaaaaaaaaaaa", idempotency_key="")


async def test_scale_to_the_current_count_is_a_recorded_no_op():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    result = await adapter.scale("checkout", 1, idempotency_key="act_2")
    assert result.no_op is True
    assert result.succeeded is True


async def test_scale_up_clones_the_running_container():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    result = await adapter.scale("checkout", 3, idempotency_key="act_3")
    docker = adapter._client
    assert result.no_op is False
    assert len(docker.created) == 2
    # Clones are labelled so a later scale-down can only reclaim Aegis's own.
    assert all(c["labels"][AEGIS_MANAGED_LABEL] == "true" for c in docker.created)
    assert all(c["image"] == "aegis/workload:1.4.2" for c in docker.created)


async def test_scale_down_refuses_to_remove_containers_aegis_did_not_create():
    adapter = compose(
        [FakeContainer("aaaaaaaaaaaa11", "checkout"), FakeContainer("bbbbbbbbbbbb22", "checkout")]
    )
    with pytest.raises(ExternalServiceError) as exc:
        await adapter.scale("checkout", 1, idempotency_key="act_4")
    assert exc.value.retryable is False
    assert "did not create" in exc.value.message


async def test_scale_down_reclaims_its_own_replicas():
    managed = FakeContainer("bbbbbbbbbbbb22", "checkout", managed=True)
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout"), managed])

    result = await adapter.scale("checkout", 1, idempotency_key="act_5")

    assert result.no_op is False
    assert ("stop", 10) in managed.calls
    assert ("remove", False) in managed.calls


@pytest.mark.parametrize("bad", [0, -1, 11, 1000])
async def test_scale_is_bounded_on_both_ends(bad: int):
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    with pytest.raises(ValidationError):
        await adapter.scale("checkout", bad, idempotency_key=f"act_{bad}")


async def test_drain_disconnects_networks_before_stopping():
    container = FakeContainer("aaaaaaaaaaaa11", "checkout", networks=("aegis-net", "edge"))
    adapter = compose([container])

    result = await adapter.drain_instance("aaaaaaaaaaaa", idempotency_key="act_6")

    assert [n for n, _ in adapter._client.disconnects] == ["aegis-net", "edge"]
    assert ("stop", 30) in container.calls
    assert result.no_op is False
    assert "disconnect" in result.performed


async def test_drain_of_a_stopped_container_is_a_no_op():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout", status="exited")])
    result = await adapter.drain_instance("aaaaaaaaaaaa", idempotency_key="act_7")
    assert result.no_op is True
    assert adapter._client.disconnects == []


async def test_rollback_starts_the_replacement_before_stopping_the_old_one():
    old = FakeContainer("aaaaaaaaaaaa11", "checkout", image="aegis/workload:1.4.2")
    adapter = compose([old])

    result = await adapter.rollback_deployment("checkout", "1.4.1", idempotency_key="act_8")

    assert adapter._client.created[0]["image"] == "aegis/workload:1.4.1"
    assert ("stop", 30) in old.calls
    assert result.no_op is False
    assert "1.4.1" in result.performed
    new = adapter._client._containers[-1]
    assert ("start",) in new.calls


async def test_rollback_keeps_the_service_dns_name_and_a_unique_container_name():
    # Callers reach the service by its Compose name; a replacement that does not
    # answer to it is an outage. And two redeploys of one service must not
    # collide on the replacement's container name.
    old = FakeContainer("aaaaaaaaaaaa11", "checkout", image="aegis/workload:1.4.2")
    adapter = compose([old])
    await adapter.rollback_deployment("checkout", "1.4.1", idempotency_key="act_20")
    await adapter.rollback_deployment("checkout", "1.4.2", idempotency_key="act_21")

    docker = adapter._client
    connects = [entry for entry in docker.disconnects if len(entry) == 4]
    assert connects and all(entry[3] == ("checkout",) for entry in connects)
    names = [created["name"] for created in docker.created]
    assert len(names) == 2 and len(set(names)) == 2


async def test_rollback_to_the_running_version_is_a_no_op():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout", image="aegis/workload:1.4.2")])
    result = await adapter.rollback_deployment("checkout", "1.4.2", idempotency_key="act_9")
    assert result.no_op is True
    assert adapter._client.created == []


async def test_rollback_version_is_validated():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    with pytest.raises(ValidationError):
        await adapter.rollback_deployment("checkout", "1.4.1; rm -rf /", idempotency_key="act_10")


async def test_rollback_without_a_running_container_is_not_found():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout", status="exited")])
    with pytest.raises(NotFoundError):
        await adapter.rollback_deployment("checkout", "1.4.1", idempotency_key="act_11")


async def test_close_releases_the_docker_client():
    adapter = compose([FakeContainer("aaaaaaaaaaaa11", "checkout")])
    docker = adapter._client
    await adapter.close()
    assert docker.closed is True


async def test_rollback_does_not_carry_the_old_images_identity():
    # Image-owned labels and baked-in ENV describe the old build. Copied onto the
    # replacement they would make a 1.4.1 container report itself as 1.4.2.
    old = FakeContainer("aaaaaaaaaaaa11", "checkout", image="aegis/workload:1.4.2")
    old.labels["aegis.version"] = "1.4.2"
    old.labels["aegis.dependency.version"] = "1.0.9"
    adapter = compose([old])
    await adapter.rollback_deployment("checkout", "1.4.1", idempotency_key="act_30")

    created = adapter._client.created[0]
    assert "aegis.version" not in created["labels"]
    assert "aegis.dependency.version" not in created["labels"]
    assert created["labels"][COMPOSE_SERVICE_LABEL] == "checkout"


async def test_a_replacement_that_fails_to_start_is_removed_and_the_old_one_kept():
    old = FakeContainer("aaaaaaaaaaaa11", "checkout", image="aegis/workload:1.4.2")
    adapter = compose([old])
    docker = adapter._client
    original_create = docker.containers.create

    def failing_create(image, **kwargs):
        created = original_create(image, **kwargs)

        def boom() -> None:
            raise RuntimeError("port already allocated")

        created.start = boom
        return created

    docker.containers.create = failing_create
    with pytest.raises(Exception):  # noqa: B017 - any failure must propagate
        await adapter.rollback_deployment("checkout", "1.4.1", idempotency_key="act_31")

    replacement = docker._containers[-1]
    assert ("remove", True) in replacement.calls
    assert ("stop", 30) not in old.calls


async def test_the_replacement_joins_the_service_name_only_once_it_is_ready():
    # Joined while still booting, it would take live traffic it cannot serve and
    # the redeploy itself would read as an outage.
    old = FakeContainer("aaaaaaaaaaaa11", "checkout", image="aegis/workload:1.4.2")
    adapter = compose([old])
    docker = adapter._client
    order: list[str] = []
    original_create = docker.containers.create

    def tracking_create(image, **kwargs):
        created = original_create(image, **kwargs)
        created.attrs["State"] = {"Status": "running", "Health": {"Status": "starting"}}
        polls = {"n": 0}

        def reload() -> None:
            polls["n"] += 1
            order.append("reload")
            if polls["n"] >= 2:
                created.attrs["State"]["Health"]["Status"] = "healthy"

        created.reload = reload
        return created

    docker.containers.create = tracking_create
    original_get = docker.networks.get

    def tracking_get(name):
        network = original_get(name)
        original_connect = network.connect

        def connect(container, aliases=None):
            order.append("connect")
            original_connect(container, aliases=aliases)

        network.connect = connect
        return network

    docker.networks.get = tracking_get
    await adapter.rollback_deployment("checkout", "1.4.1", idempotency_key="act_40")

    assert order.index("connect") > order.index("reload")
    assert order.count("reload") >= 2
