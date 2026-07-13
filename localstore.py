"""Local model repository on the webcomfy host.

The staging ground for the fleet: LoRA/체크포인트 files live here and webcomfy
pushes them to remote ComfyUI servers on demand — explicitly from the /models
page (diff + copy) or transparently right before a generation that needs a
model the target server is missing (see server.provision_missing).

Layout mirrors the remote model API (MODEL_API_SPEC.md) so the /models UI and
copy jobs treat "local" as just another server id:

    <LOCAL_MODELS_DIR>/<category>/<name>        e.g. local_models/loras/foo.safetensors
    <LOCAL_MODELS_DIR>/.index.json              sha256/uploaded_at metadata
    <LOCAL_MODELS_DIR>/.tmp/                    in-flight uploads (same filesystem)

All public functions return the spec's JSON shapes (file meta objects, category
summaries) and raise modelapi.ModelAPIError with the spec's error codes, so
FastAPI handlers can serve either backend through one code path.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from modelapi import ModelAPIError

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("LOCAL_MODELS_DIR", BASE_DIR / "local_models")).resolve()
INDEX_PATH = ROOT / ".index.json"
TMP_DIR = ROOT / ".tmp"

# Categories mirror ComfyUI's folder_paths keys (modern alias names).
DEFAULT_CATEGORIES = (
    "checkpoints", "diffusion_models", "loras", "text_encoders",
    "vae", "upscale_models", "embeddings",
)
ALLOWED_EXTS = {".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".yaml"}
CHUNK = 1 << 20


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_name(name: str) -> bool:
    if not name or "\\" in name or "\0" in name:
        return False
    if name.startswith("/") or name.endswith("/"):
        return False
    for seg in name.split("/"):
        if not seg or seg in (".", "..") or seg.startswith("."):
            return False
    return True


def _check(category: str, name: str) -> Path:
    """Validate (category, name) and return the confined absolute path."""
    if category not in categories():
        raise ModelAPIError(404, "unknown_category", f"unknown category: {category}")
    if not _valid_name(name):
        raise ModelAPIError(400, "invalid_name", f"invalid name: {name}")
    base = (ROOT / category).resolve()
    resolved = Path(os.path.realpath(base / name))
    if not str(resolved).startswith(str(base) + os.sep):
        raise ModelAPIError(400, "invalid_name", f"invalid name: {name}")
    return resolved


def categories() -> list[str]:
    found = set(DEFAULT_CATEGORIES)
    if ROOT.is_dir():
        found |= {p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}
    return sorted(found)


# --- metadata index (same semantics as the custom node's) --------------------
_index_lock = threading.Lock()


def _index_load() -> dict[str, Any]:
    try:
        files = json.loads(INDEX_PATH.read_text()).get("files")
        if isinstance(files, dict):
            return files
    except (OSError, ValueError):
        pass
    return {}


def _index_write(files: dict[str, Any]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=INDEX_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "files": files}, f, indent=1)
        os.replace(tmp, INDEX_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _index_get(key: str) -> dict[str, Any] | None:
    with _index_lock:
        entry = _index_load().get(key)
    return entry if isinstance(entry, dict) else None


def _index_put(key: str, entry: dict[str, Any]) -> None:
    with _index_lock:
        files = _index_load()
        files[key] = entry
        _index_write(files)


def _index_drop(key: str) -> None:
    with _index_lock:
        files = _index_load()
        if files.pop(key, None) is not None:
            _index_write(files)


# --- background hash worker (single slot) ------------------------------------
_hash_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="localstore-hash")
_pending: set[str] = set()
_pending_lock = threading.Lock()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _hash_job(key: str, path: Path, snap: tuple[int, float]) -> None:
    try:
        digest = _sha256_file(path)
        st = path.stat()
        if (st.st_size, st.st_mtime) != snap:
            return  # changed while hashing — discard
        prev = _index_get(key)
        uploaded_at = None
        if prev and prev.get("size") == st.st_size and prev.get("mtime") == st.st_mtime:
            uploaded_at = prev.get("uploaded_at")
        _index_put(key, {
            "size": st.st_size, "mtime": st.st_mtime,
            "sha256": digest, "uploaded_at": uploaded_at,
        })
    except OSError:
        pass
    finally:
        with _pending_lock:
            _pending.discard(key)


def _enqueue_hash(key: str, path: Path) -> None:
    st = path.stat()
    with _pending_lock:
        if key in _pending:
            return
        _pending.add(key)
    _hash_pool.submit(_hash_job, key, path, (st.st_size, st.st_mtime))


# --- file meta / listing (spec-shaped responses) ------------------------------
def _meta(category: str, name: str, path: Path) -> dict[str, Any]:
    st = path.stat()
    key = f"{category}/{name}"
    entry = _index_get(key)
    sha256 = uploaded_at = None
    if entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime:
        sha256 = entry.get("sha256")
        uploaded_at = entry.get("uploaded_at")
    with _pending_lock:
        pending = key in _pending
    return {
        "name": name,
        "category": category,
        "size": st.st_size,
        "mtime": _iso(st.st_mtime),
        "sha256": sha256,
        "uploaded_at": uploaded_at,
        "hash_state": "done" if sha256 else ("pending" if pending else "none"),
    }


def _names(category: str) -> list[str]:
    base = ROOT / category
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXTS:
            continue
        rel = p.relative_to(base)
        if any(seg.startswith(".") for seg in rel.parts):
            continue
        out.append(str(rel).replace(os.sep, "/"))
    return out


def summary() -> dict[str, Any]:
    cats = []
    for category in categories():
        names = _names(category)
        total = 0
        for n in names:
            try:
                total += (ROOT / category / n).stat().st_size
            except OSError:
                continue
        cats.append({"category": category, "file_count": len(names), "total_size": total})
    return {"api_version": 1, "categories": cats}


def list_files(category: str) -> dict[str, Any]:
    if category not in categories():
        raise ModelAPIError(404, "unknown_category", f"unknown category: {category}")
    return {"files": [_meta(category, n, ROOT / category / n) for n in _names(category)]}


def file_meta(category: str, name: str) -> dict[str, Any]:
    path = _check(category, name)
    if not path.is_file():
        raise ModelAPIError(404, "not_found", f"{category}/{name} not found")
    return _meta(category, name, path)


def path_of(category: str, name: str) -> Path:
    """Absolute path of an existing file (404 if absent) — for downloads/pushes."""
    path = _check(category, name)
    if not path.is_file():
        raise ModelAPIError(404, "not_found", f"{category}/{name} not found")
    return path


def exists(category: str, name: str) -> bool:
    try:
        return path_of(category, name) is not None
    except ModelAPIError:
        return False


def delete(category: str, name: str) -> None:
    path = path_of(category, name)
    try:
        os.unlink(path)
    except OSError as exc:
        raise ModelAPIError(500, "internal", f"delete failed: {exc}")
    _index_drop(f"{category}/{name}")


def trigger_hash(category: str, name: str, force: bool = False) -> tuple[int, dict[str, Any]]:
    path = path_of(category, name)
    meta = _meta(category, name, path)
    if meta["hash_state"] == "done" and not force:
        return 200, meta
    _enqueue_hash(f"{category}/{name}", path)
    return 202, {"hash_state": "pending"}


def options_map() -> dict[str, list[str]]:
    """Local file names keyed like ComfyOptions — merged into the UI dropdowns
    so a model that only exists locally is selectable (auto-pushed on use)."""
    return {
        "loras": _names("loras"),
        "unets": _names("diffusion_models") + _names("unet"),
        "clips": _names("text_encoders") + _names("clip"),
        "vaes": _names("vae"),
        "upscale_models": _names("upscale_models"),
    }


# --- streaming upload ---------------------------------------------------------
class Uploader:
    """Chunk-fed upload into the store: tmp file + sha256, atomic finish.

    Use as a context manager; an exception (client disconnect, hash mismatch)
    aborts and removes the tmp file.
    """

    def __init__(self, category: str, name: str, replace: bool = False) -> None:
        dest = _check(category, name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTS:
            raise ModelAPIError(400, "invalid_extension", f"extension {ext or '(none)'} not allowed")
        if dest.is_file() and not replace:
            raise ModelAPIError(409, "already_exists", f"{category}/{name} exists; pass replace=1")
        self.category = category
        self.name = name
        self.dest = dest
        self.replaced = dest.is_file()
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        fd, self.tmp_path = tempfile.mkstemp(dir=TMP_DIR, suffix=".part")
        self._file = os.fdopen(fd, "wb")
        self._hasher = hashlib.sha256()
        self._done = False

    def write(self, chunk: bytes) -> None:
        self._file.write(chunk)
        self._hasher.update(chunk)

    def finish(self, expected_sha256: str | None = None) -> tuple[int, dict[str, Any]]:
        """Close, verify, move into place; returns (status_code, file meta)."""
        self._file.close()
        digest = self._hasher.hexdigest()
        if expected_sha256 and digest != expected_sha256.lower():
            os.unlink(self.tmp_path)
            self._done = True
            raise ModelAPIError(422, "hash_mismatch", f"expected {expected_sha256}, got {digest}")
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.tmp_path, self.dest)
        self._done = True
        st = self.dest.stat()
        _index_put(f"{self.category}/{self.name}", {
            "size": st.st_size, "mtime": st.st_mtime,
            "sha256": digest, "uploaded_at": _now_iso(),
        })
        return (200 if self.replaced else 201), _meta(self.category, self.name, self.dest)

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._file.close()
        except OSError:
            pass
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass

    def __enter__(self) -> "Uploader":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.abort()
