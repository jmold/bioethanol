# -*- mode: python ; coding: utf-8 -*-
import os

a = Analysis(
    ["desktop_backend.py"],
    pathex=[os.path.abspath('.')],
    binaries=[],
    datas=[
        ("flowsheet_reference.json", "."),
        ("flowsheet_alt_separation_before_fermentation.json", "."),
    ],
    hiddenimports=["blocks", "models", "flowsheet", "api", "api_v020", "distillation_shortcut", "native_dynamic_engine", "connected_dynamic_engine", "scenario_engine", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
    module_collection_mode={
        "blocks": "py",
        "models": "py",
        "flowsheet": "py",
        "api": "py",
        "api_v020": "py",
    },
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bioagri-backend",
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
)
