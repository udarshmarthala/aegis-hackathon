"""Authentication and authorization.

Two deliberately separate concerns:

* **Authentication** answers "who is this?" - Firebase verifies the ID token.
* **Authorization** answers "may they do this?" - Aegis roles decide, server
  side, at the moment of the decision.

Conflating them is how an authenticated-but-unprivileged user ends up approving
a production rollback. Firebase never grants Aegis authority.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum

from aegis.core.config import Settings
from aegis.core.errors import AuthenticationError
from aegis.core.logging import get_logger

log = get_logger(__name__)


class Role(StrEnum):
    VIEWER = "viewer"        # read incidents and evidence
    RESPONDER = "responder"  # run investigations, comment
    APPROVER = "approver"    # approve tier-2 actions
    ADMIN = "admin"          # policy, kill switch, integrations

    @property
    def implied(self) -> frozenset[str]:
        """Role inheritance, resolved once here rather than at each call site."""
        ladder = {
            Role.VIEWER: {Role.VIEWER},
            Role.RESPONDER: {Role.VIEWER, Role.RESPONDER},
            Role.APPROVER: {Role.VIEWER, Role.RESPONDER, Role.APPROVER},
            Role.ADMIN: {Role.VIEWER, Role.RESPONDER, Role.APPROVER, Role.ADMIN},
        }
        return frozenset(r.value for r in ladder[self])


@dataclass(frozen=True, slots=True)
class Principal:
    uid: str
    email: str
    roles: frozenset[str]
    display_name: str = ""

    def has(self, role: Role) -> bool:
        return role.value in self.effective_roles

    @property
    def effective_roles(self) -> frozenset[str]:
        out: set[str] = set()
        for raw in self.roles:
            try:
                out |= set(Role(raw).implied)
            except ValueError:
                continue  # unknown role grants nothing
        return frozenset(out)


class FirebaseVerifier:
    """Wraps firebase-admin so the rest of the app never imports it directly.

    Initialisation is lazy and failure-tolerant: if the service account is
    missing the verifier reports unconfigured rather than crashing the process,
    and the API then rejects every authenticated request. Fail closed, stay up.
    """

    __slots__ = ("_app", "_ready", "_settings")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._app = None
        self._ready = False

    def initialise(self) -> bool:
        if self._ready:
            return True
        path = self._settings.firebase_service_account_path
        project = self._settings.firebase_project_id
        if not path or not project:
            log.warning("firebase not configured; token verification unavailable")
            return False
        try:
            import firebase_admin
            from firebase_admin import credentials

            if firebase_admin._apps:  # noqa: SLF001 - library exposes no public check
                self._app = firebase_admin.get_app()
            else:
                self._app = firebase_admin.initialize_app(
                    credentials.Certificate(path), {"projectId": project}
                )
            self._ready = True
            log.info("firebase verifier ready", project_id=project)
        except Exception as exc:  # noqa: BLE001 - never fatal at boot
            log.error("firebase initialisation failed", error=str(exc))
            return False
        return self._ready

    @property
    def ready(self) -> bool:
        return self._ready

    def verify(self, id_token: str) -> Principal:
        if not self._ready and not self.initialise():
            raise AuthenticationError("identity provider unavailable",
                                      code="AUTH_PROVIDER_UNAVAILABLE")
        try:
            from firebase_admin import auth as fb_auth

            claims = fb_auth.verify_id_token(id_token, check_revoked=False)
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError("invalid or expired token") from exc

        # Roles come from Aegis, carried as a custom claim set by an admin.
        # A token cannot mint its own authority.
        raw_roles = claims.get("aegis_roles") or [Role.VIEWER.value]
        return Principal(
            uid=claims["uid"],
            email=claims.get("email", ""),
            roles=frozenset(raw_roles),
            display_name=claims.get("name", ""),
        )


def verify_ingest_token(supplied: str | None, settings: Settings) -> None:
    """Constant-time check for the machine-to-machine ingestion token.

    ``compare_digest`` avoids leaking the token through response timing.
    """
    expected = settings.alert_ingest_token.get_secret_value()
    if not expected:
        raise AuthenticationError("ingestion token is not configured",
                                  code="INGEST_TOKEN_UNSET")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise AuthenticationError("invalid ingestion token")
