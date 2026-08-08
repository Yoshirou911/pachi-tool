# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project_root = Path(SPEC).parent
app_icon = project_root / "desktop" / "PACHI_TOOL.ico"

datas = [
    (str(project_root / "web"), "web"),
    (str(project_root / "data" / "machines"), "data/machines"),
    (str(project_root / "data" / "opportunity_catalog.json"), "data"),
]

for optional_name in ("hall_coords.json",):
    optional_path = project_root / "data" / optional_name
    if optional_path.exists():
        datas.append((str(optional_path), "data"))

a = Analysis(
    [str(project_root / "desktop_app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "webview.platforms.edgechromium",
        "clr",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PACHI TOOL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(app_icon),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PACHI TOOL",
)
