"""自動更新模組 — 透過 GitHub Releases 檢查 / 下載 / 自我替換新版 exe。

設計重點
--------
* 只用 Python 標準庫（urllib / ssl / hashlib），不額外加依賴，方便 PyInstaller 打包。
* 全部網路與檔案動作都可在背景執行緒呼叫，UI 端再用 tk.after 收結果。
* Windows 上「執行中的 exe 不能被覆蓋，但可以被改名」——利用這點做自我替換：
    1. 把目前的 exe 改名成 `<exe>.old`
    2. 把下載好的新檔放回原檔名
    3. 啟動新 exe、本行程結束
    4. 新版啟動時 cleanup_old() 把殘留的 `.old` 刪掉
* 只有「凍結」(PyInstaller 打包) 後才會真的自我替換；用 `python hid_tool.py` 開發執行
  時 is_frozen() 為 False，不會動到任何檔案。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request

# 發版的 GitHub repo（owner/name）
GITHUB_REPO = "hn14932306-sudo/hid_tool"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_TIMEOUT = 12          # 秒；離線時快速放棄，不卡 UI
_UA = "RE024-Touch-Inspector-Updater"


def _ssl_context():
    """建立 SSL context。優先用 truststore（走 Windows 系統信任庫／SChannel），
    才能在有 TLS 攔截（公司 MITM proxy）的網路下，信任公司根憑證——這也是瀏覽器/
    git 能連的原因。沒有 truststore 時退回標準 context。"""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# 版本字串
# ---------------------------------------------------------------------------
def parse_version(s: str) -> tuple:
    """'v1.2' / '1.2.3' / 'V1.10' → (1, 2) / (1, 2, 3) / (1, 10)。無法解析回 (0,)。"""
    s = (s or "").strip().lstrip("vV")
    parts = []
    for tok in s.replace("-", ".").replace("_", ".").split("."):
        num = "".join(ch for ch in tok if ch.isdigit())
        if num == "":
            break
        parts.append(int(num))
    return tuple(parts) if parts else (0,)


def is_newer(latest_label: str, current_label: str) -> bool:
    return parse_version(latest_label) > parse_version(current_label)


# ---------------------------------------------------------------------------
# 執行環境
# ---------------------------------------------------------------------------
def is_frozen() -> bool:
    """是否為 PyInstaller 打包後的 exe（只有此情況才做自我替換）。"""
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> str:
    return os.path.abspath(sys.executable)


def cleanup_old() -> None:
    """啟動時刪除自我替換留下的 `<exe>.old` 殘檔（可能還被鎖，失敗就下次再刪）。"""
    if not is_frozen():
        return
    old = current_exe_path() + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 查詢 GitHub Releases
# ---------------------------------------------------------------------------
def _http_get(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": accept}
    )
    return urllib.request.urlopen(req, timeout=_TIMEOUT, context=_ssl_context())


def _http_json(url: str) -> dict:
    with _http_get(url) as r:
        return json.loads(r.read().decode("utf-8"))


def _pick_asset(assets: list, edition: str):
    """依 edition 從 release 資產中挑對應的 exe。

    約定：FAE 版資產檔名含 'FAE'（不分大小寫）；工程版資產名不含 'FAE'。
    """
    exes = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
    if not exes:
        return None
    want_fae = (edition != "Engineer")
    for a in exes:
        is_fae = "fae" in str(a.get("name", "")).lower()
        if is_fae == want_fae:
            return a
    return exes[0]   # 找不到對應的就回第一個，總比不更新好


def check_latest(current_label: str, edition: str):
    """查最新 release。回傳新版資訊 dict，或 None（已是最新 / 沒有對應資產）。

    可能丟出網路相關例外（離線、逾時、403…）——由呼叫端負責安靜吞掉。
    回傳 dict 欄位：version / notes / url / name / size / digest
    """
    data = _http_json(_API_LATEST)
    tag = data.get("tag_name") or data.get("name") or ""
    if not is_newer(tag, current_label):
        return None
    asset = _pick_asset(data.get("assets", []), edition)
    if not asset:
        return None
    return {
        "version": tag,
        "notes": (data.get("body") or "").strip(),
        "url": asset["browser_download_url"],
        "name": asset["name"],
        "size": int(asset.get("size", 0) or 0),
        "digest": str(asset.get("digest", "") or ""),   # 新版 API：'sha256:...'
    }


# ---------------------------------------------------------------------------
# 下載 + 套用
# ---------------------------------------------------------------------------
def download(url: str, dest: str, progress_cb=None) -> str:
    """下載到 dest，回傳檔案的 sha256 hex。progress_cb(done, total) 可選。"""
    h = hashlib.sha256()
    with _http_get(url, accept="application/octet-stream") as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)
    return h.hexdigest()


def verify_digest(sha256_hex: str, digest: str) -> bool:
    """比對 GitHub API 的 digest（'sha256:...'）。沒提供 digest 時視為通過（HTTPS 已保完整性）。"""
    if not digest:
        return True
    want = digest.split(":", 1)[-1].strip().lower()
    return bool(want) and want == sha256_hex.lower()


def staged_path() -> str:
    """下載暫存路徑：放在 exe 同目錄，方便之後 os.replace（同磁碟區）。"""
    return current_exe_path() + ".new"


def apply_update(new_file: str) -> None:
    """以下載好的新檔自我替換並重啟。呼叫成功後本行程立即結束（不返回）。"""
    exe = current_exe_path()
    old = exe + ".old"
    # 先清掉上一輪可能殘留的 .old
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    os.replace(exe, old)            # 把執行中的 exe 改名（Windows 允許改名、不允許覆蓋）
    try:
        os.replace(new_file, exe)   # 同磁碟區的原子替換
    except OSError:
        shutil.move(new_file, exe)  # 跨磁碟區後援
    subprocess.Popen([exe], close_fds=True)
    os._exit(0)                     # 不跑 atexit / tk 清理，乾淨退出讓新版接手
