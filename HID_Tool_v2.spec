# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

# 產生 edition 檔（Engineer = 全功能），hid_tool 會 import _edition 取值
with open('_edition.py', 'w', encoding='utf-8') as _f:
    _f.write('EDITION = "Engineer"\n')

# sv_ttk 的主題 .tcl 檔必須一起打包，否則執行檔找不到主題
datas = collect_data_files('sv_ttk')
datas += [('assets\\RE024_icon_heatmap.ico', 'assets')]

a = Analysis(
    ['hid_tool.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['sv_ttk'],
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
