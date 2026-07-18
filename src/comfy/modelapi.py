"""Async client for the webcomfy model-management API on a ComfyUI server.

Consumes the `/webcomfy/models` endpoints (MODEL_API_SPEC.md v1) that the
`webcomfy_models` custom node exposes on each ComfyUI instance: category
summaries, file listings/metadata, streaming download/upload, delete, and hash
(re)computation. Upstream errors are re-raised as `ModelAPIError` carrying the
spec's `{code, message}` payload so FastAPI handlers can relay them verbatim.
"""

from __future__ import annotations

import base64
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from paths import ROOT_DIR
from store.servers import ServerEntry

CHUNK = 1 << 20

# --- Ed25519 request signing (MODEL_API_SPEC.md section 1) -------------------
# Servers with a `key_name` get every request signed with keys/<key_name>.key
# (generate with scripts/gen_keypair.py); the matching .pub must sit in the
# ComfyUI-Remote-Manager extension root. key_name == "" sends unsigned requests
# (only accepted by extensions with no trusted keys deployed).

KEYS_DIR = ROOT_DIR / "keys"
SIG_VERSION = "webcomfy-v1"

_key_cache: dict[Path, tuple[float, Ed25519PrivateKey]] = {}

# Model files run to many GB: never time out mid-body, only on connect/first byte.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=30.0)
_client = httpx.AsyncClient(timeout=_TIMEOUT)


class ModelAPIError(Exception):
    """An upstream model-API failure, mirroring the spec's error envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    def as_json(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


def _private_key(key_name: str) -> Ed25519PrivateKey:
    path = KEYS_DIR / f"{key_name}.key"
    try:
        st = path.stat()
    except OSError:
        raise ModelAPIError(
            500, "signing_key_missing",
            f"{path} not found — generate with scripts/gen_keypair.py",
        )
    cached = _key_cache.get(path)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ModelAPIError(
            500, "signing_key_invalid", f"{path} is not an Ed25519 private key"
        )
    _key_cache[path] = (st.st_mtime, key)
    return key


def _headers(
    entry: ServerEntry,
    method: str,
    parts: tuple[str, ...] = (),
    params: dict[str, str] | None = None,
) -> dict[str, str]:
    """Signature headers for one request; {} when the server has no key."""
    if not entry.key_name:
        return {}
    key = _private_key(entry.key_name)
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    message = "\n".join(
        (SIG_VERSION, method.upper(), _path(*parts), query, timestamp, nonce)
    )
    signature = base64.b64encode(key.sign(message.encode())).decode()
    return {
        "X-Webcomfy-Key": entry.key_name,
        "X-Webcomfy-Timestamp": timestamp,
        "X-Webcomfy-Nonce": nonce,
        "X-Webcomfy-Signature": signature,
    }


def _path(*parts: str) -> str:
    path = "/".join(p.strip("/") for p in parts if p)
    return "/webcomfy/models" + (f"/{path}" if path else "")


def _url(entry: ServerEntry, *parts: str) -> str:
    return entry.base_url + _path(*parts)


def _raise_for(resp: httpx.Response, body: bytes | None = None) -> None:
    """Translate a non-2xx spec response into ModelAPIError."""
    if resp.is_success:
        return
    code, message = "internal", f"upstream returned HTTP {resp.status_code}"
    try:
        err = (resp.json() if body is None else httpx.Response(200, content=body).json())["error"]
        code, message = str(err["code"]), str(err["message"])
    except Exception:  # noqa: BLE001 - non-JSON upstream error body
        pass
    raise ModelAPIError(resp.status_code, code, message)


def _wrap_transport_error(entry: ServerEntry, exc: httpx.HTTPError) -> ModelAPIError:
    return ModelAPIError(502, "upstream_unreachable", f"{entry.base_url}: {exc}")


async def summary(entry: ServerEntry) -> dict[str, Any]:
    """GET /webcomfy/models — category summary."""
    try:
        resp = await _client.get(_url(entry), headers=_headers(entry, "GET"))
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.json()

async def list_files(entry: ServerEntry, category: str) -> dict[str, Any]:
    """GET /webcomfy/models/{category} — full recursive file list."""
    try:
        resp = await _client.get(
            _url(entry, category), headers=_headers(entry, "GET", (category,))
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.json()


async def file_meta(entry: ServerEntry, category: str, name: str) -> dict[str, Any]:
    """GET /webcomfy/models/{category}/{name}?meta=1 — one file's metadata."""
    params = {"meta": "1"}
    try:
        resp = await _client.get(
            _url(entry, category, name),
            params=params,
            headers=_headers(entry, "GET", (category, name), params),
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.json()


@asynccontextmanager
async def download(
    entry: ServerEntry, category: str, name: str, range_header: str | None = None
) -> AsyncGenerator[httpx.Response]:
    """GET /webcomfy/models/{category}/{name} — streaming download.

    Yields the open httpx response (status/headers + aiter_bytes). Forwards an
    optional HTTP Range header so browser-resumed downloads pass through.
    """
    headers = _headers(entry, "GET", (category, name))
    if range_header:
        headers["Range"] = range_header
    try:
        async with _client.stream(
            "GET", _url(entry, category, name), headers=headers
        ) as resp:
            if not resp.is_success and resp.status_code != 206:
                _raise_for(resp, body=await resp.aread())
            yield resp
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc


async def upload(
    entry: ServerEntry,
    category: str,
    name: str,
    content: AsyncIterator[bytes],
    replace: bool = False,
    sha256: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST /webcomfy/models/{category}/{name} — raw-binary streaming upload.

    Returns (status_code, file-meta json). 201 = new file, 200 = replaced.
    """
    params: dict[str, str] = {}
    if replace:
        params["replace"] = "1"
    if sha256:
        params["sha256"] = sha256
    try:
        resp = await _client.post(
            _url(entry, category, name),
            params=params,
            headers={
                **_headers(entry, "POST", (category, name), params),
                "Content-Type": "application/octet-stream",
            },
            content=content,
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.status_code, resp.json()


async def delete(entry: ServerEntry, category: str, name: str) -> None:
    """DELETE /webcomfy/models/{category}/{name}."""
    try:
        resp = await _client.delete(
            _url(entry, category, name), headers=_headers(entry, "DELETE", (category, name))
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)


async def trigger_hash(
    entry: ServerEntry, category: str, name: str, force: bool = False
) -> tuple[int, dict[str, Any]]:
    """POST /webcomfy/models/{category}/{name}/hash — returns (status, body).

    200 = already hashed (body is the file meta), 202 = computation queued.
    """
    params = {"force": "1"} if force else {}
    try:
        resp = await _client.post(
            _url(entry, category, name, "hash"),
            params=params,
            headers=_headers(entry, "POST", (category, name, "hash"), params),
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.status_code, resp.json()
