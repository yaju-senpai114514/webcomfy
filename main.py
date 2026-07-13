"""Launch the comfy-web servers: `uv run python main.py`.

Runs two apps in one process on adjacent ports:
  - the generation UI   (server:app)  on PORT      (default 8000)
  - the read-only viewer (viewer:app) on PORT + 1  (browse output + configs)
Both share one asyncio event loop via uvicorn.Server.serve().
"""

import asyncio
import os
import signal
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))  # app modules live in src/
load_dotenv()  # HOST/PORT (and the rest) come from .env; real env vars still win


class _NoSignalServer(uvicorn.Server):
    """Two servers in one loop: each would install its own signal handlers and the
    second clobbers the first, so Ctrl+C/SIGTERM would only stop one and hang the
    process. Suppress both and install one handler that stops them together."""

    def install_signal_handlers(self) -> None:
        pass


async def _serve_both(host: str, port: int) -> None:
    main_srv = _NoSignalServer(uvicorn.Config("web.server:app", host=host, port=port, reload=False))
    view_srv = _NoSignalServer(uvicorn.Config("web.viewer:app", host=host, port=port + 1, reload=False))

    def _shutdown() -> None:
        main_srv.should_exit = True
        view_srv.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # e.g. Windows
            pass
    await asyncio.gather(main_srv.serve(), view_srv.serve())


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"comfy-web UI:     http://{host}:{port}")
    print(f"comfy-web viewer: http://{host}:{port + 1}  (read-only)")
    asyncio.run(_serve_both(host, port))


if __name__ == "__main__":
    main()
