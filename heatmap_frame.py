"""
Heatmap frame backend — 解析韌體 dump 的觸控矩陣 TXT，並把每個 frame 渲染成
帶背景色的 HTML 表格（熱圖）。colormap 自製，不依賴 matplotlib。

刻意不 import tkinter，HTML 匯出 worker 才能被 pickle 並交給
ProcessPoolExecutor 執行（含 PyInstaller 打包後的環境）。

僅支援「純自互容」格式：Used_Tx 之前為一般列、之後為特殊列，全部上色，
中間插一條白色間隔列。
"""

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Colormaps — 控制點取樣自 matplotlib / ColorBrewer，0~255 RGB
# 每個 colormap 是 [(stop, (r, g, b)), ...]，stop ∈ [0, 1]
# ---------------------------------------------------------------------------

_COLORMAPS = {
    "coolwarm": [
        (0.000, (59, 76, 192)),
        (0.125, (98, 130, 234)),
        (0.250, (141, 176, 254)),
        (0.375, (184, 208, 249)),
        (0.500, (221, 221, 221)),
        (0.625, (245, 196, 173)),
        (0.750, (244, 154, 123)),
        (0.875, (222, 96, 77)),
        (1.000, (180, 4, 38)),
    ],
    "bwr": [
        (0.0, (0, 0, 255)),
        (0.5, (255, 255, 255)),
        (1.0, (255, 0, 0)),
    ],
    "seismic": [
        (0.00, (0, 0, 76)),
        (0.25, (0, 0, 255)),
        (0.50, (255, 255, 255)),
        (0.75, (255, 0, 0)),
        (1.00, (128, 0, 0)),
    ],
    "RdBu": [
        (0.0, (103, 0, 31)),
        (0.1, (178, 24, 43)),
        (0.2, (214, 96, 77)),
        (0.3, (244, 165, 130)),
        (0.4, (253, 219, 199)),
        (0.5, (247, 247, 247)),
        (0.6, (209, 229, 240)),
        (0.7, (146, 197, 222)),
        (0.8, (67, 147, 195)),
        (0.9, (33, 102, 172)),
        (1.0, (5, 48, 97)),
    ],
    "PiYG": [
        (0.0, (142, 1, 82)),
        (0.1, (197, 27, 125)),
        (0.2, (222, 119, 174)),
        (0.3, (241, 182, 218)),
        (0.4, (253, 224, 239)),
        (0.5, (247, 247, 247)),
        (0.6, (230, 245, 208)),
        (0.7, (184, 225, 134)),
        (0.8, (127, 188, 65)),
        (0.9, (77, 146, 33)),
        (1.0, (39, 100, 25)),
    ],
}

CMAP_NAMES = ["coolwarm", "bwr", "seismic", "RdBu", "PiYG"]
_LUT_SIZE = 256
NONE_BG = "#CCCCCC"
NONE_TXT = "#000000"


def _interp(stops: List[Tuple[float, Tuple[int, int, int]]], t: float) -> Tuple[int, int, int]:
    if t <= 0:
        return stops[0][1]
    if t >= 1:
        return stops[-1][1]
    for k in range(len(stops) - 1):
        s0, c0 = stops[k]
        s1, c1 = stops[k + 1]
        if t <= s1:
            f = 0.0 if s1 == s0 else (t - s0) / (s1 - s0)
            return tuple(int(round(c0[j] + (c1[j] - c0[j]) * f)) for j in range(3))
    return stops[-1][1]


def _cell_colors(rgb: Tuple[int, int, int]) -> Tuple[str, str]:
    """回傳 (背景 hex, 文字 hex)。文字依亮度選黑/白，確保數字可讀。"""
    bg = "#%02X%02X%02X" % rgb
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    txt = "#000000" if lum > 150 else "#FFFFFF"
    return bg, txt


def build_lut(name: str, n: int = _LUT_SIZE) -> List[Tuple[str, str]]:
    """產生 n 段查色表，每段為 (背景 hex, 文字 hex)。"""
    stops = _COLORMAPS.get(name) or _COLORMAPS["coolwarm"]
    lut = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        lut.append(_cell_colors(_interp(stops, t)))
    return lut


def value_to_index(val: float, vmin: float, vmax: float, n: int = _LUT_SIZE) -> int:
    if vmax <= vmin:
        return 0
    t = (val - vmin) / (vmax - vmin)
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    return int(round(t * (n - 1)))


def cell_color(val, vmin: float, vmax: float, lut: List[Tuple[str, str]]) -> Tuple[str, str]:
    """給 GUI canvas 用：回傳某數值對應的 (背景 hex, 文字 hex)。"""
    if val is None:
        return NONE_BG, NONE_TXT
    return lut[value_to_index(val, vmin, vmax, len(lut))]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _rectify(rows: List[List[int]]) -> Tuple[List[List[Optional[int]]], int]:
    """把長度不一的列補 None 對齊成矩形，回傳 (矩形, 不齊列數)。"""
    max_cols = max(len(r) for r in rows)
    non_rect = 0
    rect: List[List[Optional[int]]] = []
    for r in rows:
        if len(r) != max_cols:
            non_rect += 1
        rect.append(r + [None] * (max_cols - len(r)))
    return rect, non_rect


