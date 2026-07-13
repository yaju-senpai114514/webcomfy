"""FastAPI backend: serve the UI, expose options/defaults, stream generation,
orchestrate a fleet of remote ComfyUI servers (registry + health + per-server
model management via the webcomfy_models custom-node API), and run an AFK
background loop that fans generation out across every enabled server.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from comfy import modelapi
from comfy.client import ComfyClient, ComfyOptions, Event
from comfy.modelapi import ModelAPIError
from gen import analyze, pipeline
from gen.models import GenerationConfig
from paths import STATIC_DIR
from store import configs, embed, localstore, storage
from store import servers as serverstore
from store.servers import ServerEntry

app = FastAPI(title="comfy-web")
configs.ensure_seeded()
serverstore.ensure_seeded()


@app.exception_handler(ModelAPIError)
async def _model_api_error(_request: Request, exc: ModelAPIError) -> JSONResponse:
    """Relay upstream model-API errors verbatim (spec §5 envelope + status)."""
    return JSONResponse(exc.as_json(), status_code=exc.status)


def _resolve_server(sid: str | None) -> ServerEntry:
    """The requested server entry, or the default (first enabled) one."""
    if sid:
        try:
            return serverstore.get(sid)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown server: {sid}")
    entry = serverstore.default_server()
    if entry is None:
        raise HTTPException(status_code=400, detail="no enabled ComfyUI server configured")
    return entry


def missing_models(cfg: GenerationConfig, options: ComfyOptions) -> list[tuple[str, str]]:
    """(kind, name) models the config needs that a server's /object_info doesn't offer."""
    missing: list[tuple[str, str]] = []
    for kind, name, available in (
        ("unet", cfg.models.unet_name, options["unets"]),
        ("clip", cfg.models.clip_name, options["clips"]),
        ("vae", cfg.models.vae_name, options["vaes"]),
        ("upscale", cfg.upscale.model_name, options["upscale_models"]),
    ):
        if name and available and name not in available:
            missing.append((kind, name))
    for lora in cfg.loras:
        if lora.name and options["loras"] and lora.name not in options["loras"]:
            missing.append(("lora", lora.name))
    return missing


# Which model-API categories can hold each config field. ComfyUI aliases some
# folders (unet ↔ diffusion_models, clip ↔ text_encoders); the remote exposes
# whichever keys its folder_paths has, so try the modern name first.
KIND_CATEGORIES: dict[str, tuple[str, ...]] = {
    "unet": ("diffusion_models", "unet"),
    "clip": ("text_encoders", "clip"),
    "vae": ("vae",),
    "upscale": ("upscale_models",),
    "lora": ("loras",),
}
PROVISION_PROGRESS_EVERY = 64 * (1 << 20)  # progress event per 64 MiB pushed

AsyncEmit = Any  # Callable[[dict], Awaitable[None]] — kept loose for closures


async def provision_missing(entry: ServerEntry, cfg: GenerationConfig, emit: AsyncEmit) -> list[str]:
    """Push models the config needs but `entry` lacks from the local store.

    The transparency layer: a generation can reference a model that only exists
    in webcomfy's local repository and it gets uploaded to the target server
    right before running. Returns human-readable "kind:name" entries that are
    still missing afterwards (nowhere to be found / push failed).
    """
    client = ComfyClient(entry.base_url)
    options = await asyncio.to_thread(client.get_options)
    missing = missing_models(cfg, options)
    if not missing:
        return []
    try:
        remote_cats = {c["category"] for c in (await modelapi.summary(entry))["categories"]}
    except ModelAPIError:
        # No models API on this server — nothing to push through.
        return [f"{kind}:{name}" for kind, name in missing]

    still: list[str] = []
    for kind, name in missing:
        local_cat = next((c for c in KIND_CATEGORIES[kind] if localstore.exists(c, name)), None)
        if local_cat is None:
            still.append(f"{kind}:{name}")
            continue
        remote_cat = next((c for c in KIND_CATEGORIES[kind] if c in remote_cats), KIND_CATEGORIES[kind][0])
        meta = localstore.file_meta(local_cat, name)
        path = localstore.path_of(local_cat, name)
        total = meta["size"]
        base_ev = {"type": "provision", "category": remote_cat, "name": name, "total": total}
        await emit({**base_ev, "state": "uploading", "bytes_done": 0})

        done = 0
        reported = 0

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal done, reported
            with open(path, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, modelapi.CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done - reported >= PROVISION_PROGRESS_EVERY:
                        reported = done
                        await emit({**base_ev, "state": "uploading", "bytes_done": done})
                    yield chunk

        try:
            await modelapi.upload(entry, remote_cat, name, chunks(), sha256=meta.get("sha256"))
            await emit({**base_ev, "state": "done", "bytes_done": total})
        except ModelAPIError as exc:
            if exc.code == "already_exists":
                # Raced with another provisioner pushing the same file — fine.
                await emit({**base_ev, "state": "done", "bytes_done": total})
            else:
                await emit({**base_ev, "state": "failed", "error": exc.message})
                still.append(f"{kind}:{name}")
    return still


# --- server registry --------------------------------------------------------
class ServerCreate(BaseModel):
    name: str
    base_url: str
    token: str = ""
    enabled: bool = True


class ServerUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    token: str | None = None
    enabled: bool | None = None


@app.get("/api/servers")
def api_servers() -> dict[str, Any]:
    default = serverstore.default_server()
    return {
        "servers": [s.model_dump() for s in serverstore.list_all()],
        "default": default.id if default else None,
    }


@app.post("/api/servers")
def api_server_create(body: ServerCreate) -> dict[str, Any]:
    entry = serverstore.create(body.name, body.base_url, token=body.token, enabled=body.enabled)
    return entry.model_dump()


@app.put("/api/servers/{sid}")
def api_server_update(sid: str, body: ServerUpdate) -> dict[str, Any]:
    try:
        entry = serverstore.update(
            sid, name=body.name, base_url=body.base_url, token=body.token, enabled=body.enabled
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    return entry.model_dump()


@app.delete("/api/servers/{sid}")
def api_server_delete(sid: str) -> dict[str, bool]:
    try:
        serverstore.delete(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    return {"ok": True}


@app.get("/api/servers/{sid}/health")
async def api_server_health(sid: str) -> dict[str, Any]:
    """Liveness of one ComfyUI server: queue depth + models-API availability."""
    entry = _resolve_server(sid)
    out: dict[str, Any] = {
        "id": entry.id,
        "ok": False,
        "queue_running": None,
        "queue_pending": None,
        "models_api": False,
        "error": None,
    }
    client = ComfyClient(entry.base_url)
    try:
        running, pending = await asyncio.to_thread(client.get_queue)
        out.update(ok=True, queue_running=running, queue_pending=pending)
    except Exception as exc:  # noqa: BLE001 - report, don't crash health checks
        out["error"] = str(exc)
        return out
    try:
        await modelapi.summary(entry)
        out["models_api"] = True
    except ModelAPIError as exc:
        out["models_error"] = exc.message
    return out


@app.get("/api/options")
def api_options(server_id: str | None = None) -> dict[str, Any]:
    """Dropdown choices from one ComfyUI server (models/loras/samplers)."""
    entry = _resolve_server(server_id)
    options: ComfyOptions | dict[str, str]
    try:
        options = ComfyClient(entry.base_url).get_options()
    except Exception as exc:  # noqa: BLE001 - surface a friendly error to the UI
        options = {"error": str(exc)}
    return {
        "options": options,
        "local": localstore.options_map(),  # 로컬 저장소 파일 — 선택 시 자동 전송 대상
        "base_url": entry.base_url,
        "server": {"id": entry.id, "name": entry.name},
    }


# --- named-config store ----------------------------------------------------
class ConfigCreate(BaseModel):
    name: str
    config: GenerationConfig


class ConfigUpdate(BaseModel):
    name: str | None = None
    config: GenerationConfig | None = None


class SelectBody(BaseModel):
    id: str


@app.get("/api/configs")
def api_configs() -> dict[str, Any]:
    """All config metadata + the currently-selected id and its full config."""
    selected = configs.get_selected()
    current = configs.get(selected).config.model_dump() if selected else None
    return {
        "configs": [m.model_dump() for m in configs.list_metas()],
        "selected": selected,
        "current": current,
    }


@app.post("/api/configs")
def api_config_create(body: ConfigCreate) -> dict[str, Any]:
    sc = configs.create(body.name, body.config)
    configs.set_selected(sc.id)
    return {"id": sc.id, "name": sc.name, "created": sc.created, "modified": sc.modified}


@app.post("/api/configs/select")
def api_config_select(body: SelectBody) -> dict[str, bool]:
    configs.set_selected(body.id)
    return {"ok": True}


@app.get("/api/configs/{cid}")
def api_config_get(cid: str) -> dict[str, Any]:
    try:
        return configs.get(cid).model_dump()
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="config not found")


@app.put("/api/configs/{cid}")
def api_config_update(cid: str, body: ConfigUpdate) -> dict[str, Any]:
    try:
        sc = configs.update(cid, config=body.config, name=body.name)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="config not found")
    return {"id": sc.id, "name": sc.name, "created": sc.created, "modified": sc.modified}


@app.post("/api/configs/{cid}/duplicate")
def api_config_duplicate(cid: str) -> dict[str, Any]:
    try:
        sc = configs.duplicate(cid)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="config not found")
    configs.set_selected(sc.id)
    return {"id": sc.id, "name": sc.name, "created": sc.created, "modified": sc.modified}


@app.delete("/api/configs/{cid}")
def api_config_delete(cid: str) -> dict[str, Any]:
    new_selected = configs.delete(cid)
    return {"ok": True, "selected": new_selected}


@app.post("/api/analyze")
def api_analyze(cfg: GenerationConfig) -> dict[str, Any]:
    """Statically analyse a config's wildcard tree for dead branches and tokens
    that can never be substituted. Returns a list of issues (empty = clean)."""
    issues = analyze.analyze_spec(cfg.positive)
    return {"issues": [i.as_dict() for i in issues]}


@app.post("/api/reproduce")
async def api_reproduce(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Recover the config + master seed embedded in an uploaded webp."""
    data = await file.read()
    result = embed.extract(data)
    if result is None:
        raise HTTPException(status_code=400, detail="no embedded reproduction metadata")
    cfg, master_seed = result
    return {"config": cfg.model_dump(), "master_seed": master_seed}


