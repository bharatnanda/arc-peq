"""Builds the app for the OS you run it on. Works with uv or plain pip.

    python3 packaging/build.py

- If `uv` is installed it is used (uv sync, then uv run).
- Otherwise a separate virtual environment, .venv-build, is created and filled with pip.
  (It is kept apart from .venv so uv and pip never fight over the same folder.)
"""
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent


def run(*cmd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=root, check=True)


if shutil.which("uv"):
    print("Using uv")
    run("uv", "sync")
    py = ["uv", "run", "python"]
else:
    print("uv not found, using pip in .venv-build")
    venv = root / ".venv-build"
    if not venv.exists():
        run(sys.executable, "-m", "venv", venv)
    py_exe = venv / ("Scripts" if sys.platform == "win32" else "bin") / ("python.exe" if sys.platform == "win32" else "python")
    run(py_exe, "-m", "pip", "install", "-q", "--upgrade", "pip")
    run(py_exe, "-m", "pip", "install", "-q", "-r", "requirements.txt", "pyinstaller>=6,<7", "pillow>=10,<13")
    py = [py_exe]

run(*py, "packaging/make_icons.py")
for d in ("build", "dist"):
    shutil.rmtree(root / d, ignore_errors=True)
run(*py, "-m", "PyInstaller", "--noconfirm", "packaging/arcpeq.spec")

if sys.platform == "darwin":
    run("codesign", "--force", "--deep", "--sign", "-", "dist/Arc PEQ.app")   # ad-hoc, NOT notarisation
    print("\nBuilt dist/Arc PEQ.app. First launch: right-click > Open (it is not notarised).")
elif sys.platform == "win32":
    print("\nBuilt dist\\ArcPEQ\\ArcPEQ.exe (copy the whole ArcPEQ folder).")
else:
    print("\nBuilt dist/ArcPEQ/ArcPEQ")
