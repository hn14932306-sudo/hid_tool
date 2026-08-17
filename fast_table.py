"""以 tk.Text 為底的高速資料表。

ttk.Treeview 每次重繪都會重畫整個可見區的所有 cell，成本正比於
「可見列數 × 欄位數」；監聽高回報率封包時這會把 Tk event loop 塞住。
這裡改成自己持有資料、只把「目前可見的那幾列」寫進 Text，
量測下同樣 33 列 × 14 欄的情境約快一個數量級。

只實作監聽表格真正用到的功能：等寬欄位對齊、整列背景色（tag）、
垂直/水平捲動、欄寬設定。不做欄位排序與拖曳選取。
"""

import collections
import tkinter as tk
import tkinter.font as tkfont
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["FastTable"]

_MIN_COL_CHARS = 3
_COL_GAP = 1          # 欄與欄之間的空白字元數


class FastTable(tk.Frame):
    """對外行為接近 Treeview，但資料由自己保管。

    rows 以「最新在最前」儲存（index 0 = 最新一列），和原本
    Treeview.insert("", 0, ...) 的視覺順序一致。
    """

    def __init__(self, parent, font=("Consolas", 9), height=20,
                 bg="#ffffff", fg="#181b20", header_bg="#f5f6f9",
                 show_header=True, **kw):
        super().__init__(parent, **kw)
        self._show_header = show_header
        self._font = tkfont.Font(family=font[0], size=font[1])
        self._char_w = max(1, self._font.measure("0"))
        self._bg = bg
        self._fg = fg

        self._cols: List[str] = []                      # col_id 順序
        self._display: Optional[List[str]] = None       # None = 全部顯示
        self._labels: Dict[str, str] = {}
        self._widths: Dict[str, int] = {}               # col_id -> 字元數
        self._anchors: Dict[str, str] = {}

        self._rows: collections.deque = collections.deque()
        self._tags: collections.deque = collections.deque()
        self._max_rows = 200
        self._top = 0                                   # 捲動位置（0 = 最新）
        self._tag_bg: Dict[str, str] = {}
        self._tag_prio: Dict[str, int] = {}
        self._dirty = True

        self._header = tk.Text(self, height=1, font=self._font, wrap="none",
                               bd=0, highlightthickness=0, padx=4, pady=2,
                               bg=header_bg, fg=fg, cursor="arrow")
        if show_header:
            self._header.pack(side=tk.TOP, fill=tk.X)
        self._header.configure(state="disabled")

        self._body = tk.Text(self, height=height, font=self._font, wrap="none",
                             bd=0, highlightthickness=0, padx=4, pady=0,
                             bg=bg, fg=fg, cursor="arrow", spacing1=0, spacing3=0)
        self._body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._body.configure(state="disabled")

        self._yscroll_cb = None
        self._xscroll_cb = None
        self._body.bind("<Configure>", self._on_configure)
        self._body.bind("<MouseWheel>", self._on_wheel)
        self._body.bind("<Button-4>", lambda e: self._wheel(-3))
        self._body.bind("<Button-5>", lambda e: self._wheel(3))

    # ---------------------------------------------------------- 設定

    def configure_scroll(self, yscrollcommand=None, xscrollcommand=None):
        self._yscroll_cb = yscrollcommand
        self._xscroll_cb = xscrollcommand

    def set_columns(self, cols: Sequence[Tuple[str, str, int]]):
        """cols: [(col_id, label, width_px)]，width_px 會換算成字元數。"""
        self._cols = [c[0] for c in cols]
        self._display = None
        self._labels = {c[0]: c[1] for c in cols}
        self._widths = {c[0]: max(_MIN_COL_CHARS, int(round(c[2] / self._char_w)),
                                  -(-self._font.measure(str(c[1])) // self._char_w))
                        for c in cols}
        self._anchors = {c[0]: "center" for c in cols}
        self.clear()
        self._recompute_tabs()
        self._redraw_header()
        self._dirty = True
        self._render()

    def get_columns(self) -> List[Tuple[str, str, int]]:
        """回傳 [(col_id, label, width_px)]，供統計列沿用同一組欄寬對齊。"""
        return [(c, self._labels.get(c, c), self._widths.get(c, 8) * self._char_w)
                for c in self._cols]

    def set_display_columns(self, col_ids: Optional[Sequence[str]]):
        """只顯示這些欄位（None = 全部）。資料不受影響，純顯示層。"""
        self._display = list(col_ids) if col_ids is not None else None
        self._recompute_tabs()
        self._redraw_header()
        self._dirty = True
        self._render()

    def column(self, col_id: str, width: Optional[int] = None,
               anchor: Optional[str] = None):
        """width 以像素為單位（和 Treeview 一致）；不給值則回傳目前寬度。"""
        if width is None and anchor is None:
            return self._widths.get(col_id, 0) * self._char_w
        if width is not None:
            self._widths[col_id] = max(_MIN_COL_CHARS, int(round(width / self._char_w)))
        if anchor is not None:
            self._anchors[col_id] = anchor
        self._recompute_tabs()
        self._redraw_header()
        self._dirty = True
        self._render()
        return None

    _ANCHOR_TO_TK_TAB = {"w": "left", "e": "right", "center": "center"}

    def _recompute_tabs(self):
        """欄位對齊改交給 Tk Text 原生的 tab stop（像素位置，Tk 排版引擎直接
        算，不是我們自己拿字型測量去湊空白）——用手動補空白對齊，字型量出來的
        寬度跟空白字元本身的寬度只要有一點點誤差，多欄接在一起就會慢慢飄掉；
        tab stop 是 Tk 排版時的絕對像素位置，沒有這個累積誤差的問題。"""
        gap_px = _COL_GAP * self._char_w
        x = 0
        stops = []
        for c in self._active_cols():
            w_px = self._widths.get(c, 8) * self._char_w
            a = self._ANCHOR_TO_TK_TAB.get(self._anchors.get(c, "center"), "center")
            pos = x if a == "left" else (x + w_px if a == "right" else x + w_px // 2)
            # Tk 的 -tabs 選項規定每個 stop 都要是「正距離」，0 會直接丟
            # TclError；第一欄如果是靠左對齊，算出來就剛好是 0，之前這裡
            # try/except 吞掉例外，整組 tabs 沒套用成功，落回舊設定，
            # 才會看起來像是欄位對齊設定沒生效。
            pos = max(1, pos)
            stops.append(f"{pos} {a}")
            x += w_px + gap_px
        tabs_str = " ".join(stops)
        for w in (self._body, self._header):
            try:
                w.configure(tabs=tabs_str)
            except tk.TclError:
                pass

    def heading(self, col_id: str, text: str):
        self._labels[col_id] = text
        self._redraw_header()

    def tag_configure(self, name: str, background: str = None,
                      priority: int = 0, **_kw):
        """priority 大的蓋過小的。Text 本身是「後 tag_raise 的贏」，
        改成明確的數字，才不會因為呼叫順序被重排就悄悄改變顏色。"""
        if background:
            self._tag_bg[name] = background
            self._tag_prio[name] = priority
            self._body.tag_configure(name, background=background)
            for n in sorted(self._tag_prio, key=lambda k: self._tag_prio[k]):
                self._body.tag_raise(n)

    def set_max_rows(self, n: int):
        self._max_rows = max(1, int(n))
        if len(self._rows) > self._max_rows:
            while len(self._rows) > self._max_rows:
                self._rows.pop()
                self._tags.pop()
            self._dirty = True

    # ---------------------------------------------------------- 資料

    def insert_rows(self, batch: Sequence[Tuple[Sequence, Tuple[str, ...]]]):
        """batch 依「舊 → 新」排列，插入後最新的在最上面。"""
        rows = self._rows
        tags = self._tags
        for values, row_tags in batch:
            rows.appendleft(list(values))
            tags.appendleft(row_tags or ())
        n = self._max_rows
        while len(rows) > n:
            rows.pop()
            tags.pop()
        if self._top:
            # 使用者往下捲看歷史時，新資料不要把畫面跳掉
            self._top = min(self._top + len(batch), max(0, len(rows) - self._visible_rows()))
        self._dirty = True

    def clear(self):
        self._rows.clear()
        self._tags.clear()
        self._top = 0
        self._dirty = True

    def row_count(self) -> int:
        return len(self._rows)

    # ---------------------------------------------------------- 渲染

    def _visible_rows(self) -> int:
        try:
            h = self._body.winfo_height()
        except tk.TclError:
            return 1
        lh = self._font.metrics("linespace")
        return max(1, h // max(1, lh))

    def _active_cols(self) -> List[str]:
        return self._display if self._display is not None else self._cols

    def _fit_truncate(self, s: str, w_chars: int) -> str:
        """照這個字型『實際渲染出來的像素寬度』截斷，而不是字元數——中文字在
        等寬字型底下的畫面寬度不保證剛好是半形字元的整數倍（實際倍率看系統幫
        CJK 字元代換成哪個字型），用真正的 font.measure() 才不會兩列對不齊。"""
        target_px = w_chars * self._char_w
        if self._font.measure(s) <= target_px:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._font.measure(s[:mid]) <= target_px:
                lo = mid
            else:
                hi = mid - 1
        return s[:lo]

    def _fmt_row(self, values: Sequence) -> str:
        """每欄前面放一個 \t，靠 _recompute_tabs() 設好的 tab stop 對齊——
        不再自己拼空白，Tk 排版引擎的 tab 定位是像素級精準，不會累積誤差。
        還是要截斷過長內容，不然這一列的字會蓋過下一欄的 tab stop，讓
        Tk 直接跳去下下一欄的位置，反而讓這一整列歪掉。"""
        cols = self._active_cols()
        idx = {c: i for i, c in enumerate(self._cols)}
        parts = []
        for c in cols:
            w = self._widths.get(c, 8)
            i = idx.get(c, -1)
            v = "" if i < 0 or i >= len(values) else values[i]
            s = "" if v is None else str(v)
            parts.append(self._fit_truncate(s, w))
        return "\t" + "\t".join(parts)

    def _redraw_header(self):
        cols = self._active_cols()
        parts = [self._fit_truncate(str(self._labels.get(c, c)), self._widths.get(c, 8))
                 for c in cols]
        line = "\t" + "\t".join(parts)
        self._header.configure(state="normal")
        self._header.delete("1.0", "end")
        self._header.insert("1.0", line)
        self._header.configure(state="disabled")

    def _render(self):
        """只把可見範圍的列寫進 Text —— 成本和總列數無關。"""
        if not self._dirty:
            return
        self._dirty = False
        vis = self._visible_rows()
        top = max(0, min(self._top, max(0, len(self._rows) - vis)))
        self._top = top
        chunk = list(self._rows)[top:top + vis]
        chunk_tags = list(self._tags)[top:top + vis]

        body = self._body
        body.configure(state="normal")
        body.delete("1.0", "end")
        if chunk:
            body.insert("1.0", "\n".join(self._fmt_row(r) for r in chunk))
            for i, tg in enumerate(chunk_tags, start=1):
                for name in tg:
                    if name in self._tag_bg:
                        body.tag_add(name, f"{i}.0", f"{i}.end+1c")
        body.configure(state="disabled")

        total = max(1, len(self._rows))
        if self._yscroll_cb:
            self._yscroll_cb(top / total, min(1.0, (top + vis) / total))
        if self._xscroll_cb:
            self._xscroll_cb(*self._body.xview())

    def flush(self):
        """把累積的變更畫出來。呼叫端在一批資料進完後呼叫一次。"""
        self._render()

    # ---------------------------------------------------------- 捲動

    def _on_configure(self, _e=None):
        self._dirty = True
        self._render()

    def _on_wheel(self, e):
        self._wheel(-3 if e.delta > 0 else 3)

    def _wheel(self, step):
        self._top = max(0, min(self._top + step,
                               max(0, len(self._rows) - self._visible_rows())))
        self._dirty = True
        self._render()

    def yview(self, *args):
        if not args:
            vis = self._visible_rows()
            total = max(1, len(self._rows))
            return (self._top / total, min(1.0, (self._top + vis) / total))
        if args[0] == "moveto":
            self._top = int(float(args[1]) * len(self._rows))
        elif args[0] == "scroll":
            n = int(args[1])
            self._top += n * (self._visible_rows() if args[2] == "pages" else 1)
        self._top = max(0, min(self._top, max(0, len(self._rows) - self._visible_rows())))
        self._dirty = True
        self._render()

    def xview(self, *args):
        if not args:
            return self._body.xview()
        self._body.xview(*args)
        self._header.xview(*args)
        if self._xscroll_cb:
            self._xscroll_cb(*self._body.xview())
        return None

    def xview_moveto(self, f):
        self._body.xview_moveto(f)
        self._header.xview_moveto(f)
