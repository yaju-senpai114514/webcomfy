"""Registry of remote ComfyUI servers webcomfy orchestrates.

One `servers.json` file at the repo root holds an ordered list of entries
(id, name, base_url, optional models-API token, enabled flag). On first run the
registry is seeded from the legacy COMFY_BASE_URL / WEBCOMFY_MODELS_TOKEN env
vars so a single-server setup keeps working unchanged.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from paths import ROOT_DIR

SERVERS_FILE = ROOT_DIR / "servers.json"


class ServerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    base_url: str  # e.g. http://host:8188 (no trailing slash)
    token: str = ""  # WEBCOMFY_MODELS_TOKEN of that ComfyUI; "" = no auth
    enabled: bool = True


class _Store(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    servers: list[ServerEntry] = []


def _load() -> _Store:
    try:
        return _Store.model_validate_json(SERVERS_FILE.read_text())
    except (OSError, ValueError):
        return _Store()


def _save(store: _Store) -> None:
    fd, tmp = tempfile.mkstemp(dir=SERVERS_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(store.model_dump_json(indent=2))
        os.replace(tmp, SERVERS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_all() -> list[ServerEntry]:
    return _load().servers


def list_enabled() -> list[ServerEntry]:
    return [s for s in _load().servers if s.enabled]


def get(sid: str) -> ServerEntry:
    for s in _load().servers:
        if s.id == sid:
            return s
    raise KeyError(sid)


def create(name: str, base_url: str, token: str = "", enabled: bool = True) -> ServerEntry:
    entry = ServerEntry(
        id=uuid.uuid4().hex[:8],
        name=name.strip() or base_url,
        base_url=base_url.rstrip("/"),
        token=token,
        enabled=enabled,
    )
    store = _load()
    store.servers.append(entry)
    _save(store)
    return entry


def update(
    sid: str,
    name: str | None = None,
    base_url: str | None = None,
    token: str | None = None,
    enabled: bool | None = None,
) -> ServerEntry:
    store = _load()
    for i, s in enumerate(store.servers):
        if s.id == sid:
            if name is not None:
                s.name = name.strip() or s.name
            if base_url is not None:
                s.base_url = base_url.rstrip("/")
            if token is not None:
                s.token = token
            if enabled is not None:
                s.enabled = enabled
            store.servers[i] = s
            _save(store)
            return s
    raise KeyError(sid)


def delete(sid: str) -> None:
    store = _load()
    before = len(store.servers)
    store.servers = [s for s in store.servers if s.id != sid]
    if len(store.servers) == before:
        raise KeyError(sid)
    _save(store)


def default_server() -> ServerEntry | None:
    """The first enabled server — the target when a request names none."""
    enabled = list_enabled()
    return enabled[0] if enabled else None


def ensure_seeded() -> None:
    """First run: migrate the single-server env config into the registry."""
    if SERVERS_FILE.exists():
        return
    base_url = os.environ.get("COMFY_BASE_URL", "http://localhost:8188")
    token = os.environ.get("WEBCOMFY_MODELS_TOKEN", "")
    create("default", base_url, token=token, enabled=True)
