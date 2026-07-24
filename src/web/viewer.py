"""Read-only companion app: browse generated output images and stored configs.

Served on PORT+1 alongside the main generation UI (see main.py). It only reads
from disk — no writes, no ComfyUI calls — so it stays useful even while the main
app is busy generating or the ComfyUI backend is offline.

Endpoints:
  GET /api/configs            all stored configs (full) + selected id
  GET /api/folders?dir=       one directory level (children, current path, parent)
  GET /api/images?dir=        images in one directory (newest first)
  GET /api/meta?path=         metadata embedded in one webp (config/seed/resolved), or null
  GET /img/<path>             the image bytes (static mount, sandboxed to OUTPUT_DIR)
  GET /                       the viewer SPA
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from paths import ROOT_DIR, STATIC_DIR
from store import configs, embed

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", ROOT_DIR / "output")).resolve()
IMG_EXTS = {".webp", ".png", ".jpg", ".jpeg"}

app = FastAPI(title="comfy-web viewer")


def _safe(rel: str) -> Path:
    """Resolve a client-supplied path inside OUTPUT_DIR, or 404 (no traversal out)."""
    p = (OUTPUT_DIR / rel).resolve()
    if p != OUTPUT_DIR and not p.is_relative_to(OUTPUT_DIR):
        raise HTTPException(status_code=404, detail="path out of bounds")
    return p


@app.get("/api/configs")
def api_configs() -> dict[str, Any]:
    """Every stored config (with its full GenerationConfig) + the selected id."""
    items: list[dict[str, Any]] = []
    for meta in configs.list_metas():
        try:
            sc = configs.get(meta.id)
        except (ValueError, OSError):
            continue
        items.append({
            "id": sc.id, "name": sc.name,
            "created": sc.created, "modified": sc.modified,
            "config": sc.config.model_dump(),
        })
    items.sort(key=lambda c: c["modified"], reverse=True)
    return {"configs": items, "selected": configs.get_selected()}


def _images_in(d: Path) -> list[Path]:
    return [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS]


@app.get("/api/folders")
def api_folders(dir: str = Query("")) -> dict[str, Any]:
    """Immediate child directories of `dir`; never recursively scan the tree."""
    current = _safe(dir)
    if not current.is_dir():
        raise HTTPException(status_code=404, detail="no such folder")

    folders: list[dict[str, Any]] = []
    try:
        children = list(current.iterdir())
    except OSError as exc:
        raise HTTPException(status_code=404, detail="folder is not readable") from exc

    for child in children:
        try:
            resolved = child.resolve()
            if not child.is_dir() or not resolved.is_relative_to(OUTPUT_DIR):
                continue
            entries = list(child.iterdir())
            imgs = [
                p for p in entries
                if p.is_file() and p.suffix.lower() in IMG_EXTS
            ]
            folders.append({
                "dir": child.relative_to(OUTPUT_DIR).as_posix(),
                "name": child.name,
                "count": len(imgs),
                "has_children": any(
                    p.is_dir() and p.resolve().is_relative_to(OUTPUT_DIR)
                    for p in entries
                ),
                "mtime": child.stat().st_mtime,
            })
        except OSError:
            continue

    folders.sort(key=lambda f: str(f["name"]).casefold())
    rel = current.relative_to(OUTPUT_DIR).as_posix()
    if rel == ".":
        rel = ""
    parent = None if not rel else current.parent.relative_to(OUTPUT_DIR).as_posix()
    if parent == ".":
        parent = ""
    return {
        "folders": folders,
        "root": str(OUTPUT_DIR),
        "current": rel,
        "parent": parent,
    }


@app.get("/api/images")
def api_images(dir: str = Query("")) -> dict[str, Any]:
    """Images in one output directory (newest first)."""
    d = _safe(dir)
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="no such folder")
    imgs: list[dict[str, Any]] = []
    for p in _images_in(d):
        st = p.stat()
        rel = p.relative_to(OUTPUT_DIR).as_posix()
        imgs.append({
            "path": rel, "name": p.name, "url": f"/img/{rel}",
            "size": st.st_size, "mtime": st.st_mtime,
        })
    imgs.sort(key=lambda i: i["mtime"], reverse=True)
    return {"images": imgs}


@app.get("/api/meta")
def api_meta(path: str = Query(...)) -> dict[str, Any]:
    """Reproduction metadata embedded in a webp (config/seed/resolved), or null."""
    p = _safe(path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no such image")
    meta = None
    if p.suffix.lower() == ".webp":
        try:
            meta = embed.extract_raw(p.read_bytes())
        except (ValueError, OSError):
            meta = None
    return {"meta": meta}


# --- live AFK loop (read-only mirror) --------------------------------------
# main.py runs web.server:app and web.viewer:app in one process/event loop, so
# we can reach the same AfkManager singleton and subscribe to its live events.
def _afk() -> Any:
    # Only the already-loaded module: a standalone viewer must not import the
    # generation server (fresh import = separate, always-empty AfkManager).
    mod = sys.modules.get("web.server")
    if mod is None:
        raise RuntimeError("generation server not loaded in this process")
    return mod.afk


@app.get("/api/afk/config")
def api_afk_config() -> dict[str, Any]:
    """Current AFK status + the GenerationConfig the loop is running (or null)."""
    try:
        afk = _afk()
    except Exception:  # noqa: BLE001 - viewer running standalone, no loop to mirror
        return {"available": False, "status": {"running": False}, "config": None}
    cfg = getattr(afk, "config", None)
    return {
        "available": True,
        "status": afk.status(),
        "config": cfg.model_dump() if cfg is not None else None,
    }


@app.get("/api/afk/last.webp")
def api_afk_last() -> Response:
    """The most recently saved AFK image, for an immediate preview on tab open."""
    try:
        img = _afk().last_image
    except Exception:  # noqa: BLE001
        img = None
    if img is None:
        return Response(status_code=404)
    return Response(content=img, media_type="image/webp")


@app.websocket("/ws/afk")
async def ws_afk(ws: WebSocket) -> None:
    """Relay the shared AFK loop's events to viewer clients — same frame protocol
    as the main app (a json frame, plus a trailing binary frame for image/preview)."""
    await ws.accept()
    try:
        afk = _afk()
    except Exception:  # noqa: BLE001
        await ws.send_json({"type": "afk", "running": False, "available": False})
        await ws.close()
        return
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
    return FileResponse(STATIC_DIR / "viewer.html")


# image bytes (sandboxed to OUTPUT_DIR) then the viewer's own assets
app.mount("/img", StaticFiles(directory=OUTPUT_DIR, check_dir=False), name="images")
app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
