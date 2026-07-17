#!/bin/bash
# 사용법: ./setup_comfyui.sh <설치 경로>
# ComfyUI 백엔드 셋업. ComfyUI-Remote-Manager는 GitHub 재클론 대신
# webcomfy 레포의 서브모듈 체크아웃(핀된 버전)에서 설치한다.
set -e

WEBCOMFY_DIR="$(cd "$(dirname "$0")" && pwd)"
git -C "$WEBCOMFY_DIR" submodule update --init ComfyUI-Remote-Manager

cd "$1"

git clone https://github.com/Comfy-Org/ComfyUI.git

cd ComfyUI

uv sync
uv pip install -r requirements.txt

cd custom_nodes

git clone "$WEBCOMFY_DIR/ComfyUI-Remote-Manager" ComfyUI-Remote-Manager
git clone https://github.com/yaju-senpai114514/ComfyUI-DCW.git
