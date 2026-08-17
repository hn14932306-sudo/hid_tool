# -*- mode: python ; coding: utf-8 -*-
"""一次 build 出三顆：Engineer、FAE 與 Customer。

    pyinstaller --noconfirm --clean HID_Tool.spec

以前是 HID_Tool_v2.spec / HID_Tool_Customer.spec 兩個檔各出一顆，但
HID_Tool.spec 與 HID_Tool_v2.spec 的產出檔名一模一樣（RE024 Touch
Inspector.exe），在別台機器上很容易挑錯 spec 而不自知，所以併成這一個。
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# ---------------------------------------------------------------------------
# 相依檢查：缺套件就讓 build 直接失敗
#
# PyInstaller 對「找不到的模組」只會往 build/<name>/warn-*.txt 寫一行警告然後
# 照樣 build 成功。v1.9.9 就是這樣出事的：某台機器沒裝 hid，打包出來的 exe
# 少了 hid.cp313-win_amd64.pyd，跑起來 enumerate_hid_devices() 永遠回空
# list，「監聽裝置」下拉一個裝置都沒有，但檔案本身看起來完全正常。
# ---------------------------------------------------------------------------
for _mod in ("hid", "sv_ttk", "truststore"):
    try:
        __import__(_mod)
    except ImportError:
        raise SystemExit(
            f"[build] 缺少相依套件 {_mod!r}，請先 pip install -r requirements.txt\n"
            f"        （少了 hid 會打包出「監聽裝置列不出來」的壞版本）"
        )

# ---------------------------------------------------------------------------
# edition
#
# 不能用「build 前覆寫根目錄 _edition.py」的老招：兩個 Analysis 在同一次 spec
# 執行裡跑完，PYZ 是之後才真的去讀那個路徑編譯，最後寫進去的那份會同時進到
# 兩顆 exe。改成每個 edition 各一份獨立檔案，用 pathex 指進去。
# ---------------------------------------------------------------------------
_EDITION_ROOT = os.path.join(SPECPATH, "build", "_edition")

# 根目錄若留著開發用的 _edition.py 會蓋掉 pathex 那份，先移走。
# hid_tool.py 在找不到 _edition 時本來就預設 Engineer，拿掉不影響直接跑原始碼。
_stale = os.path.join(SPECPATH, "_edition.py")
if os.path.exists(_stale):
    os.remove(_stale)


def _make_edition_dir(edition):
    d = os.path.join(_EDITION_ROOT, edition.lower())
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "_edition.py"), "w", encoding="utf-8") as f:
        f.write(f'EDITION = "{edition}"\n')
    return d


def _assert_edition(analysis, edition, expect_dir):
    """確認這顆真的抓到自己那份 _edition —— 抓錯只會讓 FAE 版悄悄變成
    Engineer（hid_tool.py 的 import 是 try/except 帶預設值），不會有任何錯誤。"""
    for name, path, _typ in analysis.pure:
        if name == "_edition":
            if os.path.normcase(os.path.dirname(path)) != os.path.normcase(expect_dir):
                raise SystemExit(f"[build] {edition} 抓到錯的 _edition: {path}")
            return
    raise SystemExit(f"[build] {edition} 沒有把 _edition 打包進去")


# sv_ttk 的主題 .tcl 檔必須一起打包，否則執行檔找不到主題
datas = collect_data_files("sv_ttk") + [
    ("assets\\RE024_icon_heatmap.ico", "assets"),
    ("assets\\focaltech_customer_logo.png", "assets"),
    ("assets\\focaltech_official.ico", "assets"),
]

# Descriptor 比對的 base target（bases/<profile>/*.txt）。
# exe 旁另放 bases/ 會優先於打包進來的這份，換 base 不用重 build。
for _root, _dirs, _files in os.walk(os.path.join(SPECPATH, "bases")):
    for _f in _files:
        if _f.lower().endswith(".txt"):
            _full = os.path.join(_root, _f)
            datas.append((_full, os.path.relpath(_root, SPECPATH)))

_COMMON = dict(
    binaries=[],
    datas=datas,
    hiddenimports=["sv_ttk", "truststore"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

_EXE_COMMON = dict(
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
    icon="assets\\RE024_icon_heatmap.ico",
)

# --- Engineer（全功能）------------------------------------------------------
_eng_dir = _make_edition_dir("Engineer")
a_eng = Analysis(["hid_tool.py"], pathex=[_eng_dir], **_COMMON)
_assert_edition(a_eng, "Engineer", _eng_dir)
pyz_eng = PYZ(a_eng.pure)
exe_eng = EXE(
    pyz_eng, a_eng.scripts, a_eng.binaries, a_eng.datas, [],
    name="RE024 Touch Inspector",
    **_EXE_COMMON,
)

# --- FAE（閹割版）-----------------------------------------------------------
# 檔名一定要含 "FAE"：updater._pick_asset 靠這個字串分辨 release 資產
_fae_dir = _make_edition_dir("FAE")
a_fae = Analysis(["hid_tool.py"], pathex=[_fae_dir], **_COMMON)
_assert_edition(a_fae, "FAE", _fae_dir)
pyz_fae = PYZ(a_fae.pure)
exe_fae = EXE(
    pyz_fae, a_fae.scripts, a_fae.binaries, a_fae.datas, [],
    name="RE024 Touch Inspector (FAE)",
    **_EXE_COMMON,
)

# --- Customer（僅監聽 + 錄製，隱藏 Descriptor / FF01）-----------------------
_customer_dir = _make_edition_dir("Customer")
a_customer = Analysis(["hid_tool.py"], pathex=[_customer_dir], **_COMMON)
_assert_edition(a_customer, "Customer", _customer_dir)
pyz_customer = PYZ(a_customer.pure)
exe_customer = EXE(
    pyz_customer, a_customer.scripts, a_customer.binaries, a_customer.datas, [],
    name="RE024 Touch Inspector (Customer)",
    # 不用 UPX：Customer 版曾被 Trend Micro Apex One 誤判為 Troj.Win32.TRX.XXPE50FFF108，
    # UPX 加殼是常見的誘因之一，Customer 版對外分發最在意這個所以先關掉；Engineer/FAE
    # 內部使用維持原樣。
    **{**_EXE_COMMON, "icon": "assets\\focaltech_official.ico", "upx": False},
)
