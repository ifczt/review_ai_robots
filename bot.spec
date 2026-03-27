# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


hiddenimports = []
datas = []
binaries = []

for package in [
    "anthropic",
    "bcrypt",
    "cryptography",
    "dotenv",
    "httpx",
    "invoke",
    "lark_oapi",
    "nacl",
    "openai",
    "paramiko",
    "pydantic_settings",
    "pymysql",
]:
    hiddenimports += collect_submodules(package)

for package in [
    "anthropic",
    "certifi",
    "cryptography",
    "httpx",
    "lark_oapi",
    "openai",
    "paramiko",
]:
    datas += collect_data_files(package)

for package in [
    "bcrypt",
    "cryptography",
    "nacl",
]:
    binaries += collect_dynamic_libs(package)

for distribution in [
    "anthropic",
    "lark-oapi",
    "openai",
    "paramiko",
    "pydantic-settings",
]:
    datas += copy_metadata(distribution)


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
