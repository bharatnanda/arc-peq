#!/usr/bin/env bash
# Builds "Arc PEQ.app" for macOS. Run from anywhere:  ./packaging/build_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt pyinstaller pillow
python packaging/make_icons.py
rm -rf build dist
pyinstaller --noconfirm packaging/arcpeq.spec
# ad-hoc sign so Apple Silicon will launch it (this is NOT notarisation)
codesign --force --deep --sign - "dist/Arc PEQ.app"
echo
echo "Built: $(pwd)/dist/Arc PEQ.app"
echo "Drag it to Applications. First launch: right-click > Open (it is not notarised)."
