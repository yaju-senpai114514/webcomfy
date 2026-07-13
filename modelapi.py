"""Async client for the webcomfy model-management API on a ComfyUI server.

Consumes the `/webcomfy/models` endpoints (MODEL_API_SPEC.md v1) that the
`webcomfy_models` custom node exposes on each ComfyUI instance: category
summaries, file listings/metadata, streaming download/upload, delete, and hash
(re)computation. Upstream errors are re-raised as `ModelAPIError` carrying the
spec's `{code, message}` payload so FastAPI handlers can relay them verbatim.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator

import httpx

from servers import ServerEntry

CHUNK = 1 << 20

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


def _headers(entry: ServerEntry) -> dict[str, str]:
    return {"Authorization": f"Bearer {entry.token}"} if entry.token else {}


def _url(entry: ServerEntry, *parts: str) -> str:
    path = "/".join(p.strip("/") for p in parts if p)
    return f"{entry.base_url}/webcomfy/models" + (f"/{path}" if path else "")


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
        resp = await _client.get(_url(entry), headers=_headers(entry))
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.json()

async def list_files(entry: ServerEntry, category: str) -> dict[str, Any]:
    """GET /webcomfy/models/{category} — full recursive file list."""
    try:
        resp = await _client.get(_url(entry, category), headers=_headers(entry))
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.json()


async def file_meta(entry: ServerEntry, category: str, name: str) -> dict[str, Any]:
    """GET /webcomfy/models/{category}/{name}?meta=1 — one file's metadata."""
    try:
        resp = await _client.get(
            _url(entry, category, name), params={"meta": "1"}, headers=_headers(entry)
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
    headers = _headers(entry)
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
            headers={**_headers(entry), "Content-Type": "application/octet-stream"},
            content=content,
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.status_code, resp.json()


async def delete(entry: ServerEntry, category: str, name: str) -> None:
    """DELETE /webcomfy/models/{category}/{name}."""
    try:
        resp = await _client.delete(_url(entry, category, name), headers=_headers(entry))
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
            _url(entry, category, name) + "/hash", params=params, headers=_headers(entry)
        )
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(entry, exc) from exc
    _raise_for(resp)
    return resp.status_code, resp.json()
