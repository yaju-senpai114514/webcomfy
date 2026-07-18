#!/usr/bin/env python3
"""Generate an Ed25519 keypair for webcomfy → ComfyUI-Remote-Manager auth.

Usage:
    uv run scripts/gen_keypair.py <name>

Writes ``keys/<name>.key`` (private, 0600) and ``keys/<name>.pub`` (public).
Copy the ``.pub`` into each ComfyUI's ``custom_nodes/ComfyUI-Remote-Manager/``
(next to ``__init__.py``) — every ``*.pub`` there is a trusted key. In webcomfy,
set the server entry's ``key_name`` to ``<name>`` so requests get signed.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
KEYS_DIR = ROOT_DIR / "keys"

# The name doubles as a filename and an HTTP header value.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ed25519 키페어 생성 (keys/<name>.key|.pub)")
    ap.add_argument("name", help="키페어 이름 (영숫자, '.', '_', '-')")
    name = ap.parse_args().name

    if not NAME_RE.match(name):
        print(f"오류: 잘못된 이름 '{name}' — 영숫자로 시작, 영숫자/'.'/'_'/'-'만 허용", file=sys.stderr)
        return 1

    key_path = KEYS_DIR / f"{name}.key"
    pub_path = KEYS_DIR / f"{name}.pub"
    for p in (key_path, pub_path):
        if p.exists():
            print(f"오류: {p} 가 이미 존재합니다 — 다른 이름을 쓰거나 먼저 삭제하세요", file=sys.stderr)
            return 1

    KEYS_DIR.mkdir(exist_ok=True)
    private = Ed25519PrivateKey.generate()

    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    pub_path.write_bytes(
        private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )

    print(f"생성됨: {key_path} (개인키, 0600 — 절대 배포 금지)")
    print(f"생성됨: {pub_path} (공개키)")
    print()
    print("다음 단계:")
    print(f"  1. {pub_path.name} 을 각 ComfyUI의 custom_nodes/ComfyUI-Remote-Manager/ 에 복사")
    print(f"     예: scp {pub_path} <host>:<comfyui>/custom_nodes/ComfyUI-Remote-Manager/")
    print(f"  2. webcomfy UI의 서버 등록/수정에서 키 이름에 '{name}' 입력")
    return 0


if __name__ == "__main__":
    sys.exit(main())
