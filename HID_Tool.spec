# -*- mode: python ; coding: utf-8 -*-

# 每個 spec 都明確寫入自己的 edition，避免先建 FAE 後再建 Engineer 時
# 沿用上一輪留下的 _edition.py。
with open('_edition.py', 'w', encoding='utf-8') as _f:
    _f.write('EDITION = "Engineer"\n')

a = Analysis(
    ['hid_tool.py'],
    pathex=[],
    binaries=[],
    datas=[('assets\\RE024_icon_heatmap.ico', 'assets')],
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
    name='RE024 Touch Inspector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\RE024_icon_heatmap.ico',
)