# --------------------------------------------------------------------------
# Model management proxy — the browser talks to webcomfy, webcomfy talks to
# each ComfyUI's /webcomfy/models API (auth token + CORS stay server-side).
# The reserved id "local" targets webcomfy's own model repository (localstore),
# so the same endpoints/UI manage local files and local↔remote sync.
# --------------------------------------------------------------------------
LOCAL_ID = "local"


class CopyBody(BaseModel):
    src_id: str
    dst_id: str
    category: str
    name: str
    replace: bool = False


class CopyJob:
    """One model transfer streamed through webcomfy (server↔server or local↔server)."""

    def __init__(
        self, src: dict[str, str], dst: dict[str, str], category: str, name: str
    ) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.src = src
        self.dst = dst
        self.category = category
        self.name = name
        self.state = "running"  # running | done | error
        self.bytes_done = 0
        self.total: int | None = None
        self.error: str | None = None
        self.warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "src": self.src, "dst": self.dst,
            "category": self.category, "name": self.name, "state": self.state,
            "bytes_done": self.bytes_done, "total": self.total,
            "error": self.error, "warning": self.warning,
        }


COPY_JOBS: dict[str, CopyJob] = {}
MAX_JOBS = 50


async def _run_copy(
    job: CopyJob, src: ServerEntry | None, dst: ServerEntry | None, replace: bool
) -> None:
    """Stream one file src→dst. `None` on either side means the local store."""
    try:
        if src is None:
            meta = localstore.file_meta(job.category, job.name)
        else:
            meta = await modelapi.file_meta(src, job.category, job.name)
        job.total = meta.get("size")
        sha = meta.get("sha256")  # verify end-to-end when the source knows its hash

        async def local_body() -> AsyncIterator[bytes]:
            path = localstore.path_of(job.category, job.name)
            with open(path, "rb") as f:
                while chunk := await asyncio.to_thread(f.read, modelapi.CHUNK):
                    job.bytes_done += len(chunk)
                    yield chunk

        if dst is None:
            assert src is not None  # local→local is rejected at the endpoint
            with localstore.Uploader(job.category, job.name, replace=replace) as up:
                async with modelapi.download(src, job.category, job.name) as resp:
                    async for chunk in resp.aiter_bytes(modelapi.CHUNK):
                        job.bytes_done += len(chunk)
                        up.write(chunk)
                _, out = up.finish(sha)
        elif src is None:
            _, out = await modelapi.upload(
                dst, job.category, job.name, local_body(), replace=replace, sha256=sha
            )
        else:
            async with modelapi.download(src, job.category, job.name) as resp:

                async def body() -> AsyncIterator[bytes]:
                    async for chunk in resp.aiter_bytes(modelapi.CHUNK):
                        job.bytes_done += len(chunk)
                        yield chunk

                _, out = await modelapi.upload(
                    dst, job.category, job.name, body(), replace=replace, sha256=sha
                )
        job.warning = out.get("warning")
        job.state = "done"
    except Exception as exc:  # noqa: BLE001 - job state carries the failure
        job.state = "error"
        job.error = str(exc)


