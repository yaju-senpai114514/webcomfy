"""FastAPI backend: serve the UI, expose options/defaults, stream generation,
and run an AFK background loop that keeps generating + saving to disk."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

import analyze
import comfy
import configs
import embed
import pipeline
import storage
from comfy import ComfyOptions, Event
from models import GenerationConfig

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="comfy-web")
configs.ensure_seeded()


@app.get("/api/options")
def api_options() -> dict[str, Any]:
    """Dropdown choices from ComfyUI (models/loras/samplers) + ComfyUI address."""
    options: ComfyOptions | dict[str, str]
    try:
        options = comfy.get_options()
    except Exception as exc:  # noqa: BLE001 - surface a friendly error to the UI
        options = {"error": str(exc)}
    return {"options": options, "base_url": comfy.COMFY_BASE_URL}


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
    })

    task = asyncio.create_task(asyncio.to_thread(comfy.run, workflow, labels, on_event))
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
# AFK background loop: keep generating (re-rolling wildcards + seeds) and
# saving each final image to disk until stopped.
# --------------------------------------------------------------------------
class AfkManager:
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.stop_event = asyncio.Event()
        self.running = False
        self.count = 0
        self.target = 0
        self.last_path: str | None = None
        self.last_error: str | None = None
        self.last_seed: int | None = None
        self.last_image: bytes | None = None
        self.config: GenerationConfig | None = None  # the config the loop is running
        # Live-view subscribers (browsers on /ws/afk) — each gets every event.
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "count": self.count,
            "target": self.target,
            "last_path": self.last_path,
            "last_seed": self.last_seed,
            "last_error": self.last_error,
            "has_image": self.last_image is not None,
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

    def start(self, cfg: GenerationConfig) -> None:
        """Must be called from within the event loop (async endpoint)."""
        if self.running:
            raise RuntimeError("AFK loop already running")
        self.stop_event.clear()
        self.count = 0
        self.target = cfg.save.afk_count
        self.last_path = self.last_error = None
        self.last_seed = None
        self.last_image = None
        self.config = cfg
        self.task = asyncio.create_task(self._loop(cfg))
        self.running = True

    def stop(self) -> None:
        self.stop_event.set()

    async def _loop(self, cfg: GenerationConfig) -> None:
        loop = asyncio.get_running_loop()
        consecutive_errors = 0
        self._emit_status()
        try:
            while not self.stop_event.is_set():
                if self.target and self.count >= self.target:
                    break
                try:
                    master_seed = pipeline.new_master_seed()
                    workflow, labels, info = pipeline.prepare(cfg, master_seed)
                    self._emit({
                        "type": "resolved", "positive": info["positive"],
                        "loras": info["loras"], "seed1": info["seed1"],
                        "master_seed": master_seed,
                    })

                    # Stream ComfyUI events to subscribers while collecting outputs.
                    images: dict[str, bytes] = {}
                    err: dict[str, Any] = {}

                    def sink(ev: Event) -> None:
                        if ev["type"] == "image":
                            images[ev["label"]] = ev["data"]
                        elif ev["type"] == "error":
                            err.update(ev["data"])
                        loop.call_soon_threadsafe(self._emit, dict(ev))

                    await asyncio.to_thread(comfy.run, workflow, labels, sink)
                    if err:
                        raise RuntimeError(f"ComfyUI execution error: {err}")
                    final_png = images.get("final")
                    if final_png is None:
                        raise RuntimeError("no final image returned")

                    meta = embed.build_meta(cfg, master_seed, info)
                    path = await asyncio.to_thread(
                        storage.save_image, final_png, cfg.save, info["seed1"], self.count, meta
                    )
                    self.last_image = await asyncio.to_thread(path.read_bytes)
                    self.last_path = str(path)
                    self.last_seed = info["seed1"]
                    self.last_error = None
                    self.count += 1
                    consecutive_errors = 0
                    self._emit({"type": "saved", "path": str(path)})
                    self._emit_status()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = str(exc)
                    consecutive_errors += 1
                    self._emit_status()
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        break
                    await asyncio.sleep(2.0)
        finally:
            self.running = False
            self._emit_status()


afk = AfkManager()


@app.post("/api/afk/start")
async def afk_start(cfg: GenerationConfig) -> JSONResponse:
    try:
        afk.start(cfg)
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
    image/saved/afk-status) to the browser, same frame protocol as /ws/generate."""
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


@app.get("/v2")
def index_v2() -> FileResponse:
    return FileResponse(STATIC_DIR / "v2.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
