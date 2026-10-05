"""Explicit durable authority adapter for tests; no ambient cloud access."""

import time
from copy import deepcopy
from threading import RLock
from uuid import uuid4

from mcp.server.auth.provider import AccessToken
from eng_platform_api.services import database_job_store as store, mcp_store, mcp_grants


class Control:
    def __init__(self):
        self.records = {}
        self.lock = RLock()

    def get(self, kind, identity):
        with self.lock:
            return deepcopy(self.records.get(store.key(kind, identity)))

    def mutate(self, references, transform):
        with self.lock:
            changes = transform(
                {
                    store.key(*r): deepcopy(self.records.get(store.key(*r)))
                    for r in references
                }
            )
            for key, value in changes.items():
                if value is None:
                    self.records.pop(key, None)
                else:
                    self.records[key] = deepcopy(value)

    def find(self, kind, field, value, *, limit):
        return [
            deepcopy(r)
            for k, r in self.records.items()
            if k.startswith(kind + ":") and r.get(field) == value
        ][:limit]


def access_token(
    *, token="opaque", client_id="client", subject="diegomad14", scopes=None, **kwargs
):
    scopes = [mcp_grants.SCOPE] if scopes is None else scopes
    if scopes == [mcp_grants.SCOPE]:
        identity = mcp_store.token_key(str(uuid4()))
        grant = {
            "kind": "session",
            "id": identity,
            "source": "mcp",
            "login": subject,
            "client_id": client_id,
            "resource": mcp_grants.resource(None),
            "scopes": scopes,
            "authority_policy": mcp_grants.POLICY,
            "generation": 1,
            "access_hash": mcp_store.token_key(token),
            "created_at": time.time(),
            "expires_at": time.time() + 86400,
            "revoked_at": 0.0,
        }
        store.mutate([("session", identity)], lambda _: {f"session:{identity}": grant})
        mcp_store.save(
            "access",
            mcp_store.token_key(token),
            {
                "session_id": identity,
                "subject": subject,
                "client_id": client_id,
                "scopes": scopes,
                "resource": grant["resource"],
                "generation": 1,
                "expires_at": time.time() + 3600,
            },
        )
    return AccessToken(
        token=token, client_id=client_id, subject=subject, scopes=scopes, **kwargs
    )
