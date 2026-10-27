@echo off
rem Builds dist\ArcPEQ\ArcPEQ.exe for Windows. Run from a normal Command Prompt.
cd /d "%~dp0\.."
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt pyinstaller pillow
python packaging\make_icons.py
rmdir /s /q build dist 2>nul
pyinstaller --noconfirm packaging\arcpeq.spec
echo.
echo Built: %cd%\dist\ArcPEQ\ArcPEQ.exe