def _copy_side(sid: str) -> tuple[ServerEntry | None, dict[str, str]]:
    """(entry-or-local, display info) for one side of a copy."""
    if sid == LOCAL_ID:
        return None, {"id": LOCAL_ID, "name": "로컬 저장소"}
    entry = _resolve_server(sid)
    return entry, {"id": entry.id, "name": entry.name}


@app.post("/api/models/copy")
async def api_model_copy(body: CopyBody) -> dict[str, Any]:
    src, src_info = _copy_side(body.src_id)
    dst, dst_info = _copy_side(body.dst_id)
    if body.src_id == body.dst_id or (src and dst and src.id == dst.id):
        raise HTTPException(status_code=400, detail="src and dst are the same")
    job = CopyJob(src_info, dst_info, body.category, body.name)
    while len(COPY_JOBS) >= MAX_JOBS:
        # evict oldest finished job; give up if everything is still running
        done = next((k for k, j in COPY_JOBS.items() if j.state != "running"), None)
        if done is None:
            break
        COPY_JOBS.pop(done)
    COPY_JOBS[job.id] = job
    asyncio.create_task(_run_copy(job, src, dst, body.replace))
    return {"job": job.as_dict()}


@app.get("/api/model_jobs")
def api_model_jobs() -> dict[str, Any]:
    return {"jobs": [j.as_dict() for j in COPY_JOBS.values()]}


