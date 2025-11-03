# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\70510\\nihon\\vocab_v3.3.4.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\70510\\nihon\\tools', 'tools'), ('C:\\Users\\70510\\nihon\\assets\\fonts\\ZenMaruGothic-Medium.ttf', 'assets/fonts')],
    hiddenimports=[],
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
    a.datas,
    [],
    name='vocab_latest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir='C:\\Users\\70510\\AppData\\Local\\vocab_rt',
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