def parse_frames(path: str):
    """以空白行分隔解析 frames。回傳 (frames, used_tx, stats)。"""
    frames: List[List[List[Optional[int]]]] = []
    current: List[List[int]] = []
    used_tx: Optional[int] = None
    skipped_lines = 0
    non_rect_rows = 0

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("Used_Tx:"):
                try:
                    used_tx = int(line.strip().split(":")[1])
                except Exception:
                    used_tx = None
                continue
            if not line.strip():
                if current:
                    rect, nr = _rectify(current)
                    frames.append(rect)
                    non_rect_rows += nr
                    current = []
            else:
                try:
                    current.append([int(x) for x in line.strip().split()])
                except ValueError:
                    skipped_lines += 1

    if current:
        rect, nr = _rectify(current)
        frames.append(rect)
        non_rect_rows += nr

    return frames, used_tx, {
        "skipped_lines": skipped_lines,
        "non_rect_rows": non_rect_rows,
    }


# ---------------------------------------------------------------------------
# HTML 渲染（worker 在子行程執行）
# ---------------------------------------------------------------------------

_WORKER_LUT: Optional[List[Tuple[str, str]]] = None


def _init_worker(lut: List[Tuple[str, str]]):
    global _WORKER_LUT
    _WORKER_LUT = lut


def frame_to_html(i: int, frame, used_tx, vmin, vmax):
    lut = _WORKER_LUT
    n = len(lut)
    out = [
        f'<h3 style="font-family:Segoe UI,Arial,sans-serif;">Frame {i:05}</h3>',
        '<table border="1" cellspacing="0" cellpadding="2" '
        'style="border-collapse:collapse;font-size:10px;font-family:Consolas,monospace;">',
    ]
    for r_idx, row in enumerate(frame):
        if used_tx is not None and r_idx == used_tx:
            out.append('<tr><td colspan="100" style="height:6px;background:#ffffff;"></td></tr>')
        tds = []
        for val in row:
            if val is None:
                tds.append(f'<td style="background-color:{NONE_BG};color:{NONE_TXT};text-align:right;"></td>')
            else:
                bg, txt = lut[value_to_index(val, vmin, vmax, n)]
                tds.append(f'<td style="background-color:{bg};color:{txt};text-align:right;">{val}</td>')
        out.append(f"<tr>{''.join(tds)}</tr>")
    out.append("</table><br><br>")
    return i, "".join(out)


def _open_chunk(base_name, output_dir, total_frames, start_index, chunk_size):
    part = (start_index // chunk_size) + 1
    filename = os.path.join(output_dir, f"{base_name}_part{part:02d}.html")
    f = open(filename, "w", encoding="utf-8")
    end_index = min(start_index + chunk_size - 1, total_frames - 1)
    f.write('<html><head><meta charset="utf-8"><title>Frame Viewer</title></head><body>')
    f.write('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:12px;color:#333;">')
    f.write(f"總計 {total_frames} 個 Frames，本頁顯示 {start_index}~{end_index}</div><hr>")
    return filename, f


def export_html(frames, used_tx, vmin, vmax, lut, input_path, chunk_size=500,
                progress_cb=None, cancel_event=None, max_workers=4) -> List[str]:
    """平行渲染並依序串流寫出分頁 HTML，回傳輸出檔清單。"""
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_dir = os.path.join(os.path.dirname(input_path), base_name + "_frames")
    os.makedirs(output_dir, exist_ok=True)

    total = len(frames)
    if total == 0:
        return []

    output_paths: List[str] = []
    buffer = {}
    expected = 0
    written_in_part = 0
    start_index = 0
    cur_file = None

    try:
        with ProcessPoolExecutor(max_workers=max_workers,
                                 initializer=_init_worker, initargs=(lut,)) as ex:
            futures = {}
            for i, frame in enumerate(frames):
                if cancel_event and cancel_event.is_set():
                    break
                futures[ex.submit(frame_to_html, i, frame, used_tx, vmin, vmax)] = i

            filename, cur_file = _open_chunk(base_name, output_dir, total, start_index, chunk_size)
            output_paths.append(filename)

            done = 0
            for fut in as_completed(futures):
                if cancel_event and cancel_event.is_set():
                    break
                i, html_part = fut.result()
                buffer[i] = html_part
                done += 1
                if progress_cb:
                    progress_cb(done, total)

                while expected in buffer:
                    cur_file.write(buffer.pop(expected))
                    expected += 1
                    written_in_part += 1
                    if written_in_part >= chunk_size and expected < total:
                        cur_file.write("</body></html>")
                        cur_file.close()
                        start_index = expected
                        filename, cur_file = _open_chunk(base_name, output_dir, total, start_index, chunk_size)
                        output_paths.append(filename)
                        written_in_part = 0
    finally:
        if cur_file and not cur_file.closed:
            cur_file.write("</body></html>")
            cur_file.close()

    return output_paths