@app.get("/api/model_jobs/{jid}")
def api_model_job(jid: str) -> dict[str, Any]:
    job = COPY_JOBS.get(jid)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.as_dict()


@app.get("/api/models/{sid}")
async def api_models_summary(sid: str) -> dict[str, Any]:
    if sid == LOCAL_ID:
        return await asyncio.to_thread(localstore.summary)
    return await modelapi.summary(_resolve_server(sid))


@app.get("/api/models/{sid}/{category}")
async def api_models_list(sid: str, category: str) -> dict[str, Any]:
    if sid == LOCAL_ID:
        return await asyncio.to_thread(localstore.list_files, category)
    return await modelapi.list_files(_resolve_server(sid), category)


# NOTE: route order matters — register the /hash POST before the greedy
# {name:path} POST so ".../foo.safetensors/hash" hits the hash endpoint.
@app.post("/api/models/{sid}/{category}/{name:path}/hash")
async def api_model_hash(sid: str, category: str, name: str, force: bool = False) -> JSONResponse:
    if sid == LOCAL_ID:
        status, data = localstore.trigger_hash(category, name, force=force)
    else:
        status, data = await modelapi.trigger_hash(_resolve_server(sid), category, name, force=force)
    return JSONResponse(data, status_code=status)


@app.get("/api/models/{sid}/{category}/{name:path}")
async def api_model_get(
    sid: str, category: str, name: str, request: Request, meta: int = 0
) -> Response:
    if sid == LOCAL_ID:
        if meta:
            return JSONResponse(localstore.file_meta(category, name))
        path = localstore.path_of(category, name)
        return FileResponse(  # Starlette FileResponse handles Range natively
            path, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )
    entry = _resolve_server(sid)
    if meta:
        return JSONResponse(await modelapi.file_meta(entry, category, name))

    # Streaming download pass-through (Range forwarded for resumable downloads).
    cm = modelapi.download(entry, category, name, request.headers.get("Range"))
    resp = await cm.__aenter__()  # raises ModelAPIError for 4xx before streaming

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes(modelapi.CHUNK):
                yield chunk
        finally:
            await cm.__aexit__(None, None, None)

    headers = {
        k: v
        for k in ("Content-Length", "Content-Range", "Accept-Ranges")
        if (v := resp.headers.get(k)) is not None
    }
    headers["Content-Disposition"] = f'attachment; filename="{Path(name).name}"'
    return StreamingResponse(
        body(), status_code=resp.status_code,
        media_type="application/octet-stream", headers=headers,
    )


