#!/bin/bash
# 사용법: ./setup_comfyui.sh <설치 경로>

set -e

WEBCOMFY_DIR="$(cd "$(dirname "$0")" && pwd)"
git -C "$WEBCOMFY_DIR" submodule update --init ComfyUI-Remote-Manager

cd "$1"

git clone https://github.com/Comfy-Org/ComfyUI.git

cd ComfyUI

uv sync
uv pip install -r requirements.txt

cd custom_nodes

git clone https://github.com/yaju-senpai114514/ComfyUI-Remote-Manager
git clone https://github.com/yaju-senpai114514/ComfyUI-DCW.git

# ComfyUI-Remote-Manager 의존성 (cryptography — 서명 검증). custom_nodes 에서는
# uv 가 상위의 .venv 를 탐색하지 않으므로 ComfyUI/.venv 를 명시해야 한다.
uv pip install --python ../.venv -r ComfyUI-Remote-Manager/requirements.txt

echo
echo "셋업 완료. 모델 API 인증을 켜려면 webcomfy 쪽에서 만든 공개키(.pub)를 넣으세요:"
echo "  webcomfy$ uv run scripts/gen_keypair.py <name>"
echo "  webcomfy$ scp keys/<name>.pub <이 호스트>:$(pwd)/ComfyUI-Remote-Manager/"
echo "확장 루트의 *.pub 이 전부 신뢰 키로 인식되며, 하나도 없으면 무인증으로 열립니다."
