"""Repo-root anchored paths shared by modules that read/write next to the app.

Modules live under src/<package>/, so `Path(__file__).parent`-style anchoring
would scatter runtime files (configs/, servers.json, output/, …) into src.
Everything that persists to disk resolves against ROOT_DIR instead.
"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "static"