@app.post("/api/models/{sid}/{category}/{name:path}")
async def api_model_upload(
    sid: str,
    category: str,
    name: str,
    request: Request,
    replace: bool = False,
    sha256: str | None = None,
) -> JSONResponse:
    """Stream the browser's raw-binary body straight through to the ComfyUI server."""
    if sid == LOCAL_ID:
        with localstore.Uploader(category, name, replace=replace) as up:
            async for chunk in request.stream():
                up.write(chunk)
            status, data = up.finish(sha256)
        return JSONResponse(data, status_code=status)
    entry = _resolve_server(sid)
    status, data = await modelapi.upload(
        entry, category, name, request.stream(), replace=replace, sha256=sha256
    )
    return JSONResponse(data, status_code=status)


@app.delete("/api/models/{sid}/{category}/{name:path}")
async def api_model_delete(sid: str, category: str, name: str) -> Response:
    if sid == LOCAL_ID:
        localstore.delete(category, name)
    else:
        await modelapi.delete(_resolve_server(sid), category, name)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Interactive generation (one server per run, chosen in the UI)
# --------------------------------------------------------------------------
@app.websocket("/ws/generate")
async def ws_generate(ws: WebSocket) -> None:
    await ws.accept()
    try:
        raw = await ws.receive_json()
    except WebSocketDisconnect:
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Event] = asyncio.Queue()

    def on_event(ev: Event) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    # Optional master-seed override (for reproduction); else pick a fresh one.
    master_seed = raw.pop("master_seed", None) if isinstance(raw, dict) else None
    server_id = raw.pop("server_id", None) if isinstance(raw, dict) else None
    try:
        entry = _resolve_server(server_id)
    except HTTPException as exc:
        await ws.send_json({"type": "error", "data": {"message": str(exc.detail)}})
        await ws.close()
        return
    try:
        cfg = GenerationConfig.model_validate(raw)
        master_seed = int(master_seed) if master_seed not in (None, "") else pipeline.new_master_seed()
        workflow, labels, info = pipeline.prepare(cfg, master_seed)
    except ValidationError as exc:
        await ws.send_json({
            "type": "error",
            "data": {"message": "invalid config", "errors": json.loads(exc.json())},
        })
        await ws.close()
        return
    except Exception as exc:  # noqa: BLE001
        await ws.send_json({"type": "error", "data": {"message": f"build failed: {exc}"}})
        await ws.close()
        return

    # Tell the client what the wildcards actually resolved to this run.
    await ws.send_json({
        "type": "resolved",
        "positive": info["positive"],
        "loras": info["loras"],
        "seed1": info["seed1"],
        "master_seed": master_seed,
        "server": entry.name,
        "server_id": entry.id,
    })

    # Transparent provisioning: push models this config needs but the target
    # server lacks from the local store, streaming progress to the browser.
    async def emit_provision(ev: dict[str, Any]) -> None:
        await ws.send_json({**ev, "server": entry.name, "server_id": entry.id})

    try:
        still = await provision_missing(entry, cfg, emit_provision)
    except Exception as exc:  # noqa: BLE001 - options/summary fetch failed
        await ws.send_json({"type": "error", "data": {"message": f"pre-flight failed: {exc}"}})
        await ws.close()
        return
    if still:
        await ws.send_json({
            "type": "error",
            "data": {"message": "missing models (서버·로컬 저장소 모두 없음): " + ", ".join(still)},
        })
        await ws.close()
        return

    client = ComfyClient(entry.base_url)

    def _runner() -> None:
        # Surface run() failures (e.g. /prompt rejected) as an error event —
        # otherwise the queue never yields "done" and this handler waits forever.
        try:
            client.run(workflow, labels, on_event)
        except Exception as exc:  # noqa: BLE001
            on_event({"type": "error", "data": {"message": str(exc)}})

    task = asyncio.create_task(asyncio.to_thread(_runner))
    final_png: bytes | None = None

    try:
        while True:
            ev = await queue.get()
            # Image frames: send a small JSON header, then the raw bytes.
            # Indexing on ev["type"] narrows the TypedDict union per branch.
            if ev["type"] == "image":
                if ev["label"] == "final":
                    final_png = ev["data"]
                await ws.send_json({"type": "image", "label": ev["label"]})
                await ws.send_bytes(ev["data"])
            elif ev["type"] == "preview":
                await ws.send_json({"type": "preview"})
                await ws.send_bytes(ev["data"])
            else:
                if ev["type"] == "done" and cfg.save.enabled and final_png is not None:
                    try:
                        meta = embed.build_meta(cfg, master_seed, info)
                        path = await asyncio.to_thread(
                            storage.save_image, final_png, cfg.save, info["seed1"], 0, meta
                        )
                        await ws.send_json({"type": "saved", "path": str(path)})
                    except Exception as exc:  # noqa: BLE001
                        await ws.send_json({"type": "saved", "error": str(exc)})
                await ws.send_json(ev)
                if ev["type"] in ("done", "error"):
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await task
        try:
            await ws.close()
        except RuntimeError:
            pass


