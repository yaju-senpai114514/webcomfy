"""Thin ComfyUI client: discover options, queue prompts, stream results.

`ComfyClient` targets one ComfyUI instance; webcomfy holds one per registered
server (see servers.py). The module-level COMFY_BASE_URL only seeds the server
registry on first run and backs the repro.py CLI default.
"""

from __future__ import annotations

import json
import os
import struct
import uuid
from typing import Any, Callable, Literal, Mapping, TypedDict, Union

import requests
import websocket  # from the `websocket-client` package
from dotenv import load_dotenv

load_dotenv()
COMFY_BASE_URL = os.environ.get("COMFY_BASE_URL", "http://localhost:58188")


class ComfyOptions(TypedDict):
    """Available choices discovered from /object_info."""

    loras: list[str]
    unets: list[str]
    clips: list[str]
    vaes: list[str]
    upscale_models: list[str]
    samplers: list[str]
    schedulers: list[str]


# --- generation event stream (tagged union, discriminated on "type") -------
class QueuedEvent(TypedDict):
    type: Literal["queued"]
    prompt_id: str


class NodeEvent(TypedDict):
    type: Literal["node"]
    node: str


class ProgressEvent(TypedDict):
    type: Literal["progress"]
    value: int
    max: int
    node: str | None


class ImageEvent(TypedDict):
    type: Literal["image"]
    label: str
    data: bytes


class PreviewEvent(TypedDict):
    type: Literal["preview"]
    data: bytes


class ErrorEvent(TypedDict):
    type: Literal["error"]
    data: dict[str, Any]


class DoneEvent(TypedDict):
    type: Literal["done"]


Event = Union[
    QueuedEvent, NodeEvent, ProgressEvent, ImageEvent, PreviewEvent, ErrorEvent, DoneEvent
]
EventSink = Callable[[Event], None]


def _combo_options(spec: Any) -> list[str]:
    """Extract a combo's options across ComfyUI schema variants.

    Old style: ["a.safetensors", "b.safetensors", ...]
    New style: ["COMBO", {"options": [...]}]
    """
    if isinstance(spec, list) and spec:
        head = spec[0]
        if isinstance(head, list):
            return head
        if head == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return spec[1].get("options", [])
    return []


class ComfyClient:
    """Synchronous client for one ComfyUI server (generation endpoints)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get_options(self) -> ComfyOptions:
        """Fetch dropdown options (models, loras, samplers, ...) from /object_info."""
        info: Any = requests.get(f"{self.base_url}/object_info", timeout=30).json()

        def field(node: str, name: str) -> list[str]:
            try:
                return _combo_options(info[node]["input"]["required"][name])
            except KeyError:
                return []

        return {
            "loras": field("LoraLoaderModelOnly", "lora_name"),
            "unets": field("UNETLoader", "unet_name"),
            "clips": field("CLIPLoader", "clip_name"),
            "vaes": field("VAELoader", "vae_name"),
            "upscale_models": field("UpscaleModelLoader", "model_name"),
            "samplers": field("KSampler", "sampler_name"),
            "schedulers": field("KSampler", "scheduler"),
        }

    def get_queue(self) -> tuple[int, int]:
        """(running, pending) prompt counts from /queue — a cheap liveness probe."""
        q: Any = requests.get(f"{self.base_url}/queue", timeout=10).json()
        return len(q.get("queue_running", [])), len(q.get("queue_pending", []))

    def queue_prompt(self, workflow: Mapping[str, Any], client_id: str) -> str:
        """POST the workflow to /prompt and return the prompt_id."""
        resp = requests.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
        )
        if not resp.ok:
            raise RuntimeError(f"/prompt rejected ({resp.status_code}): {resp.text}")
        return resp.json()["prompt_id"]

    def run(
        self, workflow: Mapping[str, Any], labels: Mapping[str, str], on_event: EventSink
    ) -> None:
        """Queue `workflow` and drive its websocket, reporting progress via on_event.

        Events emitted (dicts):
          {"type": "queued",   "prompt_id": str}
          {"type": "node",     "node": str}
          {"type": "progress", "value": int, "max": int, "node": str|None}
          {"type": "image",    "label": str, "data": bytes}   # final outputs
          {"type": "preview",  "data": bytes}                  # live sampling preview
          {"type": "error",    "data": dict}
          {"type": "done"}
        """
        client_id = str(uuid.uuid4())
        prompt_id = self.queue_prompt(workflow, client_id)
        on_event({"type": "queued", "prompt_id": prompt_id})

        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws = websocket.WebSocket()
        ws.connect(f"{ws_url}/ws?clientId={client_id}")

        current_node = ""
        try:
            while True:
                out = ws.recv()
                if isinstance(out, str):
                    msg = json.loads(out)
                    mtype = msg.get("type")
                    data = msg.get("data", {}) or {}
                    if data.get("prompt_id") not in (None, prompt_id):
                        continue
                    if mtype == "executing":
                        node = data.get("node")
                        if node is None:
                            break  # whole prompt finished
                        current_node = node
                        on_event({"type": "node", "node": node})
                    elif mtype == "progress":
                        on_event({
                            "type": "progress",
                            "value": data.get("value", 0),
                            "max": data.get("max", 0),
                            "node": data.get("node"),
                        })
                    elif mtype == "execution_error":
                        on_event({"type": "error", "data": data})
                        break
                else:
                    # Binary frame: first 8 bytes are header (event type + image format).
                    struct.unpack(">II", out[:8])
                    payload = out[8:]
                    if current_node in labels:
                        on_event({"type": "image", "label": labels[current_node], "data": payload})
                    else:
                        on_event({"type": "preview", "data": payload})
        finally:
            ws.close()

        on_event({"type": "done"})
