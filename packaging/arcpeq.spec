# PyInstaller spec: pyinstaller --noconfirm packaging/arcpeq.spec
import sys
from pathlib import Path

here = Path(SPECPATH)
root = here.parent
icon = here / ("icon.icns" if sys.platform == "darwin" else "icon.ico" if sys.platform == "win32" else "icon.png")

a = Analysis([str(root / "run.py")], pathex=[str(root)], hiddenimports=["hid"],
             excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
                       "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtNetwork"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ArcPEQ", console=False,
          icon=str(icon) if icon.exists() else None)
coll = COLLECT(exe, a.binaries, a.datas, name="ArcPEQ")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Arc PEQ.app", icon=str(icon) if icon.exists() else None,
                 bundle_identifier="local.arcpeq.app",
                 info_plist={"CFBundleShortVersionString": "1.1.0", "CFBundleVersion": "1.1.0",
                             "NSHighResolutionCapable": True, "LSApplicationCategoryType": "public.app-category.music",
                             "NSHumanReadableCopyright": "Arc PEQ"})