# --------------------------------------------------------------------------
# AFK background loop: fan generation out across every selected server, each
# rolling fresh wildcards + seeds, until stopped or the shared target is hit.
# --------------------------------------------------------------------------
class AfkWorker:
    """Per-server state of the AFK fleet."""

    def __init__(self, entry: ServerEntry) -> None:
        self.entry = entry
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.count = 0
        self.last_path: str | None = None
        self.last_seed: int | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "id": self.entry.id,
            "name": self.entry.name,
            "running": self.running,
            "count": self.count,
            "last_path": self.last_path,
            "last_seed": self.last_seed,
            "last_error": self.last_error,
        }


class AfkManager:
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self) -> None:
        self.workers: dict[str, AfkWorker] = {}
        self.stop_event = asyncio.Event()
        self.target = 0
        self.saved = 0
        self.next_index = 0
        self.freed: list[int] = []  # indices reclaimed from failed generations
        self.last_path: str | None = None
        self.last_seed: int | None = None
        self.last_error: str | None = None
        self.last_image: bytes | None = None
        self.config: GenerationConfig | None = None
        # Live-view subscribers (browsers on /ws/afk) — each gets every event.
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def running(self) -> bool:
        return any(w.running for w in self.workers.values())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "count": self.saved,
            "target": self.target,
            "last_path": self.last_path,
            "last_seed": self.last_seed,
            "last_error": self.last_error,
            "has_image": self.last_image is not None,
            "workers": [w.status() for w in self.workers.values()],
        }

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        self.subscribers.discard(q)

    def _emit(self, ev: dict[str, Any]) -> None:
        """Fan an event out to every live subscriber. Runs in the event loop."""
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def _emit_status(self) -> None:
        self._emit({"type": "afk", **self.status()})

    def _claim(self) -> int | None:
        """Reserve the next global image index, or None once the target is met."""
        if self.freed:
            return self.freed.pop()
        if self.target and self.next_index >= self.target:
            return None
        i = self.next_index
        self.next_index += 1
        return i

    def start(self, cfg: GenerationConfig, entries: list[ServerEntry]) -> None:
        """Must be called from within the event loop (async endpoint)."""
        if self.running:
            raise RuntimeError("AFK loop already running")
        self.stop_event.clear()
        self.target = cfg.save.afk_count
        self.saved = 0
        self.next_index = 0
        self.freed = []
        self.last_path = self.last_error = None
        self.last_seed = None
        self.last_image = None
        self.config = cfg
        self.workers = {e.id: AfkWorker(e) for e in entries}
        for worker in self.workers.values():
            worker.running = True  # mark before scheduling so status() is immediately true
            worker.task = asyncio.create_task(self._worker_loop(worker, cfg))
        self._emit_status()

    def stop(self) -> None:
        self.stop_event.set()

    async def _worker_loop(self, worker: AfkWorker, cfg: GenerationConfig) -> None:
        loop = asyncio.get_running_loop()
        entry = worker.entry
        client = ComfyClient(entry.base_url)
        tag = {"server_id": entry.id, "server": entry.name}
        consecutive_errors = 0
        async def emit_provision(ev: dict[str, Any]) -> None:
            self._emit({**ev, **tag})

        try:
            # Pre-flight: push missing models from the local store; refuse to run
            # only if something is missing both on the server and locally.
            try:
                still = await provision_missing(entry, cfg, emit_provision)
                if still:
                    raise RuntimeError(
                        "missing models (서버·로컬 저장소 모두 없음): " + ", ".join(still)
                    )
            except Exception as exc:  # noqa: BLE001
                worker.last_error = str(exc)
                self.last_error = f"{entry.name}: {exc}"
                return

            while not self.stop_event.is_set():
                index = self._claim()
                if index is None:
                    break
                try:
                    master_seed = pipeline.new_master_seed()
                    workflow, labels, info = pipeline.prepare(cfg, master_seed)
                    self._emit({
                        "type": "resolved", "positive": info["positive"],
                        "loras": info["loras"], "seed1": info["seed1"],
                        "master_seed": master_seed, **tag,
                    })

                    # Stream ComfyUI events to subscribers while collecting outputs.
                    images: dict[str, bytes] = {}
                    err: dict[str, Any] = {}

                    def sink(ev: Event) -> None:
                        if ev["type"] == "image":
                            images[ev["label"]] = ev["data"]
                        elif ev["type"] == "error":
                            err.update(ev["data"])
                        loop.call_soon_threadsafe(self._emit, {**ev, **tag})

                    await asyncio.to_thread(client.run, workflow, labels, sink)
                    if err:
                        raise RuntimeError(f"ComfyUI execution error: {err}")
                    final_png = images.get("final")
                    if final_png is None:
                        raise RuntimeError("no final image returned")

                    meta = embed.build_meta(cfg, master_seed, info)
                    path = await asyncio.to_thread(
                        storage.save_image, final_png, cfg.save, info["seed1"], index, meta
                    )
                    self.last_image = await asyncio.to_thread(path.read_bytes)
                    worker.last_path = self.last_path = str(path)
                    worker.last_seed = self.last_seed = info["seed1"]
                    worker.last_error = None
                    worker.count += 1
                    self.saved += 1
                    consecutive_errors = 0
                    self._emit({"type": "saved", "path": str(path), **tag})
                    self._emit_status()
                except Exception as exc:  # noqa: BLE001
                    self.freed.append(index)  # let another worker retake this slot
                    worker.last_error = str(exc)
                    self.last_error = f"{entry.name}: {exc}"
                    consecutive_errors += 1
                    self._emit_status()
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        break
                    await asyncio.sleep(2.0)
        finally:
            worker.running = False
            self._emit_status()


