#!/bin/bash

cd $1

git clone https://github.com/Comfy-Org/ComfyUI.git

cd ComfyUI

uv sync
uv pip install -r requirements.txt

cd custom_nodes

git clone https://github.com/yaju-senpai114514/ComfyUI-Remote-Manager.git
git clone https://github.com/yaju-senpai114514/ComfyUI-DCW.git