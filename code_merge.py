"""
兩份原始碼資料夾的比對＋合併核心（「合Code」分頁用）。

左右各指定一個資料夾，依相對路徑掃出三種狀態：
  - only_left  只有左邊有
  - only_right 只有右邊有
  - diff       兩邊都有但內容不同
  - same       兩邊都有且內容相同
「複製到右邊」永遠是左覆蓋右（左邊是要合進去的來源），不處理刪除。
"""

import difflib
import filecmp
import os
import shutil
from dataclasses import dataclass
from typing import List, Optional, Set

DEFAULT_IGNORE_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
}

STATUS_SAME = "same"
STATUS_DIFF = "diff"
STATUS_ONLY_LEFT = "only_left"
STATUS_ONLY_RIGHT = "only_right"


@dataclass
class DiffEntry:
    rel_path: str
    status: str
    left_path: Optional[str]
    right_path: Optional[str]
    left_size: Optional[int]
    right_size: Optional[int]
    left_mtime: Optional[float] = None
    right_mtime: Optional[float] = None


def _list_files(root: str, ignore_dirs: Set[str]) -> List[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            out.append(rel.replace(os.sep, "/"))
    return out


def scan_diff(left_dir: str, right_dir: str,
              ignore_dirs: Optional[Set[str]] = None) -> List[DiffEntry]:
    """掃描兩個資料夾，回傳依相對路徑排序的差異清單。"""
    ignore = DEFAULT_IGNORE_DIRS if ignore_dirs is None else ignore_dirs
    left_files = set(_list_files(left_dir, ignore))
    right_files = set(_list_files(right_dir, ignore))

    entries: List[DiffEntry] = []
    for rel in sorted(left_files | right_files, key=str.lower):
        lp = os.path.join(left_dir, rel) if rel in left_files else None
        rp = os.path.join(right_dir, rel) if rel in right_files else None
        lsize = os.path.getsize(lp) if lp else None
        rsize = os.path.getsize(rp) if rp else None
        lmtime = os.path.getmtime(lp) if lp else None
        rmtime = os.path.getmtime(rp) if rp else None
        if lp and rp:
            status = STATUS_SAME if filecmp.cmp(lp, rp, shallow=False) else STATUS_DIFF
        elif lp:
            status = STATUS_ONLY_LEFT
        else:
            status = STATUS_ONLY_RIGHT
        entries.append(DiffEntry(rel, status, lp, rp, lsize, rsize, lmtime, rmtime))
    return entries


def rescan_entry(entry: DiffEntry, left_dir: str, right_dir: str) -> DiffEntry:
    """只重算單一相對路徑的狀態（複製後更新單一列，不必整個資料夾重掃）。"""
    lp = os.path.join(left_dir, entry.rel_path)
    rp = os.path.join(right_dir, entry.rel_path)
    lp = lp if os.path.isfile(lp) else None
    rp = rp if os.path.isfile(rp) else None
    lsize = os.path.getsize(lp) if lp else None
    rsize = os.path.getsize(rp) if rp else None
    lmtime = os.path.getmtime(lp) if lp else None
    rmtime = os.path.getmtime(rp) if rp else None
    if lp and rp:
        status = STATUS_SAME if filecmp.cmp(lp, rp, shallow=False) else STATUS_DIFF
    elif lp:
        status = STATUS_ONLY_LEFT
    else:
        status = STATUS_ONLY_RIGHT
    return DiffEntry(entry.rel_path, status, lp, rp, lsize, rsize, lmtime, rmtime)


def copy_left_to_right(entry: DiffEntry, right_dir: str) -> str:
    """把左邊的檔案複製到右邊資料夾對應的相對路徑，回傳目的路徑。"""
    if not entry.left_path:
        raise ValueError("左邊沒有此檔案，無法複製")
    dst = os.path.join(right_dir, entry.rel_path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(entry.left_path, dst)
    return dst


def copy_right_to_left(entry: DiffEntry, left_dir: str) -> str:
    """把右邊的檔案複製到左邊資料夾對應的相對路徑，回傳目的路徑。"""
    if not entry.right_path:
        raise ValueError("右邊沒有此檔案，無法複製")
    dst = os.path.join(left_dir, entry.rel_path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(entry.right_path, dst)
    return dst


_BINARY_SNIFF_BYTES = 4096


def _looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def looks_binary(path: str) -> bool:
    """判斷檔案是否為二進位檔（合併視窗用這個擋掉不能逐行處理的檔案）。"""
    return _looks_binary(path)


def read_text_lines(path: str) -> List[str]:
    """讀出文字檔內容，逐行（保留換行符）。"""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def write_text_lines(path: str, lines: List[str]) -> None:
    """把逐行內容寫回檔案（合併視窗存檔用）。"""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(lines)


def unified_diff_lines(entry: DiffEntry, context: int = 3) -> List[str]:
    """回傳這一項的逐行 unified diff；二進位檔案回單行提示而不嘗試解碼。"""
    if entry.left_path is None or entry.right_path is None:
        raise ValueError("只有單邊有檔案，無法產生逐行差異")
    if _looks_binary(entry.left_path) or _looks_binary(entry.right_path):
        return ["(二進位檔案，無法顯示文字差異)"]

    def read_lines(path: str) -> List[str]:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readlines()

    return list(difflib.unified_diff(
        read_lines(entry.left_path), read_lines(entry.right_path),
        fromfile=f"左：{entry.rel_path}", tofile=f"右：{entry.rel_path}",
        n=context,
    ))


def get_opcodes(left_lines: List[str], right_lines: List[str]):
    """逐行 diff 的 opcodes（合併視窗逐段搬移用）。

    回傳 List[Tuple[tag, i1, i2, j1, j2]]，tag 為
    "equal" / "replace" / "delete"（只有左邊有）/ "insert"（只有右邊有）。
    關掉 autojunk：原始碼常有大量重複的空行/縮排，開著容易漏段。
    """
    sm = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
    return sm.get_opcodes()