afk = AfkManager()


class AfkStartBody(BaseModel):
    config: GenerationConfig
    server_ids: list[str] | None = None  # None = every enabled server


@app.post("/api/afk/start")
async def afk_start(body: AfkStartBody) -> JSONResponse:
    if body.server_ids:
        try:
            entries = [serverstore.get(sid) for sid in body.server_ids]
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": f"unknown server: {exc}"}, status_code=404)
    else:
        entries = serverstore.list_enabled()
    if not entries:
        return JSONResponse({"ok": False, "error": "no enabled ComfyUI server"}, status_code=400)
    try:
        afk.start(body.config, entries)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, **afk.status()})


@app.post("/api/afk/stop")
async def afk_stop() -> dict[str, Any]:
    afk.stop()
    return {"ok": True, **afk.status()}


@app.get("/api/afk/status")
async def afk_status() -> dict[str, Any]:
    return afk.status()


@app.get("/api/afk/last.webp")
async def afk_last() -> Response:
    if afk.last_image is None:
        return Response(status_code=404)
    return Response(content=afk.last_image, media_type="image/webp")


@app.websocket("/ws/afk")
async def ws_afk(ws: WebSocket) -> None:
    """Live AFK stream: relays the background loop's events (resolved/progress/
    image/saved/afk-status) to the browser, same frame protocol as /ws/generate.
    Every event carries server_id/server so the client can render per-server."""
    await ws.accept()
    queue = afk.subscribe()
    try:
        await ws.send_json({"type": "afk", **afk.status()})
        while True:
            ev = await queue.get()
            if ev.get("type") in ("image", "preview") and "data" in ev:
                meta = {k: v for k, v in ev.items() if k != "data"}
                await ws.send_json(meta)
                await ws.send_bytes(ev["data"])
            else:
                await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    finally:
        afk.unsubscribe(queue)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/models")
def models_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "models.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
