#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt pyinstaller pillow
python packaging/make_icons.py
rm -rf build dist
pyinstaller --noconfirm packaging/arcpeq.spec
echo "Built: $(pwd)/dist/ArcPEQ/ArcPEQ"
