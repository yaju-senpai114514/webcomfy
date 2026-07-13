"""Named-config store: many GenerationConfigs on disk, each with metadata.

Layout: a `configs/` directory with one `<id>.json` per config (holding name +
created/modified timestamps + the GenerationConfig) and a `_state.json` marker
for the currently-selected id. Files starting with `_` are not configs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from paths import ROOT_DIR

from gen.models import GenerationConfig

CONFIGS_DIR = ROOT_DIR / "configs"
STATE_FILE = CONFIGS_DIR / "_state.json"
LEGACY_CONFIG = ROOT_DIR / "config.json"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigMeta(_Strict):
    id: str
    name: str
    created: str  # ISO-8601, seconds
    modified: str


class StoredConfig(ConfigMeta):
    config: GenerationConfig


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path(cid: str) -> Path:
    return CONFIGS_DIR / f"{cid}.json"


def _read(cid: str) -> StoredConfig:
    return StoredConfig.model_validate_json(_path(cid).read_text())


def _write(sc: StoredConfig) -> None:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    _path(sc.id).write_text(sc.model_dump_json(indent=2))


def _state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def _ids() -> list[str]:
    return [p.stem for p in CONFIGS_DIR.glob("*.json") if not p.name.startswith("_")]


def list_metas() -> list[ConfigMeta]:
    metas: list[ConfigMeta] = []
    for cid in _ids():
        try:
            sc = _read(cid)
            metas.append(ConfigMeta(id=sc.id, name=sc.name, created=sc.created, modified=sc.modified))
        except (ValueError, OSError):
            continue
    return metas


def get(cid: str) -> StoredConfig:
    return _read(cid)


def create(name: str, config: GenerationConfig) -> StoredConfig:
    now = _now()
    sc = StoredConfig(id=uuid.uuid4().hex[:8], name=name, created=now, modified=now, config=config)
    _write(sc)
    return sc


def update(
    cid: str,
    config: GenerationConfig | None = None,
    name: str | None = None,
) -> StoredConfig:
    sc = _read(cid)
    if config is not None:
        sc.config = config
    if name is not None:
        sc.name = name
    sc.modified = _now()
    _write(sc)
    return sc


def duplicate(cid: str) -> StoredConfig:
    sc = _read(cid)
    return create(f"{sc.name} copy", sc.config)


def delete(cid: str) -> str | None:
    """Delete a config; if it was selected, pick another. Returns new selected id."""
    _path(cid).unlink(missing_ok=True)
    state = _state()
    if state.get("selected") == cid:
        remaining = _ids()
        new_sel = remaining[0] if remaining else None
        state["selected"] = new_sel
        _save_state(state)
        return new_sel
    return state.get("selected")


def get_selected() -> str | None:
    sid = _state().get("selected")
    if isinstance(sid, str) and _path(sid).exists():
        return sid
    ids = _ids()
    return ids[0] if ids else None


def set_selected(cid: str) -> None:
    state = _state()
    state["selected"] = cid
    _save_state(state)


def ensure_seeded() -> None:
    """On first run, migrate legacy config.json (or defaults) into the store."""
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    if _ids():
        return
    if LEGACY_CONFIG.exists():
        cfg = GenerationConfig.model_validate(json.loads(LEGACY_CONFIG.read_text()))
    else:
        cfg = GenerationConfig()
    sc = create("default", cfg)
    set_selected(sc.id)
