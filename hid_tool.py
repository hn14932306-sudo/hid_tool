"""
RE024 Touch Inspector - Monitor + Send GUI
Imports backend modules: hid_descriptor, hid_rawinput, hid_device
"""

import collections
import csv
import ctypes
import hashlib
import datetime
import json
import math
import os
import queue
import re
import sys
import traceback
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, ttk, scrolledtext
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import sv_ttk
import heatmap_frame
import digiinfo_parse
import updater

from hid_descriptor import (
    HIDField,
    REPORT_TYPE_INPUT,
    REPORT_TYPE_OUTPUT,
    REPORT_TYPE_FEATURE,
    parse_report_descriptor,
    extract_field_value,
    get_usage_name,
)
from hid_rawinput import RawInputThread, DeviceChangeThread
from hid_device import (
    enumerate_hid_devices,
    format_device_label,
    device_collection,
    read_descriptor_via_hidapi,
    send_output_report,
    send_feature_report,
    get_feature_report,
    get_input_report,
    set_output_report_cmd,
    read_interrupt,
    parse_hex_bytes,
)


# ---------------------------------------------------------------------------
# Edition（版本）：由 build 時產生的 _edition.py 決定，預設 Engineer
# ---------------------------------------------------------------------------
try:
    from _edition import EDITION as _BUILD_EDITION
except Exception:
    _BUILD_EDITION = "Engineer"


# ---------------------------------------------------------------------------
# Unified GUI Application
# ---------------------------------------------------------------------------

class HIDToolApp(tk.Tk):
    _APP_NAME          = "RE024 Touch Inspector"
    _APP_AUTHOR        = "Shane.Lin"
    _APP_VERSION_LABEL = "v1.4"
    _APP_VERSION_TIME  = "2026-06-25"

    # 版本(edition)：Engineer = 全功能；FAE / Customer = 閹割版
    # 由 build 時產生的 _edition.py 決定（見 .spec），開發/沒有該檔時預設 Engineer。
    # 工程專用功能用 if self._is_engineer(): ... 包起來即可。
    _EDITION = _BUILD_EDITION

    @classmethod
    def _is_engineer(cls) -> bool:
        return cls._EDITION == "Engineer"

    # ---- UI palette & fonts（配合 sv-ttk light 主題）----
    _BG          = "#fafafa"   # sv-ttk light 背景
    _SURFACE     = "#ffffff"   # canvas / logs
    _BORDER      = "#d7d7d7"
    _TEXT        = "#1c1c1c"
    _TEXT_MUTED  = "#6b6b6b"
    _ACCENT      = "#0067c0"   # sv-ttk accent 藍
    _ACCENT_DARK = "#005ba1"
    _GREEN       = "#2f9e44"
    _GREEN_DARK  = "#268a3b"
    _RED         = "#e03131"
    _RED_DARK    = "#c92a2a"
    _STRIPE      = "#f0f0f3"   # zebra row background (subtle)

    _FONT_FAMILY  = "Microsoft JhengHei UI"   # 全 UI 統一字族（中英共用，Windows 內建）
    _FONT_UI      = (_FONT_FAMILY, 9)
    _FONT_UI_BOLD = (_FONT_FAMILY, 9, "bold")
    _FONT_MONO    = ("Consolas", 9)

    # UI 在 96 DPI（100%）下的基準視窗尺寸；實際依螢幕 DPI 動態放大
    _BASE_W, _BASE_H       = 1400, 820
    _BASE_MIN_W, _BASE_MIN_H = 1120, 680

    # 監聽裝置下拉的第一個哨兵：不選裝置、自動解所有 digitizer
    _ALL_DIGI_LABEL = "（全部 digitizer — 不選裝置，自動解碼）"

    _RECORD_MAX   = 50000   # 監聽回放的封包環形緩衝上限（約數 MB）

    @staticmethod
    def _resource_path(*parts: str) -> str:
        base_dir = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
        return os.path.join(base_dir, *parts)

    def _set_window_icon(self, window=None):
        window = window or self
        icon_path = self._resource_path("assets", "RE024_icon_heatmap.ico")
        if not os.path.exists(icon_path):
            return
        try:
            window.iconbitmap(icon_path)
            if window is self:
                window.iconbitmap(default=icon_path)
        except tk.TclError:
            pass

    @staticmethod
    def _set_dpi_awareness():
        """DPI 感知：採用 System Aware——整個程式以「啟動時主螢幕」的 DPI 計算一次，
        拖到不同 DPI 的螢幕時由 Windows 等比點陣拉伸，版面與字體不會重算、不會跑掉
        （代價：在非主螢幕上字會稍微糊一點，屬 OS 層級取捨）。
        刻意不用 per-monitor，避免拖到另一螢幕後字體被重算成過大／過小。
        必須在建立 Tk 視窗前呼叫。"""
        try:
            user32 = ctypes.windll.user32
            user32.SetProcessDpiAwarenessContext.restype  = ctypes.c_bool
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2)):
                return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def _current_dpi(self) -> int:
        """取得視窗目前所在螢幕的 DPI（per-monitor）。失敗則退回 Tk 量測。"""
        try:
            user32 = ctypes.windll.user32
            user32.GetDpiForWindow.restype  = ctypes.c_uint
            user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
            dpi = user32.GetDpiForWindow(ctypes.c_void_p(self.winfo_id()))
            if dpi:
                return int(dpi)
        except Exception:
            pass
        try:
            return int(round(self.winfo_fpixels("1i")))
        except Exception:
            return 96

    def _apply_dpi_scaling(self) -> int:
        """依目前螢幕 DPI 設定 tk scaling（pt→px），讓字體在 4K/2K 自動放大。"""
        dpi = self._current_dpi() or 96
        self._cur_dpi = dpi
        try:
            self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        self._apply_font_scaling()
        return dpi

    # sv_ttk 用負值（像素）定義的具名字體，不吃 tk scaling，需手動依倍率放大
    _SV_FONTS = (
        "SunValleyCaptionFont", "SunValleyBodyFont", "SunValleyBodyStrongFont",
        "SunValleyBodyLargeFont", "SunValleySubtitleFont", "SunValleyTitleFont",
        "SunValleyTitleLargeFont", "SunValleyDisplayFont",
    )

    # Tk 內建具名字體（ttk 元件與選單/工具提示等會用到，非等寬資料字體）
    _TK_FONTS = (
        "TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont",
        "TkTooltipFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont",
    )

    def _unify_font_family(self):
        """把 sv_ttk 主題與 Tk 內建的具名字體字族全部統一成微軟正黑體，
        讓中文不再 fallback 到細明體、與英數同一套字。只改字族不動大小／粗細，
        故 DPI 縮放與粗體仍正常。等寬資料字體（Consolas）刻意不動，維持表格對齊。"""
        for name in self._SV_FONTS + self._TK_FONTS:
            try:
                tkfont.nametofont(name).configure(family=self._FONT_FAMILY)
            except Exception:
                pass

    def _capture_sv_font_sizes(self):
        """記下 sv_ttk 具名字體的原始大小，之後依 DPI 倍率從原始值換算（避免疊加）。"""
        self._sv_font_orig = {}
        for name in self._SV_FONTS:
            try:
                self._sv_font_orig[name] = int(tkfont.nametofont(name).cget("size"))
            except Exception:
                pass

    def _sx(self, px) -> int:
        """把基準（96 DPI）的像素值依目前 DPI 倍率（sf=dpi/96）縮放，用於欄寬等固定像素。"""
        return int(round(px * (getattr(self, "_cur_dpi", 96) or 96) / 96.0))

    def _col_width(self, base, label) -> int:
        """欄寬取「基準寬度」與「標題實際文字寬度」較大者（避免長名稱被截斷），皆含 DPI 縮放。"""
        try:
            # SunValleyCaptionFont 是 Treeview 標題字體，已依 DPI 縮放，measure 即為實際像素
            needed = tkfont.nametofont("SunValleyCaptionFont").measure(str(label)) + self._sx(18)
        except Exception:
            needed = self._sx(len(str(label)) * 8 + 18)
        return max(self._sx(base), needed)

    def _apply_font_scaling(self):
        """把 sv_ttk 像素字體與 Treeview 列高依 DPI 倍率（sf=dpi/96）放大。"""
        sf = (getattr(self, "_cur_dpi", 96) or 96) / 96.0
        for name, orig in getattr(self, "_sv_font_orig", {}).items():
            try:
                tkfont.nametofont(name).configure(size=int(round(orig * sf)))
            except Exception:
                pass
        try:
            ttk.Style(self).configure("Treeview", rowheight=int(round(26 * sf)))
        except Exception:
            pass

    def _on_possible_dpi_change(self, event):
        """視窗被拖到不同 DPI 螢幕時，動態重算字體縮放。
        防抖：縮放/移動過程會連續觸發 <Configure>，等停下來再檢查一次，
        避免縮放途中反覆重設字體縮放造成卡頓。"""
        if event.widget is not self:
            return
        if getattr(self, "_dpi_check_after_id", None):
            self.after_cancel(self._dpi_check_after_id)
        self._dpi_check_after_id = self.after(250, self._do_dpi_check)

    def _do_dpi_check(self):
        self._dpi_check_after_id = None
        dpi = self._current_dpi()
        if dpi and dpi != self._cur_dpi:
            self._cur_dpi = dpi
            try:
                self.tk.call("tk", "scaling", dpi / 72.0)
            except Exception:
                pass
            self._apply_font_scaling()

    def __init__(self):
        # 高 DPI 清晰度（必須在建立 Tk 視窗前設定）
        self._set_dpi_awareness()
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RE024.TouchInspector")
        except Exception:
            pass
        super().__init__()
        self.withdraw()   # 立即隱藏，防止預設小視窗閃爍
        # 依螢幕 DPI 動態調整字體與視窗尺寸（4K/2K 高 DPI 自動放大）
        self._cur_dpi = 96
        dpi = self._apply_dpi_scaling()
        self.title(f"{self._APP_NAME} - {self._APP_VERSION_LABEL} [{self._EDITION}]")
        self._set_window_icon()
        sf = dpi / 96.0
        gw = min(int(self._BASE_W * sf), self.winfo_screenwidth())
        gh = min(int(self._BASE_H * sf), self.winfo_screenheight() - 60)
        self.geometry(f"{gw}x{gh}")
        self.minsize(min(int(self._BASE_MIN_W * sf), gw),
                     min(int(self._BASE_MIN_H * sf), gh))
        self._setup_style()

        # Shared state
        self._hidapi_devices:    List[dict]                = []
        self._selected_dev:      Optional[dict]            = None
        self._descriptors:       Dict[str, List[HIDField]] = {}
        self._raw_descriptors:   Dict[str, bytes]          = {}  # path_str -> raw descriptor bytes

        # 「全部 digitizer」自動模式
        self._all_digi_mode:     bool                      = False
        self._adigi_entries:     List[tuple]               = []   # (vidpid, col, ctx_by_rid) 預載清單
        self._digi_headers:      List[str]                 = []
        self._digi_rate_deques:  Dict[str, collections.deque] = {}  # 來源 label -> rx_time deque
        # 每裝置畫布（grid）：label -> {order,color,xr,yr,contacts,trails}
        # 注意：DigiInfo 分頁用 self._digi_canvas_touch / _digi_canvas_pen 當畫布元件，這裡用別名避免衝突
        self._adigi_devs:        Dict[str, dict]           = {}
        self._digi_dev_next:     int                       = 0
        self._digi_canvas_redraw_pending: bool             = False

        # Monitor state
        self._raw_thread:       Optional[RawInputThread] = None
        self._packet_queue:     queue.Queue              = queue.Queue()
        self._listening:        bool                     = False
        self._col_defs:         List[dict]               = []
        self._ff01_usage_vars:  Dict[int, tk.BooleanVar] = {}  # FF01 usage -> visible
        self._ff01_fmt:         tk.StringVar             = tk.StringVar(value="Hex")
        self._table_rid:        int                      = -1
        self._frame_deque:      collections.deque        = collections.deque()
        self._monitor_log_rows: List[dict]               = []
        self._last_pkt_rx_time: float                    = 0.0
        self._last_scan_time:   int                      = -1
        self._scan_time_field:  Optional[HIDField]       = None
        self._contact_count_field: Optional[Tuple[HIDField, int]] = None
        self._last_contact_count: int                    = -1
        self._last_touch_active: bool                   = False
        self._hybrid_groups:    List[dict]               = []
        self._hybrid_common:    Dict[str, Tuple[HIDField, int]] = {}
        self._frame_seq:        int                      = 0
        self._dev_tooltip:      Optional[tk.Toplevel]    = None
        self._dev_tooltip_label: Optional[tk.Label]      = None

        # Canvas state
        self._canvas_x_logical: Tuple[int, int]          = (0, 4096)
        self._canvas_y_logical: Tuple[int, int]          = (0, 4096)
        self._canvas_contacts:         Dict[int, dict]              = {}
        # 單點（手寫筆）畫布欄位參照：{"X":(hf,idx), "Y":..., "TipSwitch":..., "InRange":..., "Confidence":...}
        self._pen_canvas:              Optional[dict]               = None
        self._canvas_trails:           Dict[int, collections.deque] = {}
        self._canvas_prev_active:      set                          = set()
        self._canvas_item_ids:         Dict[int, Tuple[int, int]]   = {}
        self._canvas_item_shape:       Dict[int, str]               = {}
        self._canvas_trail_line_ids:   Dict[int, int]               = {}
        self._table_pending:           List[tuple]                   = []   # (row, tags, errs)
        self._table_flush_pending:     bool                         = False
        self._table_row_seq:           int                          = 0    # 斑馬紋交錯計數
        self._canvas_flush_pending:    bool                         = False
        self._canvas_dirty_keys:       set                          = set()  # 有新資料的 cid
        self._canvas_trail_reset_keys: set                          = set()  # 需清除舊軌跡線
        self._canvas_circle_del_keys:  set                          = set()  # 需刪除圓圈

        # Error-detection state
        self._scan_time_delta:  int                      = 0   # delta of last scan time change
        self._scan_delta_suppress: bool                  = False  # 觸控中斷後下一個 frame 的 Δ 不列入錯誤
        self._error_count:      int                      = 0

        # Record / replay state（監聽回放）
        self._record_buf:       collections.deque        = collections.deque(maxlen=self._RECORD_MAX)
        self._replay_active:    bool                     = False
        self._replay_data:      List[dict]               = []
        self._replay_idx:       int                      = 0
        self._replay_wall0:     float                    = 0.0   # 回放開始的牆鐘時間
        self._replay_t0:        float                    = 0.0   # 對應的虛擬 rx_time 起點
        self._replay_after_id:  Optional[str]            = None
        self._replay_speed:     float                    = 1.0
        self._replay_paused_at: int                      = 0
        self._replay_sync:      bool                     = False  # 程式設定滑桿時，忽略 on_scale
        self._replay_btns:      List                     = []     # 監聽/畫布分頁的回放按鈕（同步）
        self._replay_scales:    List                     = []     # 監聽/畫布分頁的時間軸滑桿（同步）

        # Command device (separate from monitor device)
        self._cmd_dev:           Optional[dict] = None  # None = use _selected_dev

        # Stress test state
        self._stress_running:          bool          = False
        self._stress_count:            int           = 0
        self._stress_fail_count:       int           = 0
        self._stress_start_time:       float         = 0.0
        self._stress_tip_active:       bool          = False
        self._stress_touch_had_no_conf: bool         = False
        self._stress_pending:          bool          = False
        self._stress_delay_id:         Optional[str] = None
        self._stress_poll_id:          Optional[str] = None
        self._stress_records:          List[dict]    = []

        # Heatmap tab state
        self._hm_frames:        List           = []
        self._hm_used_tx:       Optional[int]  = None
        self._hm_cur_frame:     int            = 0
        self._hm_lut:           List           = heatmap_frame.build_lut("seismic")
        self._hm_worker:        Optional[threading.Thread] = None
        self._hm_cancel:        Optional[threading.Event]  = None
        self._hm_progress_q:    queue.Queue    = queue.Queue()
        self._hm_drain_id:      Optional[str]  = None
        self._hm_output_files:  List[str]      = []
        self._hm_redraw_pending: bool          = False
        self._hm_playing:       bool           = False
        self._hm_play_id:       Optional[str]  = None

        # DigiInfo XML 軌跡分頁 state
        self._digi_frames:      List[dict]     = []
        self._digi_wide_rows:   List[dict]     = []
        self._digi_wide_cols:   List[str]      = []
        self._digi_long_rows:   List[dict]     = []
        self._digi_long_cols:   List[str]      = []
        self._digi_stats:       dict           = {}
        self._digi_cur:         int            = 0
        self._digi_start:       int            = 0   # 起始幀（之前的不顯示）
        self._digi_bounds_touch: Tuple[float, float, float, float] = (0.0, 1.0, 0.0, 1.0)
        self._digi_bounds_pen:   Tuple[float, float, float, float] = (0.0, 1.0, 0.0, 1.0)
        self._digi_playing:     bool           = False
        self._digi_play_id:     Optional[str]  = None
        self._digi_redraw_pending: bool        = False
        self._digi_table_visible:  bool        = False
        self._digi_render_token:   int         = 0

        self._build_ui()
        self._refresh_devices()
        self.after(20, self._poll_queue)
        self.after(50, self._startup_finalize)   # 收合側欄 + 預熱分頁 + 顯示視窗

        # 裝置熱插拔偵測
        self._devchange_after_id: Optional[str] = None
        self._devchange_thread = DeviceChangeThread(self._on_device_change_event)
        self._devchange_thread.start()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _setup_style(self):
        # Windows 11 風主題；大部分元件外觀交給 sv-ttk
        sv_ttk.set_theme("light")
        self._unify_font_family()       # 把主題字體字族統一成微軟正黑體（中英一致、不再 fallback 醜襯線體）
        self._capture_sv_font_sizes()   # 記下 sv_ttk 原始字體大小（供 DPI 縮放）
        self.option_add("*Font", self._FONT_UI)   # 影響 tk（非 ttk）元件

        style = ttk.Style(self)

        # 分頁標籤粗體
        style.configure("TNotebook.Tab", font=self._FONT_UI_BOLD)

        # 表格：加大列高 + 等寬字體（顏色/邊框交給 sv-ttk）
        style.configure("Treeview", rowheight=26)
        style.configure("Mono.Treeview", font=self._FONT_MONO)

        # 語意性文字顏色（ttk Label 的 foreground 在 sv-ttk 下可套用）
        style.configure("Muted.TLabel",   foreground=self._TEXT_MUTED)
        style.configure("FieldLabel.TLabel", foreground=self._TEXT_MUTED, font=self._FONT_UI_BOLD)
        style.configure("TopTitle.TLabel", font=("Microsoft JhengHei UI", 14, "bold"))
        style.configure("TopMuted.TLabel", foreground=self._TEXT_MUTED)
        style.configure("Version.TLabel",  foreground=self._TEXT_MUTED, font=("Consolas", 9))
        style.configure("Status.TLabel",   foreground=self._TEXT)
        style.configure("StatusRate.TLabel",  foreground="#3a3a3a", font=("Consolas", 9, "bold"))
        style.configure("StatusError.TLabel", foreground=self._RED,  font=("Consolas", 11, "bold"))

        # 頂部彩色小晶片
        style.configure("TopChip.TLabel",    background="#eaf2fb", foreground="#0067c0",
                        font=("Consolas", 9, "bold"), padding=(8, 2))
        style.configure("TopChipOk.TLabel",  background="#e7f6ec", foreground="#1e7a34",
                        font=("Consolas", 9, "bold"), padding=(8, 2))
        style.configure("TopChipErr.TLabel", background="#fdeaea", foreground="#c92a2a",
                        font=("Consolas", 9, "bold"), padding=(8, 2))

        # sv_ttk 像素字體 + Treeview 列高依目前 DPI 放大
        self._apply_font_scaling()

    def _mk_color_button(self, parent, text, command, color, color_dark):
        """彩色扁平按鈕（sv-ttk 的 ttk 按鈕吃不到背景色，故用 tk.Button）。"""
        return tk.Button(
            parent, text=text, command=command,
            bg=color, fg="white", activebackground=color_dark, activeforeground="white",
            relief=tk.FLAT, bd=0, highlightthickness=0,
            font=self._FONT_UI_BOLD, padx=16, pady=5, cursor="hand2",
        )

    def _style_log_text(self, widget):
        """統一 ScrolledText 外觀。"""
        widget.configure(bg=self._SURFACE, relief=tk.FLAT,
                         highlightthickness=1, highlightbackground=self._BORDER,
                         highlightcolor=self._BORDER, padx=6, pady=4)

    def _show_about(self, _event=None):
        dlg = tk.Toplevel(self)
        dlg.title(f"About {self._APP_NAME}")
        dlg.transient(self)
        dlg.resizable(False, False)
        try:
            dlg.configure(bg=self._BG)
        except tk.TclError:
            pass
        frm = ttk.Frame(dlg, padding=(20, 16))
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=self._APP_NAME, font=self._FONT_UI_BOLD).pack(anchor="w")
        ttk.Label(
            frm,
            text=(f"{self._APP_VERSION_LABEL} ({self._APP_VERSION_TIME})\n"
                  f"Edition: {self._EDITION}\n"
                  f"Author: {self._APP_AUTHOR}"),
            justify="left",
        ).pack(anchor="w", pady=(6, 14))
        btn_row = ttk.Frame(frm)
        btn_row.pack(fill=tk.X)
        chk = ttk.Button(
            btn_row, text="檢查更新",
            command=lambda: self._check_update_async(silent=False, parent=dlg),
        )
        chk.pack(side=tk.LEFT)
        if not updater.is_frozen():
            chk.state(["disabled"])   # 開發模式（非打包 exe）不做自我替換
        ttk.Button(btn_row, text="關閉", command=dlg.destroy).pack(side=tk.RIGHT)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()

    # ------------------------------------------------------------------
    # 自動更新（GitHub Releases）
    # ------------------------------------------------------------------
    def _check_update_async(self, silent: bool = True, parent=None):
        """背景查最新版。silent=True 時只有「有新版」才跳窗；False 會回報結果。"""
        if not updater.is_frozen() and not silent:
            messagebox.showinfo("檢查更新", "開發模式（未打包）不支援自動更新。", parent=parent or self)
            return
        if getattr(self, "_update_checking", False):
            return
        self._update_checking = True

        def worker():
            try:
                info = updater.check_latest(self._APP_VERSION_LABEL, self._EDITION)
                err = None
            except Exception as e:           # 離線 / 逾時 / 403 等一律安靜處理
                info, err = None, e
            self.after(0, lambda: self._on_update_result(info, err, silent, parent))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_result(self, info, err, silent, parent):
        self._update_checking = False
        parent = parent if (parent and parent.winfo_exists()) else self
        if err is not None:
            if not silent:
                messagebox.showwarning(
                    "檢查更新", f"無法連線到更新伺服器：\n{err}", parent=parent)
            return
        if not info:
            if not silent:
                messagebox.showinfo(
                    "檢查更新", f"目前已是最新版本（{self._APP_VERSION_LABEL}）。", parent=parent)
            return
        notes = info.get("notes", "")
        if len(notes) > 600:
            notes = notes[:600] + "…"
        msg = (f"發現新版本：{info['version']}\n"
               f"目前版本：{self._APP_VERSION_LABEL}\n\n"
               f"{notes}\n\n是否立即下載並更新？")
        if messagebox.askyesno("有可用更新", msg, parent=parent):
            self._do_update(info, parent)

    def _do_update(self, info, parent):
        """下載新版 exe（進度視窗）→ 驗證 → 自我替換重啟。"""
        dlg = tk.Toplevel(self)
        dlg.title("下載更新")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)   # 下載中不可關
        frm = ttk.Frame(dlg, padding=(20, 16))
        frm.pack(fill=tk.BOTH, expand=True)
        status = tk.StringVar(value=f"正在下載 {info['version']} …")
        ttk.Label(frm, textvariable=status).pack(anchor="w")
        bar = ttk.Progressbar(frm, length=320, mode="determinate", maximum=100)
        bar.pack(fill=tk.X, pady=(10, 0))
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()

        def progress(done, total):
            pct = (done * 100 // total) if total else 0
            self.after(0, lambda: (bar.configure(value=pct),
                                   status.set(f"正在下載 {info['version']} … {pct}%")))

        def worker():
            dest = updater.staged_path()
            try:
                sha = updater.download(info["url"], dest, progress)
                if not updater.verify_digest(sha, info.get("digest", "")):
                    raise ValueError("檔案校驗失敗（sha256 不符），已中止更新。")
            except Exception as e:
                try:
                    if os.path.exists(updater.staged_path()):
                        os.remove(updater.staged_path())
                except OSError:
                    pass
                self.after(0, lambda: (dlg.destroy(),
                                       messagebox.showerror("更新失敗", str(e), parent=parent
                                                            if parent.winfo_exists() else self)))
                return
            # 下載完成 → 套用（會結束本行程並重啟新版）
            self.after(0, lambda: self._apply_update(dlg, dest, info))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update(self, dlg, dest, info):
        try:
            dlg.destroy()
        except tk.TclError:
            pass
        messagebox.showinfo("更新就緒", "下載完成，將關閉並重新啟動新版本。", parent=self)
        # 寫入更新標記，重啟後的新版會讀取並顯示「更新內容」
        updater.apply_update(dest, info.get("version", ""), info.get("notes", ""))   # 不返回

    def _show_post_update_notes(self):
        """若本次啟動是剛從自動更新重啟，跳出「更新完成」視窗顯示 release notes。"""
        info = updater.read_update_marker()
        if not info:
            return
        updater.clear_update_marker()   # 只顯示一次
        # 版本不符（過期標記）就不顯示
        if updater.parse_version(info.get("version", "")) != \
                updater.parse_version(self._APP_VERSION_LABEL):
            return
        notes = (info.get("notes") or "").strip() or "（無更新說明）"
        dlg = tk.Toplevel(self)
        dlg.title("更新完成")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=(20, 16))
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=f"已更新到 {self._APP_VERSION_LABEL}",
                  font=self._FONT_UI_BOLD).pack(anchor="w")
        txt = tk.Text(frm, width=46, height=min(14, notes.count("\n") + 3),
                      wrap="word", relief=tk.FLAT, bd=0, padx=2, pady=6)
        self._style_log_text(txt)
        txt.insert("1.0", notes)
        txt.configure(state="disabled")
        txt.pack(fill=tk.BOTH, expand=True, pady=(8, 12))
        ttk.Button(frm, text="知道了", command=dlg.destroy).pack(anchor="e")
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()

    def _warmup_tabs(self):
        """啟動時預先渲染每個分頁（含回放子分頁），把 sv-ttk 首次繪製成本一次
        付清，之後切頁不再卡頓。"""
        try:
            nb = self._notebook
            for t in nb.tabs():
                nb.select(t)
                self.update_idletasks()
            sub = getattr(self, "_replay_nb", None)
            if sub is not None:
                for st in sub.tabs():
                    sub.select(st)
                    self.update_idletasks()
                sub.select(sub.tabs()[0])
            nb.select(nb.tabs()[0])   # 回到監聽分頁
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 登入（單一帳密；密碼以 SHA-256 雜湊存放）
    # ------------------------------------------------------------------
    _LOGIN_USER    = "RE024"
    _LOGIN_PW_HASH = "b2b6556e1a83512fed0d4d1ff24c996e44494cd552132dd887058a8df77f3c03"
    _LOGIN_MAX_TRY = 5

    def _do_login(self) -> bool:
        """開啟前的登入視窗。成功回 True；失敗 / 關閉回 False。"""
        dlg = tk.Toplevel(self)
        dlg.title("登入")
        dlg.resizable(False, False)
        try:
            dlg.iconbitmap(self._icon_path) if getattr(self, "_icon_path", None) else None
        except Exception:
            pass

        state = {"ok": False, "tries": 0}
        frm = ttk.Frame(dlg, padding=(18, 14))
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=self._APP_NAME, style="FieldLabel.TLabel").grid(
            row=0, column=0, columnspan=2, pady=(0, 10))
        ttk.Label(frm, text="帳號").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        user_var = tk.StringVar()
        user_ent = ttk.Entry(frm, textvariable=user_var, width=22)
        user_ent.grid(row=1, column=1, pady=4)
        ttk.Label(frm, text="密碼").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
        pw_var = tk.StringVar()
        pw_ent = ttk.Entry(frm, textvariable=pw_var, width=22, show="●")
        pw_ent.grid(row=2, column=1, pady=4)
        msg_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=msg_var, foreground=self._RED).grid(
            row=3, column=0, columnspan=2, pady=(4, 0))

        def attempt(*_):
            u = user_var.get().strip()
            p = pw_var.get()
            ok = (u == self._LOGIN_USER and
                  hashlib.sha256(p.encode("utf-8")).hexdigest() == self._LOGIN_PW_HASH)
            if ok:
                state["ok"] = True
                dlg.destroy()
                return
            state["tries"] += 1
            pw_var.set("")
            left = self._LOGIN_MAX_TRY - state["tries"]
            if left <= 0:
                dlg.destroy()
            else:
                msg_var.set(f"帳號或密碼錯誤（剩 {left} 次）")
                pw_ent.focus_set()

        btn = ttk.Button(frm, text="登入", command=attempt)
        btn.grid(row=4, column=0, columnspan=2, pady=(12, 0), sticky="ew")
        dlg.bind("<Return>", attempt)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)   # 關閉視窗 = 失敗

        # 置中於螢幕
        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

        user_ent.focus_set()
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.wait_window()
        return state["ok"]

    def _startup_finalize(self):
        # 登入只在 Engineer 版要求；Customer/FAE 版免帳密
        if self._is_engineer() and not self._do_login():
            self.destroy()
            return
        # Report Descriptor 側欄預設收合
        if hasattr(self, "_desc_collapsed") and not self._desc_collapsed:
            self._toggle_desc_panel()
        # alpha=0 + deiconify 讓 sv-ttk 可以渲染（預熱），但使用者看不到
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.deiconify()
        self.update_idletasks()
        # 視窗顯示到實際螢幕後重算 DPI（開在非主螢幕時才正確）
        if self._current_dpi() != self._cur_dpi:
            self._apply_dpi_scaling()
        # 拖到不同 DPI 螢幕時動態重算字體
        self.bind("<Configure>", self._on_possible_dpi_change, add="+")
        # 預熱各分頁（此時視窗透明，不會閃爍）
        self._warmup_tabs()
        # 一切就緒，顯示視窗
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        # 自動更新：先清掉上次自我替換留下的殘檔
        updater.cleanup_old()
        # 若剛從更新重啟，顯示「更新內容」
        self.after(400, self._show_post_update_notes)
        # 背景靜默檢查新版
        self.after(3000, lambda: self._check_update_async(silent=True))

    def _build_ui(self):
        # ---- Top bar (shared) ----
        top = ttk.Frame(self, style="Top.TFrame", padding=(10, 8))
        top.pack(side=tk.TOP, fill=tk.X)

        self._top_rate_var = tk.StringVar(value="0 scan/s")
        self._top_error_var = tk.StringVar(value="ERR 0")
        self._top_record_var = tk.StringVar(value="REC 0")

        # Device command strip
        row1 = ttk.Frame(top, style="Top.TFrame")
        row1.pack(fill=tk.X)

        ttk.Label(row1, text="監聽裝置", style="FieldLabel.TLabel", width=8, anchor="w").pack(side=tk.LEFT)
        self._dev_var = tk.StringVar()
        self._dev_combo = ttk.Combobox(row1, textvariable=self._dev_var, width=58, state="readonly")
        self._dev_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 8))
        self._dev_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        self._dev_combo.bind("<Enter>", self._show_dev_tooltip)
        self._dev_combo.bind("<Motion>", self._move_dev_tooltip)
        self._dev_combo.bind("<Leave>", self._hide_dev_tooltip)
        self._dev_combo.bind("<ButtonPress-1>", self._hide_dev_tooltip)

        ttk.Label(row1, text="指令裝置", style="FieldLabel.TLabel", width=8, anchor="w").pack(side=tk.LEFT, padx=(8, 0))
        self._cmd_dev_var = tk.StringVar()
        self._cmd_dev_combo = ttk.Combobox(row1, textvariable=self._cmd_dev_var, width=42, state="readonly")
        self._cmd_dev_combo.pack(side=tk.LEFT, padx=(2, 8))
        self._cmd_dev_combo.bind("<<ComboboxSelected>>", self._on_cmd_device_selected)

        ttk.Button(row1, text="重新整理", command=self._refresh_devices).pack(side=tk.LEFT, padx=2)

        self._listen_btn = self._mk_color_button(
            row1, "開始監聽", self._toggle_listen, self._GREEN, self._GREEN_DARK)
        self._listen_btn.pack(side=tk.LEFT, padx=(8, 0))

        # 彩色晶片用 tk.Label（sv-ttk 的 ttk.Label 吃不到背景色）
        chip_row = ttk.Frame(top, style="Top.TFrame")
        chip_row.pack(fill=tk.X, pady=(4, 0))
        def _chip(var, bg, fg):
            return tk.Label(chip_row, textvariable=var, bg=bg, fg=fg,
                            font=("Consolas", 9, "bold"), padx=8, pady=2)
        _chip(self._top_record_var, "#eaf2fb", "#0067c0").pack(side=tk.RIGHT, padx=(6, 0))
        _chip(self._top_error_var,  "#fdeaea", "#c92a2a").pack(side=tk.RIGHT, padx=(6, 0))
        _chip(self._top_rate_var,   "#e7f6ec", "#1e7a34").pack(side=tk.RIGHT, padx=(6, 0))

        # ---- Status bar ----
        # 比 PanedWindow 先 pack：視窗高度不足時才不會被擠出畫面
        sb_frame = ttk.Frame(self, style="Status.TFrame", padding=(8, 3))
        sb_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self._status_var = tk.StringVar(value="就緒")
        ttk.Label(sb_frame, textvariable=self._status_var, style="Status.TLabel",
                  anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)

        version_label = ttk.Label(
            sb_frame,
            text=f"by {self._APP_AUTHOR}",
            style="Version.TLabel",
            cursor="hand2",
        )
        version_label.pack(side=tk.RIGHT, padx=(12, 0))
        version_label.bind("<Button-1>", self._show_about)

        self._error_var = tk.StringVar(value="")
        ttk.Label(sb_frame, textvariable=self._error_var, anchor=tk.E,
                  style="StatusError.TLabel", width=34).pack(side=tk.RIGHT, padx=(8, 4))

        # ---- Main PanedWindow ----
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashrelief=tk.FLAT,
                               sashwidth=6, bd=0, bg=self._BG)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._paned = paned
        self._desc_panel_width = 320   # remembered width when expanded

        # -- Left panel: collapsible descriptor panel --
        left_outer = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left_outer, minsize=0)

        # Toggle button row
        toggle_row = ttk.Frame(left_outer)
        toggle_row.pack(side=tk.TOP, fill=tk.X)
        self._desc_collapsed = False
        self._desc_toggle_btn = ttk.Button(
            toggle_row, text="◀ Report Descriptor",
            style="Toggle.TButton",
            command=self._toggle_desc_panel,
        )
        self._desc_toggle_btn.pack(fill=tk.X)

        # Inner content (tree + raw button) — hidden when collapsed
        self._desc_inner = ttk.Frame(left_outer)
        self._desc_inner.pack(fill=tk.BOTH, expand=True)

        left_frame = ttk.Frame(self._desc_inner, padding=(6, 6))
        left_frame.pack(fill=tk.BOTH, expand=True)

        self._desc_tree = ttk.Treeview(
            left_frame,
            columns=("bit_size", "logical_range"),
            show="tree headings",
        )
        self._desc_tree.heading("#0",            text="欄位 / 名稱")
        self._desc_tree.heading("bit_size",      text="位元大小")
        self._desc_tree.heading("logical_range", text="Logical 範圍")
        self._desc_tree.column("#0",            width=self._sx(200), stretch=True)
        self._desc_tree.column("bit_size",      width=self._sx(70),  stretch=False)
        self._desc_tree.column("logical_range", width=self._sx(100), stretch=False)

        ttk.Button(left_frame, text="原始 Descriptor Bytes",
                   command=self._show_raw_descriptor).pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

        desc_sb = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self._desc_tree.yview)
        self._desc_tree.configure(yscrollcommand=desc_sb.set)
        desc_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._desc_tree.pack(fill=tk.BOTH, expand=True)

        self._desc_tree.tag_configure("vendor", foreground="#b45309")
        self._desc_tree.tag_configure("const",  foreground="#a1a1aa")

        # -- Right panel: Notebook --
        right_frame = ttk.Frame(paned, style="Surface.TFrame")
        paned.add(right_frame, minsize=560)

        self._notebook = ttk.Notebook(right_frame)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        monitor_tab = ttk.Frame(self._notebook, style="Surface.TFrame")
        self._notebook.add(monitor_tab, text="監聽")
        self._build_monitor_tab(monitor_tab)

        # 發送 / 壓測 / 回放分頁僅 Engineer 版（FAE/Customer 隱藏）
        if self._is_engineer():
            send_tab = ttk.Frame(self._notebook, style="Surface.TFrame")
            self._notebook.add(send_tab, text="發送")
            self._build_send_tab(send_tab)

            stress_tab = ttk.Frame(self._notebook, style="Surface.TFrame")
            self._notebook.add(stress_tab, text="壓測")
            self._build_stress_tab(stress_tab)

            # 回放分頁：內含 Differ / DigiInfo 兩個子分頁
            replay_tab = ttk.Frame(self._notebook, style="Surface.TFrame")
            self._notebook.add(replay_tab, text="回放")
            replay_nb = ttk.Notebook(replay_tab)
            replay_nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
            self._replay_nb = replay_nb

            heatmap_sub = ttk.Frame(replay_nb, style="Surface.TFrame")
            replay_nb.add(heatmap_sub, text="Differ")
            self._build_heatmap_tab(heatmap_sub)

            digi_sub = ttk.Frame(replay_nb, style="Surface.TFrame")
            replay_nb.add(digi_sub, text="DigiInfo")
            self._build_digi_tab(digi_sub)

            # 切換分頁時，自動暫停已切走的回放（Differ / DigiInfo）
            self._replay_nb.bind("<<NotebookTabChanged>>", self._on_tab_changed, add="+")

        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed, add="+")

    def _on_tab_changed(self, event=None):
        """切換主分頁或回放子分頁時，把已切走（不再顯示）的回放暫停。"""
        differ_visible = digi_visible = False
        try:
            if self._notebook.tab(self._notebook.select(), "text") == "回放":
                sub = self._replay_nb.tab(self._replay_nb.select(), "text")
                differ_visible = (sub == "Differ")
                digi_visible   = (sub == "DigiInfo")
        except Exception:
            pass
        if not differ_visible and self._hm_playing:
            self._hm_stop_play()
        if not digi_visible and self._digi_playing:
            self._digi_stop_play()

    def _build_monitor_tab(self, parent):
        ctrl_box = ttk.LabelFrame(parent, text="監聽顯示與篩選", padding=(8, 6),
                                  style="Section.TLabelframe")
        ctrl_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4))
        display_row = ttk.Frame(ctrl_box)
        display_row.pack(side=tk.TOP, fill=tk.X)
        advanced_row = ttk.Frame(ctrl_box)
        advanced_row.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        self._show_raw    = tk.BooleanVar(value=False)
        self._view_mode   = tk.StringVar(value="Hybrid")

        ttk.Label(display_row, text="Report ID:").pack(side=tk.LEFT)
        self._rid_filter_var = tk.StringVar(value="全部")
        self._rid_combo = ttk.Combobox(display_row, textvariable=self._rid_filter_var,
                                       width=8, state="readonly")
        self._rid_combo["values"] = ["全部"]
        self._rid_combo.current(0)
        self._rid_combo.pack(side=tk.LEFT, padx=(2, 12))
        self._rid_combo.bind("<<ComboboxSelected>>", lambda _: self._rebuild_table_columns())

        ttk.Label(display_row, text="View:").pack(side=tk.LEFT, padx=(8, 0))
        self._view_combo = ttk.Combobox(display_row, textvariable=self._view_mode,
                                        width=10, state="readonly",
                                        values=("Hybrid", "Parallel"))
        self._view_combo.pack(side=tk.LEFT, padx=(2, 8))
        self._view_combo.bind("<<ComboboxSelected>>", lambda _: self._rebuild_table_columns())

        ttk.Checkbutton(display_row, text="顯示 RAW 欄位",
                        variable=self._show_raw,
                        command=self._rebuild_table_columns).pack(side=tk.LEFT, padx=8)
        self._canvas_toggle_btn = ttk.Button(display_row, text="顯示畫布 ▶",
                                             command=self._toggle_monitor_canvas)
        self._canvas_toggle_btn.pack(side=tk.LEFT, padx=8)
        ttk.Button(display_row, text="清除", command=self._clear_monitor_all).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(display_row, text="匯出 Excel", command=self._export_monitor_to_excel).pack(side=tk.RIGHT, padx=4)

        ttk.Label(advanced_row, text="Frame gap(ms):").pack(side=tk.LEFT)
        self._gap_ms_var = tk.StringVar(value="4")
        ttk.Spinbox(advanced_row, from_=1, to=50, textvariable=self._gap_ms_var,
                    width=4).pack(side=tk.LEFT, padx=(2, 16))

        ttk.Label(advanced_row, text="最大 scan Δ:").pack(side=tk.LEFT)
        self._max_scan_delta_var = tk.StringVar(value="200")
        ttk.Spinbox(advanced_row, from_=0, to=9999, textvariable=self._max_scan_delta_var,
                    width=5).pack(side=tk.LEFT, padx=(2, 16))

        ttk.Label(advanced_row, text="保留筆數:").pack(side=tk.LEFT)
        self._max_rows_var = tk.StringVar(value="200")
        ttk.Spinbox(advanced_row, from_=50, to=5000, increment=50,
                    textvariable=self._max_rows_var,
                    width=5).pack(side=tk.LEFT, padx=(2, 8))

        # FF01 usage filter row
        self._ff01_filter_frame = ttk.LabelFrame(parent, text="Usage Page FF01 欄位顯示",
                                                 padding=(6, 3), style="Section.TLabelframe")
        self._ff01_filter_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))

        # 格式選擇器永久固定在右側
        fmt_right = ttk.Frame(self._ff01_filter_frame)
        fmt_right.pack(side=tk.RIGHT, padx=4, pady=1)
        ttk.Label(fmt_right, text="格式:").pack(side=tk.LEFT)
        fmt_combo = ttk.Combobox(
            fmt_right, textvariable=self._ff01_fmt,
            values=("Hex", "Dec", "Bin"), state="readonly", width=5,
        )
        fmt_combo.pack(side=tk.LEFT, padx=2)
        fmt_combo.bind("<<ComboboxSelected>>", lambda _: self._rebuild_table_columns())

        # 動態 checkbox 區域（每次換裝置時重建）
        self._ff01_check_frame = ttk.Frame(self._ff01_filter_frame)
        self._ff01_check_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 回放控制列
        self._replay_speed_var = tk.StringVar(value="1x")
        self._replay_pos_var = tk.StringVar(value="0 / 0")
        self._record_status_var = tk.StringVar(value="錄製 0")
        replay_row = ttk.LabelFrame(parent, text="錄製回放", padding=(8, 5),
                                    style="Section.TLabelframe")
        replay_row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))
        self._build_replay_controls(replay_row)

        # 表格 | 畫布 水平分割（畫布預設收合，可由「顯示畫布」展開）
        self._monitor_split = tk.PanedWindow(parent, orient=tk.HORIZONTAL,
                                             sashrelief=tk.FLAT, sashwidth=6, bd=0,
                                             bg=self._BG)
        self._monitor_split.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        tbl_frame = ttk.LabelFrame(self._monitor_split, text="Monitor Data",
                                   padding=(4, 4), style="Section.TLabelframe")
        self._monitor_split.add(tbl_frame, minsize=300, stretch="always")

        # 全裝置模式：每個來源裝置（各觸控裝置與筆）各自的 scan/s（單一裝置模式隱藏）
        self._digi_rate_var = tk.StringVar(value="")
        self._digi_rate_lbl = ttk.Label(tbl_frame, textvariable=self._digi_rate_var,
                                        style="Muted.TLabel", anchor="w", font=self._FONT_MONO)

        self._tbl_wrap = ttk.Frame(tbl_frame)
        self._tbl_wrap.pack(fill=tk.BOTH, expand=True)
        self._table = ttk.Treeview(self._tbl_wrap, show="headings", selectmode="browse",
                                   style="Mono.Treeview")
        vsb = ttk.Scrollbar(self._tbl_wrap, orient=tk.VERTICAL,   command=self._table.yview)
        hsb = ttk.Scrollbar(self._tbl_wrap, orient=tk.HORIZONTAL, command=self._table.xview)
        self._table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._table.pack(fill=tk.BOTH, expand=True)
        self._table.tag_configure("scan_error", background="#ffd6d6")
        self._table.tag_configure("stripe", background=self._STRIPE)

        # 畫布面板（建立但預設不加入分割視窗 = 收合）
        self._canvas_panel = ttk.LabelFrame(self._monitor_split, text="Touch Canvas",
                                            padding=(4, 4), style="Section.TLabelframe")
        self._canvas_shown = False
        self._build_canvas_panel(self._canvas_panel)


    def _build_send_tab(self, parent):
        pad = {"padx": 8, "pady": 6}

        param_frame = ttk.LabelFrame(parent, text="發送參數", padding=8,
                                     style="Section.TLabelframe")
        param_frame.pack(fill=tk.X, padx=8, pady=(8, 4))
        param_frame.columnconfigure(1, weight=1)

        ttk.Label(param_frame, text="Report 類型:").grid(row=0, column=0, sticky="w", padx=4)
        self._report_type = tk.StringVar(value="Output")
        self._report_type.trace_add("write", self._on_report_type_changed)
        rb_frame = ttk.Frame(param_frame)
        rb_frame.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(rb_frame, text="Output",  variable=self._report_type, value="Output").pack(side=tk.LEFT)
        ttk.Radiobutton(rb_frame, text="Feature", variable=self._report_type, value="Feature").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(rb_frame, text="Input",   variable=self._report_type, value="Input").pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(param_frame, text="Report ID (hex):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self._report_id_var = tk.StringVar(value="01")
        ttk.Entry(param_frame, textvariable=self._report_id_var, width=10).grid(
            row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(param_frame, text="Data (hex):").grid(row=2, column=0, sticky="w", padx=4)
        self._send_data_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self._send_data_var, width=55).grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(0, 4))

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Button(btn_row, text="發送 (Set Report)", command=self._on_send).pack(side=tk.LEFT, padx=4)
        self._get_btn = ttk.Button(btn_row, text="Get Report", command=self._on_get_report)
        # 初始隱藏，切到 Feature 才顯示

        int_row = ttk.Frame(parent)
        int_row.pack(fill=tk.X, padx=12, pady=(0, 6))
        self._wait_int_var = tk.BooleanVar(value=False)
        self._wait_int_cb = ttk.Checkbutton(int_row, text="等待 INT 回應", variable=self._wait_int_var)
        self._wait_int_cb.pack(side=tk.LEFT)
        ttk.Label(int_row, text="Timeout (ms):").pack(side=tk.LEFT, padx=(12, 2))
        self._int_timeout_var = tk.StringVar(value="1000")
        self._int_timeout_spin = ttk.Spinbox(int_row, from_=50, to=10000,
                                              textvariable=self._int_timeout_var, width=6)
        self._int_timeout_spin.pack(side=tk.LEFT)
        ttk.Label(int_row, text="Length (payload bytes):").pack(side=tk.LEFT, padx=(12, 2))
        self._int_length_var = tk.StringVar(value="63")
        self._int_length_entry = ttk.Entry(int_row, textvariable=self._int_length_var, width=6)
        self._int_length_entry.pack(side=tk.LEFT)
        # 「等待 INT 回應」只在 Report 類型 = Output 時可用
        self._on_report_type_changed()

        log_frame = ttk.LabelFrame(parent, text="操作記錄", padding=6,
                                   style="Section.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        # 保留變數（預設關閉），相關顯示邏輯不會啟用
        self._pair_bytes_var = tk.BooleanVar(value=False)
        # 「2-byte 合併顯示」功能暫時隱藏，不刪除；要恢復把下面三行取消註解即可
        # log_ctrl = ttk.Frame(log_frame)
        # log_ctrl.pack(fill=tk.X, pady=(0, 4))
        # ttk.Checkbutton(log_ctrl, text="2-byte 合併顯示（第一行32個，其他33個）",
        #                 variable=self._pair_bytes_var).pack(side=tk.LEFT)

        self._send_log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", font=("Consolas", 9))
        self._style_log_text(self._send_log)
        self._send_log.pack(fill=tk.BOTH, expand=True)

        ttk.Button(parent, text="清除記錄", command=self._clear_send_log).pack(anchor=tk.E, padx=8, pady=(0, 8))

    # ------------------------------------------------------------------
    # Shared Device Management
    # ------------------------------------------------------------------

    _CMD_SAME_LABEL = "（同監聽裝置）"

    def _refresh_devices(self, auto: bool = False):
        """重新列舉 HID 裝置。auto=True 表示由熱插拔事件觸發：
        保留現有選擇與 descriptor 快取，監聽中的 session 不受影響。"""
        prev_path     = self._get_dev_path_str(self._selected_dev) if self._selected_dev else None
        prev_cmd_path = self._get_dev_path_str(self._cmd_dev) if self._cmd_dev else None

        if not auto:
            self._descriptors.clear()
            self._raw_descriptors.clear()
        self._hidapi_devices = sorted(
            enumerate_hid_devices(),
            key=lambda d: (d.get("vendor_id", 0), d.get("product_id", 0), device_collection(d)),
        )
        labels = [format_device_label(d) for d in self._hidapi_devices]
        paths  = [self._get_dev_path_str(d) for d in self._hidapi_devices]
        # 第一個是「全部 digitizer」哨兵，其餘裝置往後位移 1
        self._dev_combo["values"] = [self._ALL_DIGI_LABEL] + labels
        self._hide_dev_tooltip()
        if self._all_digi_mode:
            self._dev_combo.current(0)            # 維持全部 digitizer 模式
        elif labels and prev_path in paths:
            idx = paths.index(prev_path)
            self._dev_combo.current(idx + 1)
            self._selected_dev = self._hidapi_devices[idx]
            if not auto:
                self._on_device_selected(None)   # 手動重整：重新載入 descriptor
        else:
            # 預設選「全部 digitizer」（沒有先前選擇 / 沒有裝置時）
            self._dev_combo.current(0)
            self._on_device_selected(None)
            self._selected_dev = None

        cmd_labels = [self._CMD_SAME_LABEL] + labels
        self._cmd_dev_combo["values"] = cmd_labels
        if prev_cmd_path in paths:
            cmd_idx = paths.index(prev_cmd_path)
            self._cmd_dev_combo.current(cmd_idx + 1)
            self._cmd_dev = self._hidapi_devices[cmd_idx]
        else:
            self._cmd_dev_combo.current(0)
            self._cmd_dev = None

        prefix = "偵測到裝置變更，" if auto else ""
        self._status_var.set(f"{prefix}找到 {len(self._hidapi_devices)} 個 HID 裝置")

        if self._all_digi_mode and self._listening:
            self._preload_digi_ctx()   # 監聽中裝置清單變更才重新預載（啟動時不做，避免卡頓）

    # ------------------------------------------------------------------
    # Hot-plug detection
    # ------------------------------------------------------------------

    def _on_device_change_event(self, event: str):
        """從 DeviceChangeThread 執行緒呼叫，轉回主執行緒。"""
        self.after(0, self._schedule_device_refresh)

    def _schedule_device_refresh(self):
        """去抖動：插拔常連續觸發多筆事件，等 600ms 沒有新事件再重整。"""
        if self._devchange_after_id is not None:
            self.after_cancel(self._devchange_after_id)
        self._devchange_after_id = self.after(600, self._do_device_refresh)

    def _do_device_refresh(self):
        self._devchange_after_id = None
        self._refresh_devices(auto=True)

    def _on_device_selected(self, event):
        idx = self._dev_combo.current()
        was_all = self._all_digi_mode
        if idx == 0:
            # 「全部 digitizer」：不選裝置、自動解碼所有 digitizer
            self._enter_all_digi_mode()
            self._adigi_entries = []          # 預載延後到開始監聽時才做，避免啟動卡頓
            self._hide_dev_tooltip()
            # 監聽中切入：重啟以註冊完整 digitizer TLC（含 Pen/Stylus）
            if self._listening:
                self._stop_listen()
                self._start_listen()
            return
        self._all_digi_mode = False
        dev_idx = idx - 1   # 哨兵位移
        if dev_idx < 0 or dev_idx >= len(self._hidapi_devices):
            return
        self._selected_dev = self._hidapi_devices[dev_idx]
        self._hide_dev_tooltip()
        if was_all:
            self._digi_rate_lbl.pack_forget()
            self._digi_rate_var.set("")
            self._digi_rate_deques.clear()
            self._clear_digi_canvas()
            self._reset_monitor_runtime_state()
        self._load_descriptor(self._selected_dev)

    def _on_cmd_device_selected(self, event):
        idx = self._cmd_dev_combo.current()
        if idx <= 0:
            self._cmd_dev = None  # same as monitor device
        else:
            dev_idx = idx - 1  # offset by 1 because of "（同監聽裝置）"
            if dev_idx < len(self._hidapi_devices):
                self._cmd_dev = self._hidapi_devices[dev_idx]

    def _get_cmd_dev(self) -> Optional[dict]:
        """Return the device to use for sending commands."""
        return self._cmd_dev if self._cmd_dev is not None else self._selected_dev

    def _get_selected_device_label(self) -> str:
        idx = self._dev_combo.current()
        if idx == 0:
            return self._ALL_DIGI_LABEL
        dev_idx = idx - 1   # 哨兵位移
        if 0 <= dev_idx < len(self._hidapi_devices):
            return format_device_label(self._hidapi_devices[dev_idx])
        return self._dev_var.get().strip()

    def _show_dev_tooltip(self, event=None):
        text = self._get_selected_device_label()
        if not text:
            return
        if self._dev_tooltip is None or not self._dev_tooltip.winfo_exists():
            self._dev_tooltip = tk.Toplevel(self)
            self._dev_tooltip.wm_overrideredirect(True)
            self._dev_tooltip.attributes("-topmost", True)
            self._dev_tooltip_label = tk.Label(
                self._dev_tooltip,
                text=text,
                justify=tk.LEFT,
                anchor="w",
                bg="#fff8dc",
                fg="black",
                relief=tk.SOLID,
                borderwidth=1,
                padx=8,
                pady=4,
                font=("Consolas", 9),
            )
            self._dev_tooltip_label.pack()
        else:
            self._dev_tooltip_label.config(text=text)
        self._move_dev_tooltip(event)

    def _move_dev_tooltip(self, event=None):
        if not self._dev_tooltip or not self._dev_tooltip.winfo_exists():
            return
        if event is not None:
            x = event.x_root + 16
            y = event.y_root + 20
        else:
            x = self._dev_combo.winfo_rootx() + 16
            y = self._dev_combo.winfo_rooty() + self._dev_combo.winfo_height() + 4
        self._dev_tooltip.geometry(f"+{x}+{y}")

    def _hide_dev_tooltip(self, event=None):
        if self._dev_tooltip and self._dev_tooltip.winfo_exists():
            self._dev_tooltip.destroy()
        self._dev_tooltip = None
        self._dev_tooltip_label = None

    def _get_dev_path_str(self, dev: dict) -> str:
        path = dev.get("path", b"")
        if isinstance(path, bytes):
            return path.decode("utf-8", errors="replace")
        return str(path)

    @staticmethod
    def _dev_match_key(name: str) -> str:
        """把 hidapi path 與 RawInput device_name 正規化成同一條裝置介面路徑 key
        （取 HID# 之後、去掉結尾 interface GUID）。USB 與 I2C-HID 皆適用，不依賴 VID/PID。"""
        s = (name or "").lower()
        i = s.find("hid#")
        if i >= 0:
            s = s[i:]
        j = s.rfind("#{")          # 結尾的 {GUID}，所有 HID 介面都一樣
        if j >= 0:
            s = s[:j]
        return s

    def _load_descriptor(self, dev: dict):
        path_str = self._get_dev_path_str(dev)
        if path_str in self._descriptors:
            self._populate_desc_tree(path_str)
            return

        self._status_var.set(f"讀取 Descriptor: {path_str[:60]}...")

        def worker():
            raw = read_descriptor_via_hidapi(dev.get("path", b""))
            if raw:
                self._raw_descriptors[path_str] = raw
                try:
                    fields = parse_report_descriptor(raw)
                except Exception as e:
                    fields = []
                    self.after(0, lambda: self._status_var.set(f"Descriptor 解析錯誤: {e}"))
                self._descriptors[path_str] = fields
                self.after(0, lambda: self._populate_desc_tree(path_str))
            else:
                self._raw_descriptors[path_str] = b""
                self._descriptors[path_str] = []
                self.after(0, lambda: self._status_var.set("無法讀取 Descriptor（可能需要管理員權限）"))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_desc_tree(self, path_str: str):
        for item in self._desc_tree.get_children():
            self._desc_tree.delete(item)

        fields = self._descriptors.get(path_str, [])
        if not fields:
            self._desc_tree.insert("", tk.END, text="（無 Descriptor 資料）")
            return

        from collections import OrderedDict
        groups: Dict[int, Dict[str, List[HIDField]]] = OrderedDict()
        for f in fields:
            groups.setdefault(f.report_id, OrderedDict()).setdefault(f.report_type, []).append(f)

        for rid, types in groups.items():
            rid_node = self._desc_tree.insert("", tk.END, text=f"Report ID = {rid:#04x}", open=True)
            for rtype, flist in types.items():
                type_node = self._desc_tree.insert(rid_node, tk.END, text=rtype, open=True)
                for hf in flist:
                    tag = "const" if hf.is_const else ("vendor" if hf.is_vendor else "touch")
                    self._desc_tree.insert(
                        type_node, tk.END,
                        text=hf.label,
                        values=(hf.bit_size, f"[{hf.logical_min}, {hf.logical_max}]"),
                        tags=(tag,),
                    )

        count        = len(fields)
        vendor_count = sum(1 for f in fields if f.is_vendor)
        self._status_var.set(
            f"Descriptor 載入完成: {count} 個欄位（其中 {vendor_count} 個 Vendor 欄位）"
        )

        input_rids = sorted(set(
            f.report_id for f in fields
            if f.report_type == REPORT_TYPE_INPUT and not f.is_const
        ))
        self._rid_combo["values"] = ["全部"] + [f"0x{r:02X}" for r in input_rids]
        self._rid_filter_var.set(f"0x{input_rids[0]:02X}" if len(input_rids) == 1 else "全部")
        self._update_ff01_filter(path_str)
        self._update_canvas_range(path_str)
        self._rebuild_table_columns()

    # ------------------------------------------------------------------
    # FF01 usage filter
    # ------------------------------------------------------------------

    def _update_ff01_filter(self, path_str: str):
        """根據目前 descriptor 重建 FF01 usage 勾選面板。"""
        for widget in self._ff01_check_frame.winfo_children():
            widget.destroy()
        self._ff01_usage_vars.clear()

        fields = self._descriptors.get(path_str, [])
        seen: set = set()
        ff01_usages: List[int] = []
        for hf in fields:
            if hf.report_type != REPORT_TYPE_INPUT or hf.bit_size < 8:
                continue
            first_usage = hf.usages[0] if hf.usages else 0
            if hf.usage_page == 0xFF01 or (hf.usage_page, first_usage) in self._FF01_LIKE:
                if first_usage not in seen:
                    ff01_usages.append(first_usage)
                    seen.add(first_usage)

        if not ff01_usages:
            ttk.Label(self._ff01_check_frame, text="（無 FF01 欄位）",
                      style="Muted.TLabel").pack(side=tk.LEFT, padx=4)
            return

        for usage in ff01_usages:
            var = tk.BooleanVar(value=True)
            self._ff01_usage_vars[usage] = var
            cb = ttk.Checkbutton(
                self._ff01_check_frame,
                text=f"U{usage:02X}",
                variable=var,
                command=self._rebuild_table_columns,
            )
            cb.pack(side=tk.LEFT, padx=2, pady=1)

    # ------------------------------------------------------------------
    # Raw Descriptor viewer
    # ------------------------------------------------------------------

    def _toggle_desc_panel(self):
        if self._desc_collapsed:
            # Expand: restore saved width
            self._desc_inner.pack(fill=tk.BOTH, expand=True)
            self._paned.paneconfig(self._desc_inner.master, minsize=0)
            self._paned.sash_place(0, self._desc_panel_width, 0)
            self._desc_toggle_btn.config(text="◀ Report Descriptor")
            self._desc_collapsed = False
        else:
            # Collapse: remember current width then shrink to button only
            try:
                self._desc_panel_width = self._paned.sash_coord(0)[0]
            except Exception:
                pass
            self._desc_inner.pack_forget()
            self._paned.sash_place(0, 24, 0)
            self._desc_toggle_btn.config(text="▶")
            self._desc_collapsed = True

    def _show_raw_descriptor(self):
        path_str = self._get_dev_path_str(self._selected_dev) if self._selected_dev else ""
        raw = self._raw_descriptors.get(path_str)

        if raw is None:
            messagebox.showinfo("原始 Descriptor", "請先選擇裝置並等待 Descriptor 載入完成。")
            return
        if not raw:
            messagebox.showinfo("原始 Descriptor", "無法讀取 Descriptor（可能需要管理員權限）。")
            return

        win = tk.Toplevel(self)
        win.title("Report Descriptor Bytes")
        self._set_window_icon(win)
        win.geometry("820x560")
        win.minsize(600, 400)
        win.configure(bg=self._BG)

        # Toolbar
        tb = ttk.Frame(win)
        tb.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)
        ttk.Label(tb, text=f"共 {len(raw)} bytes").pack(side=tk.LEFT)
        ttk.Button(tb, text="複製 Hex", command=lambda: self._copy_to_clipboard(
            win, " ".join(f"{b:02X}" for b in raw)
        )).pack(side=tk.RIGHT, padx=4)
        ttk.Button(tb, text="複製 C Array", command=lambda: self._copy_to_clipboard(
            win, "{\n" + "".join(
                ("    " if i % 16 == 0 else "") +
                f"0x{b:02X}," +
                ("\n" if i % 16 == 15 else " ")
                for i, b in enumerate(raw)
            ).rstrip() + "\n}"
        )).pack(side=tk.RIGHT, padx=4)

        # Notebook with two views
        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # --- Hex view ---
        hex_frame = ttk.Frame(nb)
        nb.add(hex_frame, text="Hex 檢視")

        hex_text = scrolledtext.ScrolledText(
            hex_frame, font=("Consolas", 10), wrap=tk.NONE,
            state=tk.NORMAL, relief=tk.FLAT,
        )
        self._style_log_text(hex_text)
        hex_text.pack(fill=tk.BOTH, expand=True)

        # 16 bytes per line: offset | hex | ascii
        lines = []
        for offset in range(0, len(raw), 16):
            chunk = raw[offset:offset + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{offset:04X}:  {hex_part:<47}  {asc_part}")
        hex_text.insert(tk.END, "\n".join(lines))
        hex_text.config(state=tk.DISABLED)

        # --- Parsed view ---
        parsed_frame = ttk.Frame(nb)
        nb.add(parsed_frame, text="解析檢視")

        parsed_text = scrolledtext.ScrolledText(
            parsed_frame, font=("Consolas", 10), wrap=tk.NONE,
            state=tk.NORMAL, relief=tk.FLAT,
        )
        self._style_log_text(parsed_text)
        parsed_text.pack(fill=tk.BOTH, expand=True)
        parsed_text.tag_configure("offset", foreground="gray")
        parsed_text.tag_configure("main",   foreground="#0055cc", font=("Consolas", 10, "bold"))
        parsed_text.tag_configure("global", foreground="#007700")
        parsed_text.tag_configure("local",  foreground="#cc6600")
        parsed_text.tag_configure("unknown",foreground="red")

        self._populate_parsed_descriptor(parsed_text, raw)
        parsed_text.config(state=tk.DISABLED)

    @staticmethod
    def _copy_to_clipboard(win: tk.Toplevel, text: str):
        win.clipboard_clear()
        win.clipboard_append(text)

    @staticmethod
    def _populate_parsed_descriptor(text_widget, raw: bytes):
        """Parse and pretty-print a HID report descriptor into a Text widget."""
        _USAGE_PAGES = {
            0x01: "Generic Desktop", 0x02: "Simulation", 0x03: "VR",
            0x07: "Keyboard", 0x08: "LED", 0x09: "Button",
            0x0C: "Consumer", 0x0D: "Digitizer", 0x0F: "PID",
        }
        _MAIN_TAGS = {
            0x08: "Input", 0x09: "Output", 0x0A: "Collection",
            0x0B: "Feature", 0x0C: "End Collection",
        }
        _GLOBAL_TAGS = {
            0x00: "Usage Page", 0x01: "Logical Minimum", 0x02: "Logical Maximum",
            0x03: "Physical Minimum", 0x04: "Physical Maximum",
            0x05: "Unit Exponent", 0x06: "Unit",
            0x07: "Report Size", 0x08: "Report ID", 0x09: "Report Count",
            0x0A: "Push", 0x0B: "Pop",
        }
        _LOCAL_TAGS = {
            0x00: "Usage", 0x01: "Usage Minimum", 0x02: "Usage Maximum",
            0x03: "Designator Index", 0x04: "Designator Minimum",
            0x05: "Designator Maximum", 0x07: "String Index",
            0x08: "String Minimum", 0x09: "String Maximum", 0x0A: "Delimiter",
        }
        _COLLECTION_TYPES = {0: "Physical", 1: "Application", 2: "Logical", 3: "Report",
                             4: "Named Array", 5: "Usage Switch", 6: "Usage Modifier"}

        indent = 0
        i = 0
        while i < len(raw):
            b = raw[i]
            btype = (b >> 2) & 0x03
            btag  = (b >> 4) & 0x0F
            bsize = b & 0x03
            if bsize == 3:
                bsize = 4

            offset = i
            i += 1
            val_bytes = raw[i:i + bsize]
            i += bsize

            # Decode value
            if bsize == 0:
                val = 0
            elif bsize == 1:
                val = val_bytes[0]
            elif bsize == 2:
                val = int.from_bytes(val_bytes, "little")
            else:
                val = int.from_bytes(val_bytes, "little")

            hex_bytes = " ".join(f"{x:02X}" for x in [b] + list(val_bytes))
            offset_str = f"{offset:04X}: "

            if btype == 0:   # Main
                # End Collection de-dents before printing
                if btag == 0x0C:
                    indent = max(0, indent - 2)
                tag_name = _MAIN_TAGS.get(btag, f"Main({btag:#04x})")
                if btag == 0x0A:   # Collection
                    desc = f"{tag_name} ({_COLLECTION_TYPES.get(val, f'{val:#04x}')})"
                elif btag in (0x08, 0x09, 0x0B):  # Input/Output/Feature
                    flags = []
                    if val & 0x01: flags.append("Const")
                    else:          flags.append("Data")
                    if val & 0x02: flags.append("Variable")
                    else:          flags.append("Array")
                    if val & 0x04: flags.append("Relative")
                    desc = f"{tag_name} ({', '.join(flags)})"
                else:
                    desc = tag_name
                pad = " " * indent
                text_widget.insert(tk.END, offset_str, "offset")
                text_widget.insert(tk.END, f"{hex_bytes:<12}  {pad}{desc}\n", "main")
                if btag == 0x0A:   # Collection — indent after
                    indent += 2
            elif btype == 1:  # Global
                tag_name = _GLOBAL_TAGS.get(btag, f"Global({btag:#04x})")
                if btag == 0x00:   # Usage Page
                    up_name = _USAGE_PAGES.get(val)
                    desc = f"{tag_name}: {val:#06x}" + (f" ({up_name})" if up_name else
                                                         " (Vendor)" if val >= 0xFF00 else "")
                else:
                    sval = val if val < 0x8000 else val - 0x10000
                    desc = f"{tag_name}: {sval}"
                pad = " " * indent
                text_widget.insert(tk.END, offset_str, "offset")
                text_widget.insert(tk.END, f"{hex_bytes:<12}  {pad}{desc}\n", "global")
            elif btype == 2:  # Local
                tag_name = _LOCAL_TAGS.get(btag, f"Local({btag:#04x})")
                desc = f"{tag_name}: {val:#06x}"
                pad = " " * indent
                text_widget.insert(tk.END, offset_str, "offset")
                text_widget.insert(tk.END, f"{hex_bytes:<12}  {pad}{desc}\n", "local")
            else:
                text_widget.insert(tk.END, offset_str, "offset")
                text_widget.insert(tk.END, f"{hex_bytes:<12}  (Long item / unknown)\n", "unknown")

    # ------------------------------------------------------------------
    # Monitor: Table columns
    # ------------------------------------------------------------------

    # 比照 FF01 逐 byte 展開的欄位（非真的 FF01）：
    #   (0x09, 0xC5) 廠商資料、(0x0D, 0x1F) 手寫筆 debug 資訊
    _FF01_LIKE = {(0x09, 0xC5), (0x0D, 0x1F)}

    # 這些 usage 重複出現時代表「多個實例」（多接點 / 多軸），
    # 不可當成一個寬位元值合併（例如 Usage(X) Report Count 2 = 兩個 X）
    _MULTI_INSTANCE_USAGES = {
        (0x01, 0x30),  # X
        (0x01, 0x31),  # Y
        (0x01, 0x32),  # Z
        (0x01, 0x33),  # Rx
        (0x01, 0x34),  # Ry
        (0x01, 0x35),  # Rz
        (0x0D, 0x42),  # TipSwitch
        (0x0D, 0x47),  # Confidence
        (0x0D, 0x48),  # Width
        (0x0D, 0x49),  # Height
        (0x0D, 0x51),  # ContactID
    }

    @staticmethod
    def _is_byte_expanded_field(hf: HIDField) -> bool:
        """UP=0x09 且 bit_size >= 8 的欄位按 byte 展開顯示（排除 1-bit button 及 FF01-like 欄位）。"""
        if hf.usage_page != 0x09 or hf.bit_size < 8:
            return False
        first_usage = hf.usages[0] if hf.usages else 0
        return (0x09, first_usage) not in HIDToolApp._FF01_LIKE

    @staticmethod
    def _is_vendor_usage_byte_expanded_field(hf: HIDField) -> bool:
        if hf.bit_size < 8:
            return False
        if hf.usage_page == 0xFF01:
            return True
        first_usage = hf.usages[0] if hf.usages else 0
        return (hf.usage_page, first_usage) in HIDToolApp._FF01_LIKE

    @staticmethod
    def _is_combined_value_field(hf: HIDField) -> bool:
        if hf.is_vendor or hf.report_count <= 1 or hf.per_bit_size <= 0:
            return False
        if hf.usage_page == 0x09 or not hf.usages:
            return False
        # 座標 / 觸控等 usage 重複 = 多實例，不可合併成單一寬值
        if (hf.usage_page, hf.usages[0]) in HIDToolApp._MULTI_INSTANCE_USAGES:
            return False
        return all(u == hf.usages[0] for u in hf.usages[:hf.report_count])

    @staticmethod
    def _combine_field_parts(vals: List[int], per_bit_size: int, logical_min: int) -> int:
        total_bits = per_bit_size * len(vals)
        value = 0
        for idx, part in enumerate(vals):
            value |= (part & ((1 << per_bit_size) - 1)) << (idx * per_bit_size)
        if logical_min < 0 and total_bits > 0:
            sign_bit = 1 << (total_bits - 1)
            if value & sign_bit:
                value -= 1 << total_bits
        return value

    def _build_touch_usage_entries(
        self, input_fields: List[HIDField]
    ) -> Dict[str, List[Tuple[HIDField, int]]]:
        usage_keys = {
            "Confidence": (0x0D, 0x47),
            "TipSwitch": (0x0D, 0x42),
            "InRange": (0x0D, 0x32),
            "Invert": (0x0D, 0x3C),
            "Eraser": (0x0D, 0x45),
            "BarrelSwitch": (0x0D, 0x44),
            "ContactID": (0x0D, 0x51),
            "X": (0x01, 0x30),
            "Y": (0x01, 0x31),
            "Width": (0x0D, 0x48),
            "Height": (0x0D, 0x49),
        }
        entries: Dict[str, List[Tuple[HIDField, int]]] = {name: [] for name in usage_keys}
        entries["CenterX"] = []
        entries["CenterY"] = []
        for hf in input_fields:
            if hf.is_vendor or self._is_byte_expanded_field(hf):
                continue
            if self._is_combined_value_field(hf):
                usage = hf.usages[0]
                for name, key in usage_keys.items():
                    if (hf.usage_page, usage) == key:
                        entries[name].append((hf, 0))
                        break
                continue
            # X / Y 在同一個 field 內 input count == 2 時，
            # 第 2 個值視為 CenterX / CenterY（同一接點的中心座標）
            fu = hf.usages[0] if hf.usages else 0
            if (hf.report_count == 2
                    and (hf.usage_page, fu) in ((0x01, 0x30), (0x01, 0x31))
                    and all(u == fu for u in hf.usages[:2])):
                base = "X" if fu == 0x30 else "Y"
                entries[base].append((hf, 0))
                entries["Center" + base].append((hf, 1))
                continue
            for idx in range(hf.report_count):
                usage = hf.usages[idx] if idx < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                for name, key in usage_keys.items():
                    if (hf.usage_page, usage) == key:
                        entries[name].append((hf, idx))
                        break
        return entries

    def _setup_hybrid_columns(self, input_fields: List[HIDField]) -> bool:
        usage_entries = self._build_touch_usage_entries(input_fields)
        if not usage_entries["ContactID"] or not (usage_entries["X"] and usage_entries["Y"]):
            self._hybrid_groups = []
            self._hybrid_common = {}
            return False

        group_count = max(
            len(usage_entries["ContactID"]),
            len(usage_entries["X"]),
            len(usage_entries["Y"]),
            len(usage_entries["TipSwitch"]),
        )
        if group_count < 1:
            self._hybrid_groups = []
            self._hybrid_common = {}
            return False

        def pick(name: str, index: int) -> Optional[Tuple[HIDField, int]]:
            items = usage_entries[name]
            return items[index] if index < len(items) else None

        self._hybrid_groups = []
        for idx in range(group_count):
            self._hybrid_groups.append({
                "slot": idx,
                "Confidence": pick("Confidence", idx),
                "TipSwitch": pick("TipSwitch", idx),
                "InRange": pick("InRange", idx),
                "Invert": pick("Invert", idx),
                "Eraser": pick("Eraser", idx),
                "BarrelSwitch": pick("BarrelSwitch", idx),
                "ContactID": pick("ContactID", idx),
                "X": pick("X", idx),
                "CenterX": pick("CenterX", idx),
                "Y": pick("Y", idx),
                "CenterY": pick("CenterY", idx),
                "Width": pick("Width", idx),
                "Height": pick("Height", idx),
            })

        common_defs: Dict[str, Tuple[int, int]] = {
            "ScanTime": (0x0D, 0x56),
            "ContactCount": (0x0D, 0x54),
        }
        self._hybrid_common = {}
        for name, key in common_defs.items():
            for hf in input_fields:
                if hf.is_vendor or self._is_byte_expanded_field(hf):
                    continue
                usages = hf.usages[:hf.report_count] if hf.usages else []
                if self._is_combined_value_field(hf):
                    usages = [hf.usages[0]]
                for idx, usage in enumerate(usages):
                    if (hf.usage_page, usage) == key:
                        self._hybrid_common[name] = (hf, 0 if self._is_combined_value_field(hf) else idx)
                        break
                if name in self._hybrid_common:
                    break

        col_defs: List[dict] = [
            {"col_id": "__frame__", "label": "Frame", "width": 60, "kind": "meta", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "__rid__", "label": "RID", "width": 50, "kind": "meta", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "__slot__", "label": "Slot", "width": 50, "kind": "meta", "field_ref": None, "value_index": -1, "byte_index": -1},
        ]
        if "ScanTime" in self._hybrid_common:
            col_defs.append({"col_id": "ScanTime", "label": "ScanTime", "width": 85, "kind": "common", "field_ref": None, "value_index": -1, "byte_index": -1})
        col_defs.append({"col_id": "X", "label": "X", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1})
        if usage_entries["CenterX"]:
            col_defs.append({"col_id": "CenterX", "label": "CenterX", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1})
        col_defs.append({"col_id": "Y", "label": "Y", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1})
        if usage_entries["CenterY"]:
            col_defs.append({"col_id": "CenterY", "label": "CenterY", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1})
        col_defs += [
            {"col_id": "Width", "label": "Width", "width": 70, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "Height", "label": "Height", "width": 70, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "ContactID", "label": "ContactID", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
        ]
        if "ContactCount" in self._hybrid_common:
            col_defs.append({"col_id": "ContactCount", "label": "Count", "width": 60, "kind": "common", "field_ref": None, "value_index": -1, "byte_index": -1})

        # Button fields (usage page 0x09) — stored in _hybrid_common for value reading, merged into ConfTip
        for hf in input_fields:
            if hf.is_vendor or self._is_byte_expanded_field(hf):
                continue
            if hf.usage_page != 0x09:
                continue
            first_usage = hf.usages[0] if hf.usages else 0
            if (hf.usage_page, first_usage) in self._FF01_LIKE:
                continue
            for i in range(hf.report_count):
                usage = hf.usages[i] if i < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                key = f"btn_{usage:02X}"
                if key not in self._hybrid_common:
                    self._hybrid_common[key] = (hf, i)

        # Status column goes here — after touch/common fields, before FF01
        # 合併 InRange / Tip / Eraser / Invert / BarrelSwitch，僅列出值為 1 的旗標名稱
        col_defs.append({"col_id": "Status", "label": "Status", "width": 180, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1})

        _vu_col_w = {"Hex": 58, "Dec": 42, "Bin": 78}.get(self._ff01_fmt.get(), 58)
        for hf in input_fields:
            if not self._is_vendor_usage_byte_expanded_field(hf):
                continue
            usage = hf.usages[0] if hf.usages else 0
            if usage in self._ff01_usage_vars and not self._ff01_usage_vars[usage].get():
                continue
            n_bytes = max(1, (hf.bit_size + 7) // 8)
            for b in range(n_bytes):
                col_defs.append({
                    "col_id": f"vu_{usage:02X}_{id(hf)}_{b}",
                    "label": f"U{usage:02X}[{b}]",
                    "width": _vu_col_w,
                    "kind": "extra",
                    "field_ref": hf,
                    "value_index": -1,
                    "byte_index": b,
                })
        if self._show_raw.get():
            col_defs.append({"col_id": "__raw__", "label": "RAW", "width": 220, "kind": "meta", "field_ref": None, "value_index": -1, "byte_index": -1})

        self._col_defs = col_defs
        return True

    def _get_field_display_value(self, payload: bytes, hf: HIDField, idx: int) -> object:
        vals = extract_field_value(payload, hf.bit_offset, hf.per_bit_size, hf.report_count, hf.logical_min) \
            if hf.per_bit_size > 0 else []
        if self._is_combined_value_field(hf):
            return self._combine_field_parts(vals, hf.per_bit_size, hf.logical_min) if vals else ""
        return vals[idx] if idx < len(vals) else ""

    # Status 欄位顯示用：旗標 col_id -> 顯示名稱（依序）
    _STATUS_FLAGS = [
        ("Confidence", "Confidence"),
        ("InRange", "InRange"),
        ("TipSwitch", "Tip"),
        ("Eraser", "Eraser"),
        ("Invert", "Invert"),
        ("BarrelSwitch", "BarrelSwitch"),
        ("phyButton", "phyButton"),
    ]

    def _merge_status(self, flags: dict) -> str:
        """flags: {col_id: value}，把值為 1 的旗標名稱以空白串接。"""
        parts = []
        for key, label in self._STATUS_FLAGS:
            try:
                if int(flags.get(key, 0)):
                    parts.append(label)
            except (TypeError, ValueError):
                pass
        return " ".join(parts)

    # 個別欄位（非 hybrid／手寫筆）路徑用：usage -> Status 顯示名稱
    _STATUS_USAGE_LABELS = {
        (0x0D, 0x47): "Confidence",
        (0x0D, 0x32): "InRange",
        (0x0D, 0x42): "Tip",
        (0x0D, 0x45): "Eraser",
        (0x0D, 0x3C): "Invert",
        (0x0D, 0x44): "BarrelSwitch",
    }

    def _status_label_for(self, usage_page: int, usage: int) -> Optional[str]:
        """回傳該 usage 對應的 Status 旗標名稱；非狀態旗標回傳 None。"""
        lbl = self._STATUS_USAGE_LABELS.get((usage_page, usage))
        if lbl:
            return lbl
        # 實體按鍵（UP=0x09，排除 FF01-like 與 padding）→ phyButton
        if usage_page == 0x09 and usage != 0 and (usage_page, usage) not in self._FF01_LIKE:
            return "phyButton"
        return None

    def _merge_status_entries(self, entries, value_reader) -> str:
        """entries: [(HIDField, value_index, label)]，把值為 1 的旗標名稱依固定順序串接。"""
        order = [lbl for _, lbl in self._STATUS_FLAGS]
        active = []
        for hf, vidx, label in entries:
            vals = value_reader(hf)
            try:
                v = int(vals[vidx]) if vidx < len(vals) else 0
            except (TypeError, ValueError):
                v = 0
            if v and label not in active:
                active.append(label)
        active.sort(key=lambda l: order.index(l) if l in order else len(order))
        return " ".join(active)

    def _fmt_ff01_byte(self, val: int) -> str:
        fmt = self._ff01_fmt.get()
        if fmt == "Dec":
            return str(val)
        if fmt == "Bin":
            return f"{val:08b}"
        return f"{val:02X}"

    def _rebuild_table_columns(self):
        if self._all_digi_mode:
            self._setup_all_digi_columns()   # 全裝置模式：固定欄位，不被 View/RID/RAW 蓋掉
            return
        path_str = self._get_dev_path_str(self._selected_dev) if self._selected_dev else ""
        self._setup_table_columns(self._descriptors.get(path_str, []))

    def _setup_table_columns(self, fields: List[HIDField]):
        rid_sel = self._rid_filter_var.get()
        target_rid = None if rid_sel == "全部" else int(rid_sel, 16)

        input_fields = [
            f for f in fields
            if f.report_type == REPORT_TYPE_INPUT
            and (target_rid is None or f.report_id == target_rid)
            and (
                not f.is_const
                or self._is_byte_expanded_field(f)
                or self._is_vendor_usage_byte_expanded_field(f)
            )
        ]

        self._table_rid = target_rid if target_rid is not None else -1
        self._last_pkt_rx_time = 0.0
        self._last_scan_time   = -1
        self._scan_time_delta  = 0
        self._scan_delta_suppress = False
        self._scan_time_field  = next(
            (hf for hf in input_fields if not hf.is_vendor
             and any((hf.usage_page, u) == (0x0D, 0x56) for u in hf.usages)), None
        )
        self._contact_count_field = None
        for hf in input_fields:
            if hf.is_vendor or self._is_byte_expanded_field(hf):
                continue
            usages = hf.usages[:hf.report_count] if hf.usages else []
            if self._is_combined_value_field(hf):
                usages = [hf.usages[0]]
            for idx, usage in enumerate(usages):
                if (hf.usage_page, usage) == (0x0D, 0x54):
                    self._contact_count_field = (hf, 0 if self._is_combined_value_field(hf) else idx)
                    break
            if self._contact_count_field is not None:
                break
        self._last_contact_count = -1
        self._last_touch_active = False
        self._frame_seq = 0
        self._error_count = 0
        self._error_var.set("")
        if hasattr(self, "_top_error_var"):
            self._top_error_var.set("ERR 0")

        if self._view_mode.get() == "Hybrid" and self._setup_hybrid_columns(input_fields):
            self._pen_canvas = None   # 多點由 hybrid 路徑處理畫布
            ids = [c["col_id"] for c in self._col_defs]
            self._table["columns"] = ids
            self._table["show"] = "headings"
            for c in self._col_defs:
                self._table.heading(c["col_id"], text=c["label"])
                self._table.column(c["col_id"], width=self._col_width(c["width"], c["label"]),
                                   stretch=(c["col_id"] == "__raw__"), anchor="center")
            return

        # 單點（手寫筆）：descriptor 沒有 ContactID，hybrid 不成立，
        # 在此建立畫布欄位參照，讓畫布也能畫筆（即時監聽與回放共用）
        pen_entries = self._build_touch_usage_entries(input_fields)
        if pen_entries["X"] and pen_entries["Y"] and not pen_entries["ContactID"]:
            self._pen_canvas = {
                "X":          pen_entries["X"][0],
                "Y":          pen_entries["Y"][0],
                "TipSwitch":  pen_entries["TipSwitch"][0]  if pen_entries["TipSwitch"]  else None,
                "InRange":    pen_entries["InRange"][0]    if pen_entries["InRange"]    else None,
                "Eraser":     pen_entries["Eraser"][0]     if pen_entries["Eraser"]     else None,
                "Invert":     pen_entries["Invert"][0]     if pen_entries["Invert"]     else None,
                "Confidence": pen_entries["Confidence"][0] if pen_entries["Confidence"] else None,
            }
        else:
            self._pen_canvas = None

        col_defs: List[dict] = [
            {"col_id": "__rid__", "label": "RID", "width": 50,
             "field_ref": None, "value_index": -1, "byte_index": -1, "combine_parts": False},
        ]
        if self._show_raw.get():
            col_defs.append({"col_id": "__raw__", "label": "RAW", "width": 220,
                             "field_ref": None, "value_index": -1, "byte_index": -1, "combine_parts": False})

        usage_total: Dict[Tuple[int, int], int] = {}
        for hf in input_fields:
            if not hf.is_vendor and not self._is_byte_expanded_field(hf):
                if self._is_combined_value_field(hf):
                    u = hf.usages[0]
                    k = (hf.usage_page, u)
                    usage_total[k] = usage_total.get(k, 0) + 1
                else:
                    for i in range(hf.report_count):
                        u = hf.usages[i] if i < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                        k = (hf.usage_page, u)
                        usage_total[k] = usage_total.get(k, 0) + 1

        usage_seen:      Dict[Tuple[int, int], int] = {}
        vendor_byte_idx: int = 0
        byte_field_idx:  int = 0
        _vu_col_w = {"Hex": 58, "Dec": 42, "Bin": 78}.get(self._ff01_fmt.get(), 58)

        # 把 Confidence/InRange/Tip/Eraser/Invert/BarrelSwitch/實體按鍵
        # 合併成單一 Status 欄位（插入在第一個狀態旗標出現的位置）
        status_entries: List[Tuple[HIDField, int, str]] = []
        status_added = False

        for hf in input_fields:
            if self._is_vendor_usage_byte_expanded_field(hf):
                usage = hf.usages[0] if hf.usages else 0
                if usage in self._ff01_usage_vars and not self._ff01_usage_vars[usage].get():
                    continue
                n_bytes = max(1, (hf.bit_size + 7) // 8)
                for b in range(n_bytes):
                    col_defs.append({
                        "col_id":      f"vu_{usage:02X}_{id(hf)}_{b}",
                        "label":       f"U{usage:02X}[{b}]",
                        "width":       _vu_col_w,
                        "field_ref":   hf,
                        "value_index": -1,
                        "byte_index":  b,
                        "combine_parts": False,
                    })
            elif hf.is_vendor:
                for b in range(max(1, (hf.bit_size + 7) // 8)):
                    col_defs.append({
                        "col_id":      f"vnd_{id(hf)}_{b}",
                        "label":       f"V{vendor_byte_idx}",
                        "width":       40,
                        "field_ref":   hf,
                        "value_index": -1,
                        "byte_index":  b,
                        "combine_parts": False,
                    })
                    vendor_byte_idx += 1
            elif self._is_byte_expanded_field(hf):
                # UP=0x09 U=0xC5: 展開成 bit_size/8 個 byte 欄位
                n_bytes = max(1, hf.bit_size // 8)
                for b in range(n_bytes):
                    col_defs.append({
                        "col_id":      f"byf_{id(hf)}_{b}",
                        "label":       f"B{byte_field_idx}",
                        "width":       40,
                        "field_ref":   hf,
                        "value_index": -1,
                        "byte_index":  b,
                        "combine_parts": False,
                    })
                    byte_field_idx += 1
            elif self._is_combined_value_field(hf):
                u   = hf.usages[0]
                k   = (hf.usage_page, u)
                occ = usage_seen.get(k, 0)
                usage_seen[k] = occ + 1
                base  = get_usage_name(hf.usage_page, u)
                label = f"{base}[{occ}]" if usage_total.get(k, 1) > 1 else base
                col_defs.append({
                    "col_id":      f"fld_{id(hf)}_combined",
                    "label":       label,
                    "width":       80,
                    "field_ref":   hf,
                    "value_index": 0,
                    "byte_index":  -1,
                    "combine_parts": True,
                })
            else:
                for i in range(hf.report_count):
                    u   = hf.usages[i] if i < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                    slabel = self._status_label_for(hf.usage_page, u)
                    if slabel is not None:
                        status_entries.append((hf, i, slabel))
                        if not status_added:
                            col_defs.append({
                                "col_id":      "Status",
                                "label":       "Status",
                                "width":       180,
                                "field_ref":   None,
                                "value_index": -1,
                                "byte_index":  -1,
                                "combine_parts": False,
                                "status_entries": status_entries,
                            })
                            status_added = True
                        continue
                    k   = (hf.usage_page, u)
                    occ = usage_seen.get(k, 0)
                    usage_seen[k] = occ + 1
                    base  = get_usage_name(hf.usage_page, u)
                    label = f"{base}[{occ}]" if usage_total.get(k, 1) > 1 else base
                    col_defs.append({
                        "col_id":      f"fld_{id(hf)}_{i}",
                        "label":       label,
                        "width":       65,
                        "field_ref":   hf,
                        "value_index": i,
                        "byte_index":  -1,
                        "combine_parts": False,
                    })

        self._col_defs  = col_defs

        ids = [c["col_id"] for c in col_defs]
        self._table["columns"] = ids
        self._table["show"]    = "headings"
        for c in col_defs:
            self._table.heading(c["col_id"], text=c["label"])
            self._table.column(c["col_id"], width=self._col_width(c["width"], c["label"]),
                               stretch=(c["col_id"] == "__raw__"), anchor="center")

    # ------------------------------------------------------------------
    # Monitor: Listen / Stop
    # ------------------------------------------------------------------

    def _toggle_listen(self):
        if self._listening:
            self._stop_listen()
        else:
            self._start_listen()

    def _start_listen(self):
        if self._raw_thread and self._raw_thread.is_alive():
            return

        # 開始即時監聽前先結束回放
        if self._replay_active or self._replay_paused_at:
            self._replay_finish()

        if self._selected_dev:
            path_str = self._get_dev_path_str(self._selected_dev)
            self._descriptors.pop(path_str, None)
            self._raw_descriptors.pop(path_str, None)
            self._load_descriptor(self._selected_dev)
        elif self._all_digi_mode:
            self._preload_digi_ctx()   # 全裝置模式：監聽開始時才預載所有 digitizer descriptor

        extra_up = self._selected_dev.get("usage_page", 0) if self._selected_dev else 0
        extra_u  = self._selected_dev.get("usage",      0) if self._selected_dev else 0

        # 全裝置模式：註冊整段 digitizer top-level collection（0x0D 0x01~0x0E），
        # 涵蓋 Digitizer/Pen/LightPen/TouchScreen/TouchPad/Whiteboard/CMM/3D/Stylus/Finger…
        extra_usages = None
        if self._all_digi_mode:
            extra_usages = [(0x0D, u) for u in range(0x01, 0x0F)]

        self._raw_thread = RawInputThread(
            self._packet_queue, extra_usage_page=extra_up, extra_usage=extra_u,
            extra_usages=extra_usages,
        )
        self._raw_thread.start()
        self._raw_thread._ready_event.wait(timeout=3.0)

        self._listening = True
        self._listen_btn.config(text="停止監聽", bg=self._RED, activebackground=self._RED_DARK)
        self._status_var.set("監聽中...")

    def _stop_listen(self):
        if self._raw_thread:
            self._raw_thread.stop()
            self._raw_thread = None
        self._listening = False
        self._listen_btn.config(text="開始監聽", bg=self._GREEN, activebackground=self._GREEN_DARK)
        self._status_var.set("已停止監聽")

    # ------------------------------------------------------------------
    # Monitor: Queue polling & packet handling
    # ------------------------------------------------------------------

    def _is_new_frame(self, pkt: dict, rx_time: float, gap_threshold: float) -> bool:
        if self._scan_time_field is not None:
            data = pkt.get("data", b"")
            if data and data[0] == self._scan_time_field.report_id:
                payload = data[1:] if len(data) > 1 else b""
                hf   = self._scan_time_field
                vals = extract_field_value(payload, hf.bit_offset, hf.per_bit_size,
                                           hf.report_count, hf.logical_min)
                if vals:
                    st = vals[0]
                    if st != self._last_scan_time:
                        if self._last_scan_time >= 0 and not self._scan_delta_suppress:
                            wrap_base = max(1, hf.logical_max - hf.logical_min + 1)
                            self._scan_time_delta = (st - self._last_scan_time) % wrap_base
                        else:
                            self._scan_time_delta = 0
                        self._scan_delta_suppress = False
                        self._last_scan_time = st
                        return True
                    return False
        return rx_time - self._last_pkt_rx_time >= gap_threshold

    def _gap_threshold(self) -> float:
        try:
            return float(self._gap_ms_var.get()) / 1000.0
        except ValueError:
            return 0.004

    def _ingest_packet(self, pkt: dict, gap_threshold: float):
        """單一封包進管線：判斷新幀、更新狀態、填表格與畫布。
        即時監聽與回放共用，確保兩者行為完全一致。"""
        if self._all_digi_mode:
            self._handle_digi_packet(pkt)
            return
        rx_time = pkt.get("rx_time", time.monotonic())
        is_new_frame = self._is_new_frame(pkt, rx_time, gap_threshold)
        if is_new_frame:
            self._frame_deque.append(rx_time)
        self._last_pkt_rx_time = rx_time
        self._handle_packet(pkt, is_new_frame=is_new_frame)

    def _update_scan_rate(self, now: float):
        cutoff = now - 1.0
        while self._frame_deque and self._frame_deque[0] < cutoff:
            self._frame_deque.popleft()
        if hasattr(self, "_top_rate_var"):
            self._top_rate_var.set(f"{len(self._frame_deque)} scan/s")
        if self._all_digi_mode:
            self._update_digi_rate_detail(cutoff)

    def _update_digi_rate_detail(self, cutoff: float):
        """全裝置模式：算出每個來源裝置（各觸控裝置 / 筆）目前的 scan/s。"""
        parts = []
        for label in sorted(self._digi_rate_deques):
            dq = self._digi_rate_deques[label]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if dq:
                parts.append(f"{label}={len(dq)}/s")
        self._digi_rate_var.set("scan/s   " + "   ".join(parts) if parts else "")

    def _poll_queue(self):
        # 整段包 try/finally：單一封包/處理出錯也絕不讓 poll 迴圈停掉
        try:
            pkts = []
            try:
                while len(pkts) < 64:
                    pkts.append(self._packet_queue.get_nowait())
            except queue.Empty:
                pass

            if pkts and not self._replay_active:
                gap_threshold = self._gap_threshold()
                # 只處理「選定監聽裝置」的封包；RawInput 會收到所有同 usage page 裝置
                listen_key = (self._dev_match_key(self._get_dev_path_str(self._selected_dev))
                              if self._selected_dev else "")
                for pkt in pkts:
                    try:
                        if listen_key:
                            dn = pkt.get("device_name", "")
                            if dn and self._dev_match_key(dn) != listen_key:
                                continue   # 非選定裝置，忽略（不錄製/不計數/不顯示）
                        if self._listening:
                            self._record_buf.append({
                                "data": pkt.get("data", b""),
                                "rx_time": pkt.get("rx_time", time.monotonic()),
                                "device_name": pkt.get("device_name", ""),
                            })
                        self._ingest_packet(pkt, gap_threshold)
                    except Exception:
                        traceback.print_exc()
                self._update_scan_rate(time.monotonic())
                self._update_record_status()
        except Exception:
            traceback.print_exc()
        finally:
            self.after(20, self._poll_queue)

    _MAX_ROWS = 300

    @staticmethod
    def _excel_col_name(index: int) -> str:
        name = ""
        while index > 0:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name or "A"

    def _write_simple_xlsx(self, path: str, headers: List[str], rows: List[List[object]]):
        def cell_xml(value: object) -> str:
            if value is None:
                return '<c t="inlineStr"><is><t></t></is></c>'
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"<c><v>{value}</v></c>"
            return f'<c t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'

        all_rows = [headers] + rows
        sheet_rows: List[str] = []
        for row_idx, row in enumerate(all_rows, start=1):
            cells: List[str] = []
            for col_idx, value in enumerate(row, start=1):
                ref = f"{self._excel_col_name(col_idx)}{row_idx}"
                xml = cell_xml(value)
                cells.append(f'<c r="{ref}"{xml[2:]}')
            sheet_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

        last_col = self._excel_col_name(max(1, len(headers)))
        dimension = f"A1:{last_col}{max(1, len(all_rows))}"
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="{dimension}"/>'
            '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            f'<sheetData>{"".join(sheet_rows)}</sheetData>'
            '</worksheet>'
        )

        content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
        rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
        workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="MonitorLog" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""
        workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
        created_at = (
            datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>{escape(self._APP_NAME)}</dc:creator>
  <cp:lastModifiedBy>{escape(self._APP_NAME)}</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>
</cp:coreProperties>"""
        app_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>{escape(self._APP_NAME)}</Application>
</Properties>"""

        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types_xml)
            zf.writestr("_rels/.rels", rels_xml)
            zf.writestr("docProps/app.xml", app_xml)
            zf.writestr("docProps/core.xml", core_xml)
            zf.writestr("xl/workbook.xml", workbook_xml)
            zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
            zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    def _export_monitor_to_excel(self):
        if not self._monitor_log_rows:
            messagebox.showinfo("Export Excel", "目前沒有可匯出的監聽資料。")
            return

        default_name = f"hid_monitor_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        file_path = filedialog.asksaveasfilename(
            title="匯出監聽資料",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel Workbook", "*.xlsx")],
        )
        if not file_path:
            return

        dynamic_headers: List[str] = []
        for item in self._monitor_log_rows:
            for header in item["headers"]:
                if header not in dynamic_headers:
                    dynamic_headers.append(header)

        headers = ["Timestamp", "Device"] + dynamic_headers
        rows: List[List[object]] = []
        for item in self._monitor_log_rows:
            row = [item["timestamp"], item["device_name"]]
            row.extend(item["data"].get(header, "") for header in dynamic_headers)
            rows.append(row)

        try:
            self._write_simple_xlsx(file_path, headers, rows)
        except Exception as exc:
            messagebox.showerror("匯出失敗", f"無法匯出 Excel:\n{exc}")
            return

        self._status_var.set(f"已匯出 {len(rows)} 筆監聽資料到 {os.path.basename(file_path)}")
        messagebox.showinfo("Export Excel", f"已成功匯出 {len(rows)} 筆資料。")

    def _append_monitor_row(self, pkt: dict, headers: List[str], row: List[object],
                            row_tags: Tuple[str, ...], error_reasons: List[str]):
        self._monitor_log_rows.append({
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "device_name": pkt.get("device_name", ""),
            "headers": headers,
            "data": dict(zip(headers, row)),
            "errors": list(error_reasons),
        })
        if error_reasons:
            self._error_count += 1
            self._error_var.set(f"ERR:{self._error_count} | {error_reasons[0]}")
            if hasattr(self, "_top_error_var"):
                self._top_error_var.set(f"ERR {self._error_count}")

        # Buffer row — actual table.insert() done in _table_flush via after(0)
        self._table_pending.append((row, row_tags))
        if not self._table_flush_pending:
            self._table_flush_pending = True
            self.after(0, self._table_flush)

    def _table_flush(self):
        self._table_flush_pending = False
        if not self._table_pending:
            return
        pending, self._table_pending = self._table_pending, []
        for row, row_tags in pending:
            self._table_row_seq += 1
            if not row_tags and self._table_row_seq % 2 == 0:
                row_tags = ("stripe",)
            self._table.insert("", 0, values=row, tags=row_tags)
        try:
            max_rows = max(50, int(self._max_rows_var.get()))
        except (ValueError, AttributeError):
            max_rows = 200
        children = self._table.get_children()
        if len(children) > max_rows:
            for iid in children[max_rows:]:
                self._table.delete(iid)

    # ------------------------------------------------------------------
    # 「全部 digitizer」自動模式：不選裝置，通用 HID digitizer 解碼
    # ------------------------------------------------------------------

    _ALL_DIGI_COLS = [
        ("__dev__",      "裝置",      110),
        ("ScanTime",     "ScanTime",   80),
        ("ContactCount", "Cnt",        45),
        ("__slot__",     "Slot",       42),
        ("ContactID",    "CID",        55),
        ("X",            "X",          70),
        ("Y",            "Y",          70),
        ("TipPressure",  "Press",      60),
        ("Width",        "W",          48),
        ("Height",       "H",          48),
        ("XTilt",        "XTilt",      55),
        ("YTilt",        "YTilt",      55),
        ("Azimuth",      "Azim",       55),
        ("Status",       "Status",    150),
    ]

    def _setup_all_digi_columns(self):
        self._col_defs = []                          # 避免其他單一裝置路徑誤用
        cols = list(self._ALL_DIGI_COLS)
        raw_on = self._show_raw.get()
        if raw_on:
            cols.append(("__raw__", "RAW", 260))     # 整包原始位元組（hex）
        # 欄數改變時舊列會錯位，先清空
        for iid in self._table.get_children():
            self._table.delete(iid)
        self._digi_headers = [c[1] for c in cols]
        ids = [c[0] for c in cols]
        self._table["columns"] = ids
        self._table["show"] = "headings"
        for cid_, lbl, w in cols:
            stretch = (cid_ == "__raw__") if raw_on else (cid_ == "Status")
            anchor = "w" if cid_ == "__raw__" else "center"
            self._table.heading(cid_, text=lbl)
            self._table.column(cid_, width=self._col_width(w, lbl),
                               stretch=stretch, anchor=anchor)

    @staticmethod
    def _short_dev_label(dev: dict) -> str:
        vid = dev.get("vendor_id", 0)
        pid = dev.get("product_id", 0)
        col = device_collection(dev)
        tag = f" Col{col:02d}" if col >= 0 else ""
        return f"{vid:04X}:{pid:04X}{tag}"

    def _build_digi_ctx(self, fields: List[HIDField]) -> dict:
        """為某裝置 descriptor 建立通用 digitizer 解碼 context：
        {report_id: {"groups":[{usage:(hf,idx)}], "scan":entry, "count":entry}}，
        僅含有 X 與 Y 的觸控 report。"""
        input_fields = [f for f in fields if f.report_type == REPORT_TYPE_INPUT]
        by_rid: dict = {}
        keys = ("ContactID", "X", "Y", "Width", "Height", "TipSwitch",
                "Confidence", "InRange", "Invert", "Eraser", "BarrelSwitch")
        for rid in sorted(set(f.report_id for f in input_fields)):
            rf = [f for f in input_fields if f.report_id == rid]
            entries = self._build_touch_usage_entries(rf)
            if not (entries["X"] and entries["Y"]):
                continue
            gc = max(len(entries["ContactID"]), len(entries["X"]),
                     len(entries["Y"]), len(entries["TipSwitch"]), 1)
            groups = [{k: (entries[k][i] if i < len(entries[k]) else None) for k in keys}
                      for i in range(gc)]

            def _find(page, usage, _rf=rf):
                for hf in _rf:
                    if hf.is_vendor:
                        continue
                    us = hf.usages[:hf.report_count] if hf.usages else []
                    for idx, u in enumerate(us):
                        if (hf.usage_page, u) == (page, usage):
                            return (hf, idx)
                return None

            xhf = entries["X"][0][0]
            yhf = entries["Y"][0][0]
            by_rid[rid] = {"groups": groups,
                           "scan":  _find(0x0D, 0x56),   # ScanTime
                           "count": _find(0x0D, 0x54),   # ContactCount
                           "press": _find(0x0D, 0x30),   # TipPressure（手寫筆）
                           "xtilt": _find(0x0D, 0x3D),   # XTilt
                           "ytilt": _find(0x0D, 0x3E),   # YTilt
                           "azim":  _find(0x0D, 0x3F),   # Azimuth
                           "xr": (xhf.logical_min, xhf.logical_max),
                           "yr": (yhf.logical_min, yhf.logical_max)}
        return by_rid

    def _preload_digi_ctx(self):
        """進入全裝置模式時，先把所有 digitizer 的 descriptor 讀好建 ctx，
        避免封包到達時裝置正被使用而讀不到 descriptor。"""
        self._adigi_entries = []
        for dev in self._hidapi_devices:
            if dev.get("usage_page") != 0x0D:
                continue
            try:
                path_str = self._get_dev_path_str(dev)
                fields = self._descriptors.get(path_str)
                if fields is None:
                    raw = read_descriptor_via_hidapi(dev.get("path", b""))
                    fields = parse_report_descriptor(raw) if raw else []
                    self._descriptors[path_str] = fields
                ctx = self._build_digi_ctx(fields)
                if not ctx:
                    continue
                key = self._dev_match_key(path_str)        # 完整路徑 key
                vp, col = self._parse_vidpid_col(path_str)
                self._adigi_entries.append((key, vp, col, ctx))
            except Exception:
                continue

    @staticmethod
    def _parse_vidpid_col(name: str):
        s = (name or "").lower()
        m = re.search(r"vid_([0-9a-f]{4}).*?pid_([0-9a-f]{4})", s)
        if not m:
            return None, None
        col = re.search(r"&col([0-9a-f]+)", s)
        return f"{m.group(1)}:{m.group(2)}", (col.group(1) if col else None)

    def _get_digi_rid_ctx(self, device_name: str, report_id: int):
        """為封包選出正確 collection 的 rid-ctx：
        1) 先用完整裝置介面路徑精準比對（USB / I2C-HID 皆可，不需 VID/PID）
        2) 退而用 VID:PID + report_id（優先 Col 相符）
        回 (rid_ctx, label) 或 None。"""
        label = self._label_from_name(device_name)
        key = self._dev_match_key(device_name)
        if key:
            for k, vp, col, ctx in self._adigi_entries:
                if k == key:
                    rc = ctx.get(report_id)
                    if rc is not None:
                        return rc, label
        pvp, pcol = self._parse_vidpid_col(device_name)
        if pvp:
            best = None
            for k, vp, col, ctx in self._adigi_entries:
                if vp != pvp:
                    continue
                rc = ctx.get(report_id)
                if rc is None:
                    continue
                if col == pcol:
                    return rc, label
                if best is None:
                    best = (rc, label)
            if best:
                return best
        return None

    @staticmethod
    def _label_from_name(name: str) -> str:
        s = (name or "").lower()
        col = re.search(r"&col([0-9a-f]+)", s)
        tag = f" Col{int(col.group(1), 16):02d}" if col else ""
        m = re.search(r"vid_([0-9a-f]{4}).*?pid_([0-9a-f]{4})", s)
        if m:
            return f"{m.group(1).upper()}:{m.group(2).upper()}{tag}"
        # I2C-HID / ACPI：沒有 VID/PID，改取 HID# 之後的硬體 ID 片段
        i = s.find("hid#")
        if i >= 0:
            hwid = s[i + 4:].split("#")[0].split("&")[0].upper()
            if hwid:
                return f"{hwid}{tag}"
        return "??"

    def _decode_digi_rows(self, ctx: dict, payload: bytes, label: str) -> List[list]:
        def rd(entry):
            if not entry:
                return ""
            hf, idx = entry
            if hf.per_bit_size <= 0:
                return ""
            vals = extract_field_value(payload, hf.bit_offset, hf.per_bit_size,
                                       hf.report_count, hf.logical_min)
            return vals[idx] if idx < len(vals) else ""

        scan  = rd(ctx["scan"])
        count = rd(ctx["count"])
        press = rd(ctx.get("press"))
        xtilt = rd(ctx.get("xtilt"))
        ytilt = rd(ctx.get("ytilt"))
        azim  = rd(ctx.get("azim"))
        try:
            active = int(count)
        except (TypeError, ValueError):
            active = -1

        rows: List[list] = []
        for slot, g in enumerate(ctx["groups"]):
            x   = rd(g["X"]);   y   = rd(g["Y"])
            tip = rd(g["TipSwitch"]); cid = rd(g["ContactID"])
            w   = rd(g["Width"]); h = rd(g["Height"])
            empty_core = (tip in ("", 0) and cid in ("", 0)
                          and x in ("", 0) and y in ("", 0))
            if active >= 0 and slot >= active and empty_core:
                continue
            if empty_core and w in ("", 0) and h in ("", 0):
                continue
            status = self._merge_status({
                "Confidence": rd(g["Confidence"]), "InRange": rd(g["InRange"]),
                "TipSwitch": tip, "Eraser": rd(g["Eraser"]),
                "Invert": rd(g["Invert"]), "BarrelSwitch": rd(g["BarrelSwitch"]),
            })
            rows.append([label, scan, count, slot, cid, x, y,
                         press, w, h, xtilt, ytilt, azim, status])
        return rows

    def _handle_digi_packet(self, pkt: dict):
        data: bytes = pkt.get("data", b"")
        if not data:
            return
        res = self._get_digi_rid_ctx(pkt.get("device_name", ""), data[0])
        if not res:
            return
        ctx, label = res
        payload = data[1:] if len(data) > 1 else b""
        rx = pkt.get("rx_time", time.monotonic())
        self._frame_deque.append(rx)
        self._digi_rate_deques.setdefault(label, collections.deque()).append(rx)
        self._frame_seq += 1
        rows = self._decode_digi_rows(ctx, payload, label)
        if self._show_raw.get():
            raw_hex = " ".join(f"{b:02X}" for b in data)   # 整包（含 report id）
            for row in rows:
                row.append(raw_hex)
        for row in rows:
            self._append_monitor_row(pkt, self._digi_headers, row, (), [])
        self._feed_digi_canvas(label, ctx, payload)

    # ---- 全裝置畫布（每裝置一格 grid）----

    def _feed_digi_canvas(self, label: str, ctx: dict, payload: bytes):
        def rd(entry):
            if not entry:
                return ""
            hf, idx = entry
            if hf.per_bit_size <= 0:
                return ""
            vals = extract_field_value(payload, hf.bit_offset, hf.per_bit_size,
                                       hf.report_count, hf.logical_min)
            return vals[idx] if idx < len(vals) else ""

        dev = self._adigi_devs.get(label)
        if dev is None:
            order = self._digi_dev_next
            self._digi_dev_next += 1
            dev = {"order": order,
                   "color": self._SLOT_COLORS[order % len(self._SLOT_COLORS)],
                   "xr": ctx.get("xr", (0, 1)), "yr": ctx.get("yr", (0, 1)),
                   "contacts": {}, "trails": {}}
            self._adigi_devs[label] = dev

        try:
            active_count = int(rd(ctx.get("count")))
        except (TypeError, ValueError):
            active_count = -1

        active_keys = set()
        off_keys = set()
        for slot, g in enumerate(ctx["groups"]):
            # 有 ContactCount 時，超出數量的 slot 是 padding，直接略過避免誤判
            if active_count >= 0 and slot >= active_count:
                continue
            cid = rd(g["ContactID"])
            key = cid if cid not in ("", None) else f"s{slot}"
            tip = rd(g["TipSwitch"]); inr = rd(g["InRange"]); era = rd(g["Eraser"])
            # 筆的 eraser 與 tip 一樣算「接觸」→ 出線、實心點
            down  = (bool(tip) if tip != "" else False) or (bool(era) if era != "" else False)
            hover = (not down) and (bool(inr) if inr != "" else False)
            if down or hover:
                try:
                    xi = float(rd(g["X"])); yi = float(rd(g["Y"]))
                except (TypeError, ValueError):
                    continue
                active_keys.add(key)
                conf = rd(g["Confidence"])   # 描述子沒宣告 Confidence 時為 ""，宣告了則為 0/1
                dev["contacts"][key] = {"x": xi, "y": yi, "down": down, "conf": conf}
                if down:
                    tr = dev["trails"].setdefault(key, collections.deque(maxlen=500))
                    if not tr or (xi, yi) != tr[-1]:
                        tr.append((xi, yi))
                # 懸空（tip off 但 inrange 還在）：保留既有軌跡，只是不再延伸；
                # 依需求「inrange off 才清除」，所以這裡不 pop 軌跡
            else:
                # 明確 off：觸控 tip=0 / 筆 inrange=0（且非 padding）
                off_keys.add(key)
        # 只移除「明確回報 off」且本包沒有其他 slot 又報成 active 的接點；
        # 純粹「這包沒出現」的接點保留，不清除（避免多裝置交錯 / 分包誤清）
        for key in off_keys - active_keys:
            dev["contacts"].pop(key, None)
            dev["trails"].pop(key, None)

        self._schedule_digi_canvas_redraw()

    def _schedule_digi_canvas_redraw(self):
        if self._digi_canvas_redraw_pending:
            return
        self._digi_canvas_redraw_pending = True
        self.after(33, self._do_digi_canvas_redraw)   # ~30fps 節流

    def _do_digi_canvas_redraw(self):
        self._digi_canvas_redraw_pending = False
        try:
            if not self._touch_canvas.winfo_viewable():
                return
        except Exception:
            return
        self._redraw_digi_canvas()

    def _digi_key_color(self, key) -> str:
        """依接點 ID 取色：int 直接用，"s{slot}" 取數字，其他 hash。"""
        if isinstance(key, int):
            idx = key
        else:
            m = re.search(r"\d+", str(key))
            idx = int(m.group()) if m else abs(hash(key))
        return self._SLOT_COLORS[idx % len(self._SLOT_COLORS)]

    def _redraw_digi_canvas(self):
        c = self._touch_canvas
        c.delete("all")
        w = c.winfo_width(); h = c.winfo_height()
        if w <= 1 or h <= 1:
            return
        devs = sorted(self._adigi_devs.items(), key=lambda kv: kv[1]["order"])
        if not devs:
            return
        cols = math.ceil(math.sqrt(len(devs)))
        rows = math.ceil(len(devs) / cols)
        cw = w / cols; ch = h / rows
        pad = 6; lbl_h = 16
        for i, (label, dev) in enumerate(devs):
            r = i // cols; col = i % cols
            cx0 = col * cw; cy0 = r * ch
            color = dev["color"]
            c.create_rectangle(cx0 + 2, cy0 + 2, cx0 + cw - 2, cy0 + ch - 2,
                               outline="#cccccc", dash=(3, 3))
            c.create_text(cx0 + 6, cy0 + 3, anchor="nw", text=label,
                          fill=color, font=("Consolas", 8, "bold"))
            ax0 = cx0 + pad; ay0 = cy0 + pad + lbl_h
            aw = cw - 2 * pad; ah = ch - 2 * pad - lbl_h
            if aw <= 4 or ah <= 4:
                continue
            xmin, xmax = dev["xr"]; ymin, ymax = dev["yr"]
            xspan = (xmax - xmin) or 1
            yspan = (ymax - ymin) or 1

            def _tx(vx, _a=ax0, _lo=xmin, _s=xspan, _w=aw):
                return _a + (vx - _lo) / _s * _w

            def _ty(vy, _a=ay0, _lo=ymin, _s=yspan, _h=ah):
                return _a + (vy - _lo) / _s * _h

            # 每個接點 ID 用不同顏色（裝置改用格子位置 + 標籤區分）
            for key, tr in dev["trails"].items():
                if len(tr) >= 2:
                    flat = []
                    for vx, vy in tr:
                        flat += [_tx(vx), _ty(vy)]
                    c.create_line(flat, fill=self._digi_key_color(key), width=2,
                                  capstyle=tk.ROUND, joinstyle=tk.ROUND)
            for key, ct in dev["contacts"].items():
                kcolor = self._digi_key_color(key)
                px = _tx(ct["x"]); py = _ty(ct["y"])
                rr = 9 if ct["down"] else 6
                lw = 2
                # 描述子有宣告 Confidence 且 = 0（低信心 / palm）→ 放大加粗強調
                cf = ct.get("conf")
                if cf not in ("", None):
                    try:
                        if int(cf) == 0:
                            rr += 12
                            lw = 4
                    except (TypeError, ValueError):
                        pass
                fill = kcolor if ct["down"] else ""
                c.create_oval(px - rr, py - rr, px + rr, py + rr,
                              fill=fill, outline=kcolor, width=lw)
                c.create_text(px, py - rr - 6, text=str(key),
                              fill=kcolor, font=("Consolas", 7))

    def _clear_digi_canvas(self):
        self._adigi_devs.clear()
        self._digi_dev_next = 0
        try:
            self._touch_canvas.delete("all")
        except Exception:
            pass

    def _handle_packet(
        self,
        pkt: dict,
        is_new_frame: bool = False,
    ):
        data: bytes = pkt.get("data", b"")
        if not data:
            return

        report_id = data[0]
        payload   = data[1:] if len(data) > 1 else b""

        if self._table_rid != -1 and report_id != self._table_rid:
            return

        if not self._col_defs:
            return

        field_cache: Dict[int, List[int]] = {}

        def get_vals(hf: HIDField) -> List[int]:
            key = id(hf)
            if key not in field_cache:
                field_cache[key] = (
                    extract_field_value(payload, hf.bit_offset, hf.per_bit_size,
                                        hf.report_count, hf.logical_min)
                    if hf.per_bit_size > 0 else []
                )
            return field_cache[key]

        if is_new_frame:
            self._frame_seq += 1

        current_contact_count = -1
        if self._contact_count_field is not None:
            cc_hf, cc_idx = self._contact_count_field
            cc_vals = get_vals(cc_hf)
            if self._is_combined_value_field(cc_hf):
                current_contact_count = self._combine_field_parts(cc_vals, cc_hf.per_bit_size, cc_hf.logical_min) if cc_vals else -1
            elif cc_idx < len(cc_vals):
                current_contact_count = cc_vals[cc_idx]

        current_touch_active = False
        if self._hybrid_groups:
            for group in self._hybrid_groups:
                tip_entry = group.get("TipSwitch")
                if not tip_entry:
                    continue
                tip_hf, tip_idx = tip_entry
                tip_vals = get_vals(tip_hf)
                if tip_idx < len(tip_vals) and tip_vals[tip_idx]:
                    current_touch_active = True
                    break
        elif current_contact_count > 0:
            current_touch_active = True

        error_reasons: List[str] = []
        row_tags: Tuple[str, ...] = ()
        try:
            max_delta = int(self._max_scan_delta_var.get())
        except ValueError:
            max_delta = 0
        suppress_scan_error = (
            is_new_frame
            and (current_touch_active and not self._last_touch_active)
        )
        if is_new_frame and max_delta > 0 and self._scan_time_delta > max_delta and not suppress_scan_error:
            error_reasons.append(f"ScanΔ={self._scan_time_delta}>{max_delta}")
            row_tags = ("scan_error",)
        if is_new_frame:
            # 觸控非活動時只抑制下一個 frame 的 scan Δ 錯誤判斷，不重設
            # _last_scan_time —— 重設會讓 frame 計數把同一 frame 的後續封包
            # （或 scan time 未變的封包）重複累計，造成 Hybrid 模式 scan/s 偏高
            if not current_touch_active:
                self._scan_delta_suppress = True
            self._scan_time_delta = 0
            if current_contact_count >= 0:
                self._last_contact_count = current_contact_count
            self._last_touch_active = current_touch_active

        if self._view_mode.get() == "Hybrid" and self._hybrid_groups:
            headers = [col["label"] for col in self._col_defs]
            raw_hex = " ".join(f"{b:02X}" for b in data)

            def read_entry(entry: Optional[Tuple[HIDField, int]]) -> object:
                if not entry:
                    return ""
                hf, idx = entry
                return self._get_field_display_value(payload, hf, idx)

            common_values = {name: read_entry(entry) for name, entry in self._hybrid_common.items()}
            _btn_active = next(
                (v for k, v in common_values.items() if k.startswith("btn_") and v not in ("", 0, None)),
                None,
            )
            try:
                active_count = int(common_values.get("ContactCount", ""))
            except (TypeError, ValueError):
                active_count = -1
            if is_new_frame:
                self._canvas_prev_active = set(self._canvas_contacts.keys())
                self._canvas_contacts.clear()
            _stress_pkt_conf = False
            _stress_pkt_tip  = False
            appended = False
            for group in self._hybrid_groups:
                confidence = read_entry(group.get("Confidence"))
                tip = read_entry(group.get("TipSwitch"))
                try:
                    if int(confidence): _stress_pkt_conf = True
                except (TypeError, ValueError): pass
                try:
                    if int(tip): _stress_pkt_tip = True
                except (TypeError, ValueError): pass
                cid_val = read_entry(group.get("ContactID"))
                x = read_entry(group.get("X"))
                y = read_entry(group.get("Y"))
                width = read_entry(group.get("Width"))
                height = read_entry(group.get("Height"))

                # In parallel reports, ContactCount tells us how many contacts are valid.
                # Hide trailing empty slots when switching to the hybrid-style view.
                if active_count >= 0 and group["slot"] >= active_count:
                    if tip in ("", 0) and cid_val in ("", 0) and x in ("", 0) and y in ("", 0):
                        continue

                if (
                    tip in ("", 0)
                    and cid_val in ("", 0)
                    and x in ("", 0)
                    and y in ("", 0)
                    and width in ("", 0)
                    and height in ("", 0)
                ):
                    continue

                status_val = self._merge_status({
                    "Confidence": confidence,
                    "InRange": read_entry(group.get("InRange")),
                    "TipSwitch": tip,
                    "Eraser": read_entry(group.get("Eraser")),
                    "Invert": read_entry(group.get("Invert")),
                    "BarrelSwitch": read_entry(group.get("BarrelSwitch")),
                    "phyButton": _btn_active,
                })
                row_map = {
                    "__frame__": self._frame_seq,
                    "__rid__": f"0x{report_id:02X}",
                    "__slot__": group["slot"],
                    "Status": status_val,
                    "ContactID": cid_val,
                    "X": x,
                    "CenterX": read_entry(group.get("CenterX")),
                    "Y": y,
                    "CenterY": read_entry(group.get("CenterY")),
                    "Width": width,
                    "Height": height,
                    "__raw__": raw_hex,
                }
                row_map.update(common_values)
                row = []
                for col in self._col_defs:
                    if col["col_id"] in row_map:
                        row.append(row_map[col["col_id"]])
                    elif col.get("byte_index", -1) >= 0:
                        hf = col["field_ref"]
                        byte_pos = (hf.bit_offset // 8) + col["byte_index"]
                        val = payload[byte_pos] if byte_pos < len(payload) else 0
                        if col["col_id"].startswith("vu_"):
                            row.append(self._fmt_ff01_byte(val))
                        else:
                            row.append(f"{val:02X}")
                    else:
                        row.append("")
                self._append_monitor_row(pkt, headers, row, row_tags, error_reasons)
                appended = True

                # Canvas: 用 ContactID 當 key，避免兩指交叉時 slot 互串
                try:
                    lx = int(x) if x not in ("", None) else None
                    ly = int(y) if y not in ("", None) else None
                except (TypeError, ValueError):
                    lx = ly = None
                try:
                    track_key = int(cid_val)
                except (TypeError, ValueError):
                    track_key = None
                if track_key is not None:
                    self._canvas_update_slot(track_key, tip, lx, ly, cid_val, confidence)

            if self._stress_running and not self._stress_pending:
                if _stress_pkt_tip:
                    if not self._stress_tip_active:
                        self._stress_touch_had_no_conf = False  # 新的一次按壓
                    self._stress_tip_active = True
                    if not _stress_pkt_conf:
                        self._stress_touch_had_no_conf = True   # Tip=1 但沒有 Confidence
                else:  # Tip=0 → 抬起
                    if self._stress_tip_active:
                        self._stress_tip_active = False
                        self._stress_on_lift_detected()

            if not appended:
                row_map = {
                    "__frame__": self._frame_seq,
                    "__rid__": f"0x{report_id:02X}",
                    "__slot__": "",
                    "__raw__": raw_hex,
                    "Status": self._merge_status({"phyButton": _btn_active}),
                }
                row_map.update(common_values)
                row = []
                for col in self._col_defs:
                    if col["col_id"] in row_map:
                        row.append(row_map[col["col_id"]])
                    elif col.get("byte_index", -1) >= 0:
                        hf = col["field_ref"]
                        byte_pos = (hf.bit_offset // 8) + col["byte_index"]
                        val = payload[byte_pos] if byte_pos < len(payload) else 0
                        if col["col_id"].startswith("vu_"):
                            row.append(self._fmt_ff01_byte(val))
                        else:
                            row.append(f"{val:02X}")
                    else:
                        row.append("")
                self._append_monitor_row(pkt, headers, row, row_tags, error_reasons)
            return

        # 單點（手寫筆）畫布：下筆畫軌跡、懸空顯示游標點
        if self._pen_canvas is not None:
            self._feed_pen_canvas(get_vals, is_new_frame)

        row = []
        for col in self._col_defs:
            cid = col["col_id"]
            if cid == "__rid__":
                row.append(f"0x{report_id:02X}")
            elif cid == "__raw__":
                row.append(" ".join(f"{b:02X}" for b in data))
            elif col.get("status_entries") is not None:
                row.append(self._merge_status_entries(col["status_entries"], get_vals))
            elif col["byte_index"] >= 0:
                hf       = col["field_ref"]
                byte_pos = (hf.bit_offset // 8) + col["byte_index"]
                val      = payload[byte_pos] if byte_pos < len(payload) else 0
                if cid.startswith("vu_"):
                    row.append(self._fmt_ff01_byte(val))
                else:
                    row.append(f"{val:02X}")
            else:
                hf   = col["field_ref"]
                idx  = col["value_index"]
                vals = get_vals(hf)
                if col.get("combine_parts"):
                    row.append(self._combine_field_parts(vals, hf.per_bit_size, hf.logical_min) if vals else "")
                else:
                    row.append(vals[idx] if idx < len(vals) else "")

        headers = [col["label"] for col in self._col_defs]
        self._append_monitor_row(pkt, headers, row, row_tags, error_reasons)

    def _clear_log(self):
        self._monitor_log_rows.clear()
        self._table_pending.clear()
        for iid in self._table.get_children():
            self._table.delete(iid)
        self._frame_seq = 0
        self._table_row_seq = 0
        self._error_count = 0
        self._error_var.set("")
        if hasattr(self, "_top_error_var"):
            self._top_error_var.set("ERR 0")

    def _clear_monitor_all(self):
        """單一清除：監聽表格、畫布、錄製緩衝一次全部清空。"""
        self._replay_clear()                  # 結束回放 + 清空錄製緩衝/時間軸
        self._reset_monitor_runtime_state()   # 清空表格 + 畫布 + 執行期狀態

    # ------------------------------------------------------------------
    # Monitor: Record / Replay（記憶體回放）
    # ------------------------------------------------------------------

    def _build_replay_controls(self, parent):
        """在指定列建立一組回放控制。FAE/Customer 版只保留 錄製 + 匯出錄製，
        ▶回放播放 / 速度 / 進度條 / 載入錄製 為 Engineer 版限定。"""
        if self._is_engineer():
            btn = ttk.Button(parent, text="▶ 回放", width=8, command=self._replay_toggle)
            btn.pack(side=tk.LEFT)
            self._replay_btns.append(btn)
            ttk.Label(parent, text="速度:").pack(side=tk.LEFT, padx=(8, 2))
            ttk.Combobox(parent, textvariable=self._replay_speed_var, width=6, state="readonly",
                         values=("0.25x", "0.5x", "1x", "2x", "4x", "8x")).pack(side=tk.LEFT)
            scale = ttk.Scale(parent, from_=0, to=max(0, len(self._replay_data) - 1),
                              orient=tk.HORIZONTAL, command=self._replay_on_scale)
            scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
            self._replay_scales.append(scale)
            ttk.Label(parent, textvariable=self._replay_pos_var, width=16,
                      font=("Consolas", 9)).pack(side=tk.LEFT)
        else:
            ttk.Label(parent, text="錄製：", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Label(parent, textvariable=self._record_status_var, style="Muted.TLabel",
                  width=10, anchor=tk.E if self._is_engineer() else tk.W).pack(
                      side=tk.LEFT, padx=(6, 0))
        ttk.Button(parent, text="匯出錄製", width=9,
                   command=self._export_recording).pack(side=tk.LEFT, padx=(6, 0))
        if self._is_engineer():
            ttk.Button(parent, text="載入錄製", width=9,
                       command=self._import_recording).pack(side=tk.LEFT, padx=(4, 0))

    def _replay_set_btn_text(self, text):
        for b in self._replay_btns:
            b.config(text=text)

    def _replay_config_to(self, n):
        for s in self._replay_scales:
            s.configure(to=n)

    def _update_record_status(self):
        n = len(self._record_buf)
        self._record_status_var.set(f"錄製 {n}")
        if hasattr(self, "_top_record_var"):
            self._top_record_var.set(f"REC {n}")

    def _enter_all_digi_mode(self):
        """進入全裝置模式的共用設定（不含 _adigi_entries 來源與監聽重啟）。"""
        self._all_digi_mode = True
        self._selected_dev = None
        self._digi_rate_deques.clear()
        self._digi_rate_var.set("")
        self._clear_digi_canvas()
        self._reset_monitor_runtime_state()
        self._setup_all_digi_columns()
        if not self._digi_rate_lbl.winfo_ismapped():
            self._digi_rate_lbl.pack(side=tk.TOP, fill=tk.X, padx=2, pady=(0, 2),
                                     before=self._tbl_wrap)

    # ------------------------------------------------------------------
    # 監聽錄製：匯出 / 載入（自包含檔，含 descriptor，之後沒接裝置也能回放）
    # ------------------------------------------------------------------

    def _raw_descriptor_for_name(self, device_name: str):
        key = self._dev_match_key(device_name)
        dev = next((d for d in self._hidapi_devices
                    if self._dev_match_key(self._get_dev_path_str(d)) == key), None)
        if dev is None:
            return None
        try:
            raw = read_descriptor_via_hidapi(dev.get("path", b""))
            return bytes(raw) if raw else None
        except Exception:
            return None

    def _export_recording(self):
        if not self._record_buf:
            messagebox.showinfo("匯出錄製", "目前沒有錄製資料。")
            return
        path = filedialog.asksaveasfilename(
            title="匯出錄製", defaultextension=".hidrec",
            filetypes=[("HID 錄製檔", "*.hidrec"), ("所有檔案", "*.*")])
        if not path:
            return
        try:
            recs = list(self._record_buf)
            descs = {}
            for r in recs:
                k = self._dev_match_key(r.get("device_name", ""))
                if not k or k in descs:
                    continue
                raw = self._raw_descriptor_for_name(r.get("device_name", ""))
                if raw:
                    descs[k] = raw.hex()
            out = {
                "format": "hidrec", "version": 1,
                "descriptors": descs,
                "records": [{"t": r.get("rx_time", 0.0),
                             "d": (r.get("data", b"") or b"").hex(),
                             "n": r.get("device_name", "")} for r in recs],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f)
            messagebox.showinfo("匯出錄製",
                                f"已匯出 {len(recs)} 筆封包、{len(descs)} 個裝置 descriptor\n{path}")
        except Exception as exc:
            messagebox.showerror("匯出錄製", f"匯出失敗:\n{exc}")

    def _import_recording(self):
        if self._listening:
            messagebox.showinfo("載入錄製", "請先停止監聽再載入。")
            return
        path = filedialog.askopenfilename(
            title="載入錄製",
            filetypes=[("HID 錄製檔", "*.hidrec"), ("所有檔案", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception as exc:
            messagebox.showerror("載入錄製", f"讀取失敗:\n{exc}")
            return

        recs  = obj.get("records", []) if isinstance(obj, dict) else (obj or [])
        descs = obj.get("descriptors", {}) if isinstance(obj, dict) else {}

        # 切到全裝置模式，用檔案內 descriptor 解碼（裝置沒接也能回放）
        self._replay_finish()
        try:
            self._dev_combo.current(0)
        except Exception:
            pass
        self._enter_all_digi_mode()

        self._adigi_entries = []
        for k, hexdesc in (descs or {}).items():
            try:
                fields = parse_report_descriptor(bytes.fromhex(hexdesc))
                ctx = self._build_digi_ctx(fields)
                if ctx:
                    vp, col = self._parse_vidpid_col(k)
                    self._adigi_entries.append((k, vp, col, ctx))
            except Exception:
                continue

        self._record_buf.clear()
        for r in recs:
            try:
                data = bytes.fromhex(r.get("d", ""))
            except (ValueError, AttributeError):
                continue
            self._record_buf.append({
                "data": data,
                "rx_time": float(r.get("t", 0.0)),
                "device_name": r.get("n", ""),
            })
        self._update_record_status()
        self._replay_data = list(self._record_buf)
        self._replay_idx = 0
        self._replay_paused_at = 0
        self._replay_config_to(max(0, len(self._replay_data) - 1))
        self._replay_set_scale(0)
        self._replay_pos_var.set(f"0 / {len(self._replay_data)}")
        messagebox.showinfo(
            "載入錄製",
            f"已載入 {len(self._record_buf)} 筆封包、{len(self._adigi_entries)} 個裝置。\n按「回放」播放。")

    def _reset_monitor_runtime_state(self):
        """回放前重置與封包處理相關的執行期狀態（不動欄位定義）。"""
        self._monitor_log_rows.clear()
        self._table_pending.clear()
        for iid in self._table.get_children():
            self._table.delete(iid)
        self._frame_seq = 0
        self._table_row_seq = 0
        self._error_count = 0
        self._error_var.set("")
        if hasattr(self, "_top_error_var"):
            self._top_error_var.set("ERR 0")
        self._last_pkt_rx_time = 0.0
        self._last_scan_time = -1
        self._scan_time_delta = 0
        self._scan_delta_suppress = False
        self._last_contact_count = -1
        self._last_touch_active = False
        self._frame_deque.clear()
        self._clear_canvas()
        if self._all_digi_mode:
            self._clear_digi_canvas()

    def _replay_toggle(self):
        if self._replay_active:
            self._replay_pause()
        else:
            self._replay_start()

    def _replay_start(self):
        if self._replay_active:
            return
        if self._listening:
            messagebox.showinfo("回放", "請先停止監聽再回放。")
            return
        if not self._record_buf:
            messagebox.showinfo("回放", "目前沒有錄製資料。")
            return

        resuming = 0 < self._replay_paused_at < len(self._replay_data)
        if not resuming:
            # 全新回放：快照緩衝、重置畫面、回到起點
            self._replay_data = list(self._record_buf)
            self._replay_idx = 0
            self._reset_monitor_runtime_state()
            self._replay_config_to(max(0, len(self._replay_data) - 1))
            self._replay_set_scale(0)
        # 倍速
        try:
            self._replay_speed = float(self._replay_speed_var.get().rstrip("x"))
        except ValueError:
            self._replay_speed = 1.0

        self._replay_active = True
        self._replay_set_btn_text("⏸ 暫停")
        self._status_var.set("回放中…")
        base = self._replay_data[self._replay_idx].get("rx_time", 0.0)
        self._replay_t0 = base
        self._replay_wall0 = time.monotonic()
        self._replay_tick()

    def _replay_pause(self):
        self._replay_active = False
        if self._replay_after_id:
            self.after_cancel(self._replay_after_id)
            self._replay_after_id = None
        self._replay_paused_at = self._replay_idx
        self._replay_set_btn_text("▶ 回放")
        self._status_var.set("回放暫停")

    def _replay_finish(self):
        self._replay_active = False
        if self._replay_after_id:
            self.after_cancel(self._replay_after_id)
            self._replay_after_id = None
        self._replay_paused_at = 0
        self._replay_set_btn_text("▶ 回放")
        self._status_var.set("回放結束")

    def _replay_tick(self):
        self._replay_after_id = None
        if not self._replay_active:
            return
        data = self._replay_data
        n = len(data)
        if self._replay_idx >= n:
            self._replay_finish()
            return
        gap_threshold = self._gap_threshold()
        virtual_now = self._replay_t0 + (time.monotonic() - self._replay_wall0) * self._replay_speed

        fed = 0
        while self._replay_idx < n:
            pkt = data[self._replay_idx]
            if pkt.get("rx_time", 0.0) > virtual_now and fed > 0:
                break
            self._ingest_packet(pkt, gap_threshold)
            self._replay_idx += 1
            fed += 1
            if fed >= 512:          # 單次 tick 上限，避免大量積壓卡住 UI
                break

        self._update_scan_rate(virtual_now)
        self._replay_set_scale(min(self._replay_idx, n - 1))
        self._replay_pos_var.set(f"{self._replay_idx} / {n}")

        if self._replay_idx >= n:
            self._replay_finish()
            return
        self._replay_after_id = self.after(15, self._replay_tick)

    def _replay_set_scale(self, value):
        """程式設定滑桿位置（不觸發 on_scale 的拖曳跳轉）。"""
        self._replay_sync = True
        try:
            for s in self._replay_scales:
                s.set(value)
        finally:
            self._replay_sync = False

    def _replay_on_scale(self, val):
        if self._replay_sync or not self._replay_data:
            return
        target = int(float(val))
        target = max(0, min(len(self._replay_data) - 1, target))
        if target == self._replay_idx:
            return
        # 拖曳跳轉：暫停、重置、瞬間重餵到 target 重建畫面。
        # 只重餵最近一段（畫布軌跡上限 500/指、scan rate 視窗 1 秒），
        # 足以重建可見狀態又不會因大量插入卡死 UI。
        was_active = self._replay_active
        if was_active:
            self._replay_pause()
        self._reset_monitor_runtime_state()
        gap_threshold = self._gap_threshold()
        window = 4000
        for i in range(max(0, target + 1 - window), target + 1):
            self._ingest_packet(self._replay_data[i], gap_threshold)
        self._replay_idx = target + 1
        self._replay_paused_at = self._replay_idx
        self._replay_set_scale(target)   # 同步另一個分頁的滑桿
        self._replay_pos_var.set(f"{self._replay_idx} / {len(self._replay_data)}")
        self._update_scan_rate(self._replay_data[target].get("rx_time", time.monotonic()))

    def _replay_clear(self):
        if self._replay_active:
            self._replay_finish()
        self._record_buf.clear()
        self._replay_data = []
        self._replay_idx = 0
        self._replay_paused_at = 0
        self._replay_config_to(0)
        self._replay_set_scale(0)
        self._replay_pos_var.set("0 / 0")
        self._update_record_status()

    # ------------------------------------------------------------------
    # Send tab
    # ------------------------------------------------------------------

    def _on_report_type_changed(self, *_):
        rtype = self._report_type.get()
        if rtype in ("Feature", "Input"):
            self._get_btn.pack(side=tk.LEFT, padx=4)
        else:
            self._get_btn.pack_forget()
        # 「等待 INT 回應」綁定 Output：Output 預設打勾並可用；其他類型取消勾選並停用
        is_output = (rtype == "Output")
        state = "normal" if is_output else "disabled"
        self._wait_int_var.set(is_output)
        for w in (getattr(self, "_wait_int_cb", None),
                  getattr(self, "_int_timeout_spin", None),
                  getattr(self, "_int_length_entry", None)):
            if w is not None:
                try:
                    w.configure(state=state)
                except Exception:
                    pass

    def _on_send(self):
        if not self._get_cmd_dev():
            self._send_log_append("[錯誤] 請先選擇一個裝置")
            return

        rid_str = self._report_id_var.get().strip()
        try:
            report_id = int(rid_str, 16)
            if not (0 <= report_id <= 255):
                raise ValueError
        except ValueError:
            self._send_log_append("[錯誤] Report ID 必須是 0x00~0xFF 的十六進位數值")
            return

        try:
            data = parse_hex_bytes(self._send_data_var.get().strip())
        except ValueError:
            self._send_log_append("[錯誤] Data 格式錯誤，請使用十六進位")
            return

        threading.Thread(
            target=self._send_report,
            args=(self._get_cmd_dev()["path"], report_id, data, self._report_type.get()),
            daemon=True,
        ).start()

    def _send_report(self, path, report_id: int, data: list, rtype: str):
        self._send_log_append(f"\n發送 {rtype} Report")
        self._send_log_append(f"  Report ID : 0x{report_id:02X}")
        self._send_log_append(f"  Data (輸入) : {' '.join(f'{b:02X}' for b in data) or '(空)'}")
        try:
            if rtype == "Feature":
                required = self._report_length(report_id, REPORT_TYPE_FEATURE) - 1
                padded = (data + [0] * required)[:required]
                self._log_payload(report_id, padded)
                sent = send_feature_report(path, report_id, padded)
                if sent < 0:
                    self._send_log_append(f"  [錯誤] 發送失敗 (回傳 {sent})")
                else:
                    self._send_log_append(f"  [成功] 已發送 {sent} bytes")
            elif rtype == "Input":
                required = self._report_length(report_id, REPORT_TYPE_OUTPUT) - 1
                padded = (data + [0] * required)[:required]
                self._log_payload(report_id, padded)
                set_output_report_cmd(path, report_id, padded)
                self._send_log_append(f"  [成功] SET_REPORT(Output) command 已送出")
            else:
                self._log_payload(report_id, data)
                sent = send_output_report(path, report_id, data)
                if sent < 0:
                    self._send_log_append(f"  [錯誤] 發送失敗 (回傳 {sent})")
                else:
                    self._send_log_append(f"  [成功] 已發送 {sent} bytes")
        except Exception as e:
            self._send_log_append(f"  [錯誤] {e}")
            return

        if self._wait_int_var.get():
            self._wait_int(path)

    def _wait_int(self, path):
        try:
            timeout_ms = int(self._int_timeout_var.get())
            length     = int(self._int_length_var.get())
        except ValueError:
            self._send_log_append("  [錯誤] Timeout 或 Length 格式錯誤")
            return
        self._send_log_append(f"  等待 INT 回應（timeout={timeout_ms}ms, length={length}B）...")
        try:
            resp = read_interrupt(path, length, timeout_ms)
            if not resp:
                self._send_log_append("  [INT] 逾時，無回應")
            else:
                self._send_log_append(f"  [INT] 收到 {len(resp)} bytes:")
                self._log_bytes(resp, self._send_log_append)
        except Exception as e:
            self._send_log_append(f"  [INT 錯誤] {e}")

    def _log_bytes(self, data, append_fn):
        """顯示 bytes：第一行 64 bytes，後續每行 66 bytes（對齊 I2C block）。
        若 _pair_bytes_var 打勾，每 2 bytes 合併顯示；第一行 byte0 留空，
        配對從 byte1 開始，第一行 32 個單位、後續 33 個單位。"""
        if not data:
            return
        pair = getattr(self, '_pair_bytes_var', None) and self._pair_bytes_var.get()

        def fmt_pairs(chunk):
            parts = []
            for i in range(0, len(chunk), 2):
                if i + 1 < len(chunk):
                    parts.append(f'{chunk[i]:02X}{chunk[i+1]:02X}')
                else:
                    parts.append(f'{chunk[i]:02X}  ')
            return ' '.join(parts)

        if pair:
            # 補回 Windows strip 掉的 2-byte I2C-HID Length field，每行 33 對（66 bytes）
            length_val = len(data) + 2
            extended = bytes([length_val & 0xFF, (length_val >> 8) & 0xFF]) + bytes(data)
            for off in range(0, len(extended), 66):
                chunk = extended[off:off + 66]
                append_fn(f"    {off:04X}:  {fmt_pairs(chunk)}")
        else:
            first = data[:64]
            append_fn(f"    0000:  {' '.join(f'{b:02X}' for b in first)}")
            for off in range(64, len(data), 66):
                chunk = data[off:off + 66]
                append_fn(f"    {off:04X}:  {' '.join(f'{b:02X}' for b in chunk)}")

    def _log_payload(self, report_id: int, payload: list):
        full = [report_id] + list(payload)
        self._send_log_append(f"  實際送出 ({len(full)} bytes):")
        self._log_bytes(full, self._send_log_append)

    def _on_get_report(self):
        if not self._get_cmd_dev():
            self._send_log_append("[錯誤] 請先選擇一個裝置")
            return

        rid_str = self._report_id_var.get().strip()
        try:
            report_id = int(rid_str, 16)
            if not (0 <= report_id <= 255):
                raise ValueError
        except ValueError:
            self._send_log_append("[錯誤] Report ID 必須是 0x00~0xFF 的十六進位數值")
            return

        rtype = self._report_type.get()
        if rtype == "Input":
            length = self._report_length(report_id, REPORT_TYPE_INPUT)
            threading.Thread(
                target=self._do_get_input_report,
                args=(self._get_cmd_dev()["path"], report_id, length),
                daemon=True,
            ).start()
        else:
            length = self._report_length(report_id, REPORT_TYPE_FEATURE)
            threading.Thread(
                target=self._do_get_feature_report,
                args=(self._get_cmd_dev()["path"], report_id, length),
                daemon=True,
            ).start()

    def _report_length(self, report_id: int, report_type: str) -> int:
        path_str   = self._get_dev_path_str(self._get_cmd_dev()) if self._get_cmd_dev() else ""
        fields     = self._descriptors.get(path_str, [])
        total_bits = sum(
            f.bit_size for f in fields
            if f.report_type == report_type and f.report_id == report_id
        )
        payload_len = (total_bits + 7) // 8 if total_bits > 0 else 64
        return payload_len + 1

    def _do_get_feature_report(self, path, report_id: int, length: int):
        self._send_log_append(f"\nGet Feature Report  ID=0x{report_id:02X}  Total Length={length}")
        try:
            data = get_feature_report(path, report_id, length)
            if not data:
                self._send_log_append("  [錯誤] 回傳空資料")
            else:
                self._send_log_append(f"  [成功] {len(data)} bytes:")
                self._log_bytes(data, self._send_log_append)
        except Exception as e:
            self._send_log_append(f"  [錯誤] {e}")

    def _do_get_input_report(self, path, report_id: int, length: int):
        self._send_log_append(f"\nGet Input Report  ID=0x{report_id:02X}  Total Length={length}")
        try:
            data = get_input_report(path, report_id, length)
            if not data:
                self._send_log_append("  [錯誤] 回傳空資料")
            else:
                self._send_log_append(f"  [成功] {len(data)} bytes:")
                self._log_bytes(data, self._send_log_append)
        except Exception as e:
            self._send_log_append(f"  [錯誤] {e}")

    def _send_log_append(self, msg: str):
        def _append():
            self._send_log.configure(state="normal")
            self._send_log.insert("end", msg + "\n")
            self._send_log.see("end")
            self._send_log.configure(state="disabled")
        self.after(0, _append)

    def _clear_send_log(self):
        self._send_log.configure(state="normal")
        self._send_log.delete("1.0", "end")
        self._send_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Canvas tab
    # ------------------------------------------------------------------

    _SLOT_COLORS = [
        "#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6",
        "#1abc9c", "#e67e22", "#34495e", "#e91e63", "#00bcd4",
    ]

    def _build_canvas_panel(self, parent):
        """觸控畫布面板，嵌在監聽分頁右側（可收合）。"""
        info_row = ttk.Frame(parent)
        info_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)

        self._canvas_info_var = tk.StringVar(value="（尚未載入裝置）")
        ttk.Label(info_row, textvariable=self._canvas_info_var,
                  font=("Consolas", 9), style="Muted.TLabel").pack(side=tk.LEFT)

        self._touch_canvas = tk.Canvas(
            parent, bg="#fbfcfd", cursor="crosshair",
            highlightthickness=1, highlightbackground=self._BORDER,
        )
        self._touch_canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self._touch_canvas.bind("<Configure>", self._on_touch_canvas_configure)

    def _on_touch_canvas_configure(self, *_):
        if self._all_digi_mode:
            self._schedule_digi_canvas_redraw()   # 節流，縮放時不會每次都全重繪
        else:
            self._redraw_canvas()

    def _toggle_monitor_canvas(self):
        if self._canvas_shown:
            self._monitor_split.forget(self._canvas_panel)
            self._canvas_shown = False
            self._canvas_toggle_btn.config(text="顯示畫布 ▶")
        else:
            self._monitor_split.add(self._canvas_panel, minsize=260, stretch="always")
            self._canvas_shown = True
            self._canvas_toggle_btn.config(text="隱藏畫布 ◀")
            # 畫布與 Monitor Data 各佔一半
            def place_sash():
                total = self._monitor_split.winfo_width()
                if total > 1:
                    self._monitor_split.sash_place(0, total // 2, 0)
                self._redraw_canvas()
            self.after(30, place_sash)

    def _build_stress_tab(self, parent):
        pad = {"padx": 8, "pady": 6}

        cmd_frame = ttk.LabelFrame(parent, text="壓測後發送指令", padding=8,
                                   style="Section.TLabelframe")
        cmd_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(cmd_frame, text="Report 類型:").grid(row=0, column=0, sticky="w", padx=4)
        self._stress_report_type = tk.StringVar(value="Output")
        ttk.Radiobutton(cmd_frame, text="Output",  variable=self._stress_report_type, value="Output",
                        command=self._stress_update_cmd_ui).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(cmd_frame, text="Feature", variable=self._stress_report_type, value="Feature",
                        command=self._stress_update_cmd_ui).grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(cmd_frame, text="Input",   variable=self._stress_report_type, value="Input",
                        command=self._stress_update_cmd_ui).grid(row=0, column=3, sticky="w")

        self._stress_feature_dir = tk.StringVar(value="Set")
        self._stress_dir_lbl = ttk.Label(cmd_frame, text="方向:")
        self._stress_dir_lbl.grid(row=0, column=4, sticky="w", padx=(20, 4))
        self._stress_dir_set_rb = ttk.Radiobutton(cmd_frame, text="Set", variable=self._stress_feature_dir,
                                                   value="Set", command=self._stress_update_cmd_ui)
        self._stress_dir_set_rb.grid(row=0, column=5, sticky="w")
        self._stress_dir_get_rb = ttk.Radiobutton(cmd_frame, text="Get", variable=self._stress_feature_dir,
                                                   value="Get", command=self._stress_update_cmd_ui)
        self._stress_dir_get_rb.grid(row=0, column=6, sticky="w")

        ttk.Label(cmd_frame, text="Report ID (hex):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self._stress_report_id_var = tk.StringVar(value="01")
        ttk.Entry(cmd_frame, textvariable=self._stress_report_id_var, width=10).grid(row=1, column=1, columnspan=2, sticky="w")

        self._stress_data_lbl = ttk.Label(cmd_frame, text="Data (hex):")
        self._stress_data_lbl.grid(row=2, column=0, sticky="w", padx=4)
        self._stress_data_var = tk.StringVar()
        self._stress_data_entry = ttk.Entry(cmd_frame, textvariable=self._stress_data_var, width=55)
        self._stress_data_entry.grid(row=2, column=1, columnspan=3, sticky="w")

        self._stress_get_len_lbl = ttk.Label(cmd_frame, text="Length (payload bytes):")
        self._stress_get_len_lbl.grid(row=3, column=0, sticky="w", padx=4, pady=4)
        self._stress_get_len_var = tk.StringVar(value="64")
        self._stress_get_len_entry = ttk.Entry(cmd_frame, textvariable=self._stress_get_len_var, width=10)
        self._stress_get_len_entry.grid(row=3, column=1, columnspan=2, sticky="w")

        self._stress_post_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cmd_frame, text="壓測結束後執行一次", variable=self._stress_post_var).grid(
            row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(6, 0))

        settings_frame = ttk.LabelFrame(parent, text="壓測設定", padding=8,
                                        style="Section.TLabelframe")
        settings_frame.pack(fill=tk.X, padx=8, pady=4)

        ttk.Label(settings_frame, text="抬起後延遲 (ms):").grid(row=0, column=0, sticky="w", padx=4)
        self._stress_delay_var = tk.StringVar(value="200")
        ttk.Spinbox(settings_frame, from_=0, to=9999, textvariable=self._stress_delay_var, width=6).grid(row=0, column=1, sticky="w", padx=(2, 20))

        ttk.Label(settings_frame, text="最大次數 (0=無限):").grid(row=0, column=2, sticky="w", padx=4)
        self._stress_max_count_var = tk.StringVar(value="0")
        ttk.Spinbox(settings_frame, from_=0, to=99999, textvariable=self._stress_max_count_var, width=7).grid(row=0, column=3, sticky="w", padx=(2, 20))

        ttk.Label(settings_frame, text="最大時間 (s, 0=無限):").grid(row=0, column=4, sticky="w", padx=4)
        self._stress_max_time_var = tk.StringVar(value="0")
        ttk.Spinbox(settings_frame, from_=0, to=99999, textvariable=self._stress_max_time_var, width=7).grid(row=0, column=5, sticky="w")

        poll_frame = ttk.LabelFrame(parent, text="定時讀取指令", padding=8,
                                    style="Section.TLabelframe")
        poll_frame.pack(fill=tk.X, padx=8, pady=4)

        # Row 0: 間隔 / Report 類型 / Report ID
        ttk.Label(poll_frame, text="間隔 (ms, 0=停用):").grid(row=0, column=0, sticky="w", padx=4)
        self._stress_interval_var = tk.StringVar(value="0")
        ttk.Spinbox(poll_frame, from_=0, to=99999, textvariable=self._stress_interval_var, width=7).grid(row=0, column=1, sticky="w", padx=(2, 20))

        ttk.Label(poll_frame, text="Report 類型:").grid(row=0, column=2, sticky="w", padx=4)
        self._poll_report_type = tk.StringVar(value="Input")
        ttk.Radiobutton(poll_frame, text="Output",  variable=self._poll_report_type, value="Output").grid(row=0, column=3, sticky="w")
        ttk.Radiobutton(poll_frame, text="Feature", variable=self._poll_report_type, value="Feature").grid(row=0, column=4, sticky="w")
        ttk.Radiobutton(poll_frame, text="Input",   variable=self._poll_report_type, value="Input").grid(row=0, column=5, sticky="w")

        ttk.Label(poll_frame, text="Report ID (hex):").grid(row=0, column=6, sticky="w", padx=(16, 4))
        self._poll_report_id_var = tk.StringVar(value="06")
        ttk.Entry(poll_frame, textvariable=self._poll_report_id_var, width=8).grid(row=0, column=7, sticky="w")

        # Row 1: SET Data
        ttk.Label(poll_frame, text="SET Data (hex):").grid(row=1, column=0, sticky="w", padx=4, pady=(6, 0))
        self._poll_data_var = tk.StringVar()
        ttk.Entry(poll_frame, textvariable=self._poll_data_var, width=55).grid(row=1, column=1, columnspan=7, sticky="w", pady=(6, 0))

        # Row 2: GET Length / GET 次數
        ttk.Label(poll_frame, text="GET Length (payload bytes):").grid(row=2, column=0, sticky="w", padx=4, pady=(4, 0))
        self._poll_len_var = tk.StringVar(value="64")
        ttk.Entry(poll_frame, textvariable=self._poll_len_var, width=8).grid(row=2, column=1, sticky="w", pady=(4, 0))

        ttk.Label(poll_frame, text="GET 次數:").grid(row=2, column=2, sticky="w", padx=(16, 4), pady=(4, 0))
        self._poll_count_var = tk.StringVar(value="1")
        ttk.Spinbox(poll_frame, from_=1, to=100, textvariable=self._poll_count_var, width=5).grid(row=2, column=3, sticky="w", pady=(4, 0))

        ctrl_row = ttk.Frame(parent)
        ctrl_row.pack(fill=tk.X, padx=8, pady=4)
        self._stress_start_btn = self._mk_color_button(
            ctrl_row, "開始壓測", self._stress_toggle, self._GREEN, self._GREEN_DARK)
        self._stress_start_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl_row, text="清除記錄", command=self._stress_log_clear).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl_row, text="匯出 CSV", command=self._stress_export_csv).pack(side=tk.LEFT, padx=4)

        stats_frame = ttk.LabelFrame(parent, text="統計", padding=6,
                                     style="Section.TLabelframe")
        stats_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        ttk.Label(stats_frame, text="總次數:").pack(side=tk.LEFT, padx=(4, 2))
        self._stress_count_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self._stress_count_var,
                  font=("Consolas", 12, "bold"), width=6).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(stats_frame, text="失敗:").pack(side=tk.LEFT, padx=(0, 2))
        self._stress_fail_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self._stress_fail_var,
                  font=("Consolas", 12, "bold"), width=6, foreground="#e53935").pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(stats_frame, text="通過:").pack(side=tk.LEFT, padx=(0, 2))
        self._stress_pass_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self._stress_pass_var,
                  font=("Consolas", 12, "bold"), width=6, foreground="#43a047").pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(stats_frame, text="已運行:").pack(side=tk.LEFT, padx=(0, 2))
        self._stress_elapsed_var = tk.StringVar(value="0.0s")
        ttk.Label(stats_frame, textvariable=self._stress_elapsed_var,
                  font=("Consolas", 11), width=8).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(stats_frame, text="狀態:").pack(side=tk.LEFT, padx=(0, 2))
        self._stress_status_var = tk.StringVar(value="就緒")
        ttk.Label(stats_frame, textvariable=self._stress_status_var,
                  font=("Arial", 10), foreground="#555").pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(parent, text="壓測記錄", padding=4,
                                   style="Section.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._stress_log = scrolledtext.ScrolledText(
            log_frame, height=12, state="disabled", font=("Consolas", 9))
        self._style_log_text(self._stress_log)
        self._stress_log.pack(fill=tk.BOTH, expand=True)

        self._stress_update_cmd_ui()

    def _update_canvas_range(self, path_str: str):
        """從 descriptor 取出 X/Y 的 logical 範圍並更新畫布標註。"""
        fields = self._descriptors.get(path_str, [])
        x_field = next(
            (hf for hf in fields
             if hf.report_type == REPORT_TYPE_INPUT and not hf.is_vendor
             and any((hf.usage_page, u) == (0x01, 0x30) for u in hf.usages)),
            None,
        )
        y_field = next(
            (hf for hf in fields
             if hf.report_type == REPORT_TYPE_INPUT and not hf.is_vendor
             and any((hf.usage_page, u) == (0x01, 0x31) for u in hf.usages)),
            None,
        )
        self._canvas_x_logical = (x_field.logical_min, x_field.logical_max) if x_field else (0, 4096)
        self._canvas_y_logical = (y_field.logical_min, y_field.logical_max) if y_field else (0, 4096)
        x_min, x_max = self._canvas_x_logical
        y_min, y_max = self._canvas_y_logical
        self._canvas_info_var.set(
            f"X: {x_min} ~ {x_max}   Y: {y_min} ~ {y_max}"
        )
        self._canvas_contacts.clear()
        self._canvas_trails.clear()
        self._canvas_prev_active.clear()
        self._canvas_item_ids.clear()
        self._canvas_item_shape.clear()
        self._canvas_trail_line_ids.clear()
        self._redraw_canvas()

    def _logical_to_canvas(self, lx: float, ly: float) -> Tuple[float, float]:
        w = self._touch_canvas.winfo_width()
        h = self._touch_canvas.winfo_height()
        pad = 14
        x_min, x_max = self._canvas_x_logical
        y_min, y_max = self._canvas_y_logical
        x_range = max(1, x_max - x_min)
        y_range = max(1, y_max - y_min)
        cx = pad + (lx - x_min) / x_range * (w - 2 * pad)
        cy = pad + (ly - y_min) / y_range * (h - 2 * pad)
        return cx, cy

    def _canvas_update_slot(self, track_key: int, tip, lx, ly, cid_val, confidence="",
                            draw_trail=True, color=None, shape="circle"):
        """純資料更新，不呼叫任何 tkinter — 畫面由 _canvas_flush 統一處理。
        draw_trail=False 時只更新圓點不畫軌跡（手寫筆懸空游標用）。
        color/shape 可覆寫顏色與形狀（circle / square），給手寫筆橡皮擦與懸空游標用。"""
        if tip and lx is not None and ly is not None:
            if draw_trail:
                if track_key not in self._canvas_prev_active:
                    self._canvas_trails.pop(track_key, None)
                    self._canvas_trail_reset_keys.add(track_key)
                trail = self._canvas_trails.setdefault(track_key, collections.deque(maxlen=500))
                if not trail or lx != trail[-1][0] or ly != trail[-1][1]:
                    trail.append((lx, ly))
                self._canvas_dirty_keys.add(track_key)
            else:
                # 懸空游標：不留軌跡，但圓點仍要更新位置
                self._canvas_trails.pop(track_key, None)
                self._canvas_trail_reset_keys.add(track_key)
                self._canvas_dirty_keys.add(track_key)
            try:
                conf_val = int(confidence)
            except (TypeError, ValueError):
                conf_val = 1
            self._canvas_contacts[track_key] = {
                "x": lx, "y": ly, "cid": cid_val, "conf": conf_val,
                "color": color, "shape": shape,
            }
        else:
            self._canvas_contacts.pop(track_key, None)
            self._canvas_circle_del_keys.add(track_key)
            self._canvas_dirty_keys.discard(track_key)
        self._schedule_canvas_flush()

    _PEN_SLOT = 0   # 手寫筆固定使用的畫布 slot（無 ContactID）

    def _feed_pen_canvas(self, get_vals, is_new_frame: bool):
        """單點手寫筆畫布更新：TipSwitch=1 下筆畫軌跡，InRange=1 懸空顯示游標點。"""
        pc = self._pen_canvas
        if not pc:
            return

        def rd(entry):
            if not entry:
                return None
            hf, idx = entry
            vals = get_vals(hf)
            return vals[idx] if idx < len(vals) else None

        if is_new_frame:
            self._canvas_prev_active = set(self._canvas_contacts.keys())

        tip = rd(pc.get("TipSwitch"))
        inr = rd(pc.get("InRange"))
        lx  = rd(pc.get("X"))
        ly  = rd(pc.get("Y"))
        conf = rd(pc.get("Confidence"))
        conf = conf if conf is not None else ""
        eraser = bool(rd(pc.get("Eraser"))) or bool(rd(pc.get("Invert")))

        pen_down = bool(tip)
        hover    = (not pen_down) and bool(inr)
        if pen_down and eraser:
            # 橡皮擦：方形 + 軌跡（不同色）
            self._canvas_update_slot(self._PEN_SLOT, 1, lx, ly, 0, conf,
                                     draw_trail=True, color="#9b59b6", shape="square")
        elif pen_down:
            # 一般筆尖：圓形 + 軌跡
            self._canvas_update_slot(self._PEN_SLOT, 1, lx, ly, 0, conf, draw_trail=True)
        elif hover:
            # 懸空：綠色圓點，不留軌跡
            self._canvas_update_slot(self._PEN_SLOT, 1, lx, ly, 0, conf,
                                     draw_trail=False, color="#2ecc71")
        else:
            self._canvas_update_slot(self._PEN_SLOT, 0, lx, ly, 0, "", draw_trail=True)

    def _schedule_canvas_flush(self):
        if not self._canvas_flush_pending:
            self._canvas_flush_pending = True
            self.after(0, self._canvas_flush)   # event loop 閒置立刻執行

    def _canvas_flush(self):
        """每次 _poll_queue 後執行一次，只處理有新資料的 slot。"""
        self._canvas_flush_pending = False
        # 只在畫布 tab 可見時才渲染
        try:
            canvas_visible = self._touch_canvas.winfo_viewable()
        except Exception:
            canvas_visible = False
        if not canvas_visible:
            self._canvas_trail_reset_keys.clear()
            self._canvas_circle_del_keys.clear()
            self._canvas_dirty_keys.clear()
            return

        c = self._touch_canvas

        # 清除需重置的軌跡線（新按下同一 ID）
        for key in self._canvas_trail_reset_keys:
            old_line = self._canvas_trail_line_ids.pop(key, None)
            if old_line is not None:
                c.delete(old_line)
        self._canvas_trail_reset_keys.clear()

        # 刪除 tip=0 的圓圈
        for key in self._canvas_circle_del_keys:
            ids = self._canvas_item_ids.pop(key, None)
            self._canvas_item_shape.pop(key, None)
            if ids:
                for item_id in ids:
                    c.delete(item_id)
        self._canvas_circle_del_keys.clear()

        if not self._canvas_dirty_keys:
            return

        # 座標轉換常數（只呼叫一次 winfo）
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            self._canvas_dirty_keys.clear()
            return
        pad = 14
        x_min, x_max = self._canvas_x_logical
        y_min, y_max = self._canvas_y_logical
        xs = (w - 2 * pad) / max(1, x_max - x_min)
        ys = (h - 2 * pad) / max(1, y_max - y_min)

        # 只更新有新資料的 slot
        dirty = self._canvas_dirty_keys.copy()
        self._canvas_dirty_keys.clear()
        for track_key in dirty:
            contact = self._canvas_contacts.get(track_key)
            if contact is None:
                continue
            lx   = contact["x"]
            ly   = contact["y"]
            cid  = contact["cid"]
            conf = contact.get("conf", 1)
            color  = contact.get("color") or self._SLOT_COLORS[track_key % len(self._SLOT_COLORS)]
            shape  = contact.get("shape", "circle")
            new_cx = pad + (lx - x_min) * xs
            new_cy = pad + (ly - y_min) * ys

            # confidence=0 → 加粗加大
            r          = 50  if conf == 0 else 20
            line_width = 4   if conf == 0 else 2

            trail = self._canvas_trails.get(track_key)
            if trail and len(trail) >= 2:
                flat: List[float] = []
                for pt_x, pt_y in trail:
                    flat.append(pad + (pt_x - x_min) * xs)
                    flat.append(pad + (pt_y - y_min) * ys)
                line_id = self._canvas_trail_line_ids.get(track_key)
                try:
                    if line_id is None:
                        raise tk.TclError
                    c.coords(line_id, flat)
                    c.itemconfig(line_id, width=line_width, fill=color)
                except tk.TclError:
                    line_id = c.create_line(
                        flat, fill=color, width=line_width,
                        capstyle=tk.ROUND, joinstyle=tk.ROUND,
                        tags=(f"trail_{track_key}", "trail"),
                    )
                    self._canvas_trail_line_ids[track_key] = line_id

            ids = self._canvas_item_ids.get(track_key)
            # 形狀改變（圓⇄方）時需重建，無法只改 coords
            if ids and self._canvas_item_shape.get(track_key) != shape:
                for item_id in ids:
                    c.delete(item_id)
                ids = None
            try:
                if not ids:
                    raise tk.TclError
                oval_id, text_id = ids
                c.coords(oval_id, new_cx - r, new_cy - r, new_cx + r, new_cy + r)
                c.coords(text_id, new_cx, new_cy)
                c.itemconfig(oval_id, fill=color)
            except tk.TclError:
                contact_tag = f"contact_{track_key}"
                _maker = c.create_rectangle if shape == "square" else c.create_oval
                oval_id = _maker(
                    new_cx - r, new_cy - r, new_cx + r, new_cy + r,
                    fill=color, outline="white", width=2,
                    tags=(contact_tag, "contact"),
                )
                text_id = c.create_text(
                    new_cx, new_cy,
                    text=str(cid if cid not in ("", None) else track_key),
                    fill="white", font=("Arial", 10, "bold"),
                    tags=(contact_tag, "contact"),
                )
                self._canvas_item_ids[track_key] = (oval_id, text_id)
                self._canvas_item_shape[track_key] = shape

        c.tag_raise("contact")

    def _redraw_canvas(self):
        """完整重繪（視窗 resize 或清除時呼叫）。"""
        c = self._touch_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return

        pad = 14
        c.create_rectangle(pad, pad, w - pad, h - pad,
                            outline="#cccccc", width=1, dash=(4, 4), tags="border")
        x_min, x_max = self._canvas_x_logical
        y_min, y_max = self._canvas_y_logical
        c.create_text(pad + 2, pad + 2, text=f"({x_min},{y_min})",
                      anchor="nw", font=("Consolas", 7), fill="#aaaaaa", tags="border")
        c.create_text(w - pad - 2, h - pad - 2, text=f"({x_max},{y_max})",
                      anchor="se", font=("Consolas", 7), fill="#aaaaaa", tags="border")

        # Redraw all trails，同時重建 line ID 對照表
        xs = (w - 2 * pad) / max(1, x_max - x_min)
        ys = (h - 2 * pad) / max(1, y_max - y_min)
        new_line_ids: Dict[int, int] = {}
        for slot, trail in self._canvas_trails.items():
            pts = list(trail)
            if len(pts) < 2:
                continue
            _ct = self._canvas_contacts.get(slot)
            color = (_ct.get("color") if _ct else None) or self._SLOT_COLORS[slot % len(self._SLOT_COLORS)]
            flat: List[float] = []
            for lx_t, ly_t in pts:
                flat.append(pad + (lx_t - x_min) * xs)
                flat.append(pad + (ly_t - y_min) * ys)
            line_id = c.create_line(
                flat, fill=color, width=2,
                capstyle=tk.ROUND, joinstyle=tk.ROUND,
                tags=(f"trail_{slot}", "trail"),
            )
            new_line_ids[slot] = line_id
        self._canvas_trail_line_ids = new_line_ids

        # Redraw active contact circles on top，重建 item ID 對照表
        r = 20
        new_ids: Dict[int, Tuple[int, int]] = {}
        new_shapes: Dict[int, str] = {}
        for slot, contact in self._canvas_contacts.items():
            try:
                lx = float(contact["x"])
                ly = float(contact["y"])
            except (TypeError, ValueError):
                continue
            cx = pad + (lx - x_min) * xs
            cy = pad + (ly - y_min) * ys
            color = contact.get("color") or self._SLOT_COLORS[slot % len(self._SLOT_COLORS)]
            shape = contact.get("shape", "circle")
            contact_tag = f"contact_{slot}"
            _maker = c.create_rectangle if shape == "square" else c.create_oval
            oval_id = _maker(cx - r, cy - r, cx + r, cy + r,
                             fill=color, outline="white", width=2,
                             tags=(contact_tag, "contact"))
            cid_label = str(contact.get("cid", slot))
            text_id = c.create_text(cx, cy, text=cid_label,
                                    fill="white", font=("Arial", 10, "bold"),
                                    tags=(contact_tag, "contact"))
            new_ids[slot] = (oval_id, text_id)
            new_shapes[slot] = shape
        self._canvas_item_ids = new_ids
        self._canvas_item_shape = new_shapes

    def _clear_canvas(self):
        self._canvas_contacts.clear()
        self._canvas_trails.clear()
        self._canvas_prev_active.clear()
        self._canvas_item_ids.clear()
        self._canvas_item_shape.clear()
        self._canvas_trail_line_ids.clear()
        # 一併清掉待處理的 key，避免 pending flush 動到已清除的舊狀態
        self._canvas_dirty_keys.clear()
        self._canvas_circle_del_keys.clear()
        self._canvas_trail_reset_keys.clear()
        self._redraw_canvas()

    # ------------------------------------------------------------------
    # Stress test
    # ------------------------------------------------------------------

    def _stress_update_cmd_ui(self):
        rtype = self._stress_report_type.get()
        has_dir = rtype in ("Feature", "Input")
        is_get  = has_dir and self._stress_feature_dir.get() == "Get"
        dir_state = "normal" if has_dir else "disabled"
        self._stress_dir_lbl.config(state=dir_state)
        self._stress_dir_set_rb.config(state=dir_state)
        self._stress_dir_get_rb.config(state=dir_state)
        if is_get:
            self._stress_data_lbl.grid_remove()
            self._stress_data_entry.grid_remove()
            self._stress_get_len_lbl.grid()
            self._stress_get_len_entry.grid()
        else:
            self._stress_get_len_lbl.grid_remove()
            self._stress_get_len_entry.grid_remove()
            self._stress_data_lbl.grid()
            self._stress_data_entry.grid()

    def _stress_toggle(self):
        if self._stress_running:
            self._stress_stop()
        else:
            self._stress_start()

    def _stress_start(self):
        if not self._get_cmd_dev():
            messagebox.showwarning("警告", "請先選擇一個裝置")
            return
        if not self._listening:
            messagebox.showwarning("警告", "請先開始監聽（按上方「開始監聽」按鈕）")
            return
        self._stress_running           = True
        self._stress_count             = 0
        self._stress_fail_count        = 0
        self._stress_start_time        = time.monotonic()
        self._stress_tip_active        = False
        self._stress_touch_had_no_conf = False
        self._stress_pending           = False
        self._stress_records           = []
        self._stress_start_btn.config(text="停止壓測", bg=self._RED, activebackground=self._RED_DARK)
        self._stress_status_var.set("運行中")
        self._stress_log_clear()
        self._stress_log_append("=== 壓測開始 ===")
        self._stress_update_stats()
        self._stress_update_timer()
        try:
            interval = int(self._stress_interval_var.get())
        except ValueError:
            interval = 0
        if interval > 0:
            self._stress_poll_id = self.after(interval, self._stress_poll_tick)

    def _stress_stop(self):
        if not self._stress_running:
            return
        self._stress_running = False
        if self._stress_delay_id:
            self.after_cancel(self._stress_delay_id)
            self._stress_delay_id = None
        if self._stress_poll_id:
            self.after_cancel(self._stress_poll_id)
            self._stress_poll_id = None
        self._stress_pending           = False
        self._stress_tip_active        = False
        self._stress_touch_had_no_conf = False
        elapsed    = time.monotonic() - self._stress_start_time
        pass_count = self._stress_count - self._stress_fail_count
        self._stress_log_append(
            f"=== 壓測結束 === 共 {self._stress_count} 次  "
            f"通過 {pass_count}  失敗 {self._stress_fail_count}  耗時 {elapsed:.1f}s"
        )
        self._stress_start_btn.config(text="開始壓測", bg=self._GREEN, activebackground=self._GREEN_DARK)
        self._stress_status_var.set("已停止")
        self._stress_update_stats()
        if self._stress_post_var.get():
            self._stress_log_append("→ 執行壓測結束後指令...")
            self._stress_send_command()

    def _stress_on_lift_detected(self):
        self._stress_count += 1
        is_fail = self._stress_touch_had_no_conf
        if is_fail:
            self._stress_fail_count += 1
        self._stress_update_stats()
        result = "FAIL" if is_fail else "OK"
        reason = "觸控中無 Confidence" if is_fail else ""
        self._stress_records.append({
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "count":     self._stress_count,
            "result":    result,
            "reason":    reason,
        })
        self._stress_log_append(
            f"[{self._stress_count}] 抬起 [{result}]" + (f" — {reason}" if reason else "")
        )

        try:
            max_count = int(self._stress_max_count_var.get())
        except ValueError:
            max_count = 0
        if max_count > 0 and self._stress_count >= max_count:
            self._stress_log_append(f"[完成] 達到目標次數 {max_count}")
            self.after(0, self._stress_stop)
            return

        elapsed = time.monotonic() - self._stress_start_time
        try:
            max_time = float(self._stress_max_time_var.get())
        except ValueError:
            max_time = 0
        if max_time > 0 and elapsed >= max_time:
            self._stress_log_append(f"[完成] 達到目標時間 {max_time:.0f}s")
            self.after(0, self._stress_stop)
            return

        self._stress_pending = True
        try:
            delay_ms = max(0, int(self._stress_delay_var.get()))
        except ValueError:
            delay_ms = 200
        self._stress_delay_id = self.after(delay_ms, self._stress_send_command)

    def _stress_poll_tick(self):
        if not self._stress_running:
            return
        self._stress_do_poll()
        try:
            interval = int(self._stress_interval_var.get())
        except ValueError:
            interval = 0
        if interval > 0:
            self._stress_poll_id = self.after(interval, self._stress_poll_tick)

    def _stress_do_poll(self):
        cmd_dev = self._get_cmd_dev()
        if not cmd_dev:
            return
        path = cmd_dev["path"]
        rtype = self._poll_report_type.get()
        rid_str = self._poll_report_id_var.get().strip()
        try:
            report_id = int(rid_str, 16)
        except ValueError:
            self._stress_log_append("  [定時] Report ID 格式錯誤")
            return
        try:
            get_count = max(1, int(self._poll_count_var.get()))
        except ValueError:
            get_count = 1
        try:
            length = max(1, int(self._poll_len_var.get())) + 1  # +1 for report ID byte
        except ValueError:
            length = 64
        raw = parse_hex_bytes(self._poll_data_var.get().strip())

        def worker():
            try:
                # SET
                if rtype == "Feature":
                    required = self._report_length(report_id, REPORT_TYPE_FEATURE) - 1
                    send_feature_report(path, report_id, (raw + [0] * required)[:required])
                elif rtype == "Input":
                    required = self._report_length(report_id, REPORT_TYPE_OUTPUT) - 1
                    padded   = (raw + [0] * required)[:required]
                    set_output_report_cmd(path, report_id, padded)
                else:
                    send_output_report(path, report_id, raw)
                self._stress_log_append(f"  [定時] Set {rtype} ID=0x{report_id:02X} OK")

                # GET × N
                for i in range(get_count):
                    if rtype == "Input":
                        data = get_input_report(path, report_id, length)
                    else:
                        data = get_feature_report(path, report_id, length)
                    idx = f"[{i+1}/{get_count}]" if get_count > 1 else ""
                    if not data:
                        self._stress_log_append(f"  [定時] Get {rtype} ID=0x{report_id:02X}{idx}: (空)")
                    else:
                        self._stress_log_append(f"  [定時] Get {rtype} ID=0x{report_id:02X}{idx} ({len(data)} bytes):")
                        self._log_bytes(data, self._stress_log_append)
            except Exception as exc:
                self._stress_log_append(f"  [定時] 錯誤: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _stress_send_command(self):
        self._stress_pending  = False
        self._stress_delay_id = None
        cmd_dev = self._get_cmd_dev()
        if not self._stress_running or not cmd_dev:
            return

        rid_str = self._stress_report_id_var.get().strip()
        try:
            report_id = int(rid_str, 16)
            if not (0 <= report_id <= 255):
                raise ValueError
        except ValueError:
            self._stress_log_append("[錯誤] Report ID 格式錯誤")
            return

        rtype   = self._stress_report_type.get()
        use_get = rtype in ("Feature", "Input") and self._stress_feature_dir.get() == "Get"
        path    = cmd_dev["path"]

        if use_get:
            try:
                length = int(self._stress_get_len_var.get()) + 1  # +1 for report ID byte
                if length <= 1:
                    raise ValueError
            except ValueError:
                self._stress_log_append("[錯誤] Length 格式錯誤")
                return

            def worker():
                try:
                    if rtype == "Input":
                        recv = get_input_report(path, report_id, length)
                        tag  = "Input"
                    else:
                        recv = get_feature_report(path, report_id, length)
                        tag  = "Feature"
                    if not recv:
                        self._stress_log_append(f"  → Get {tag} ID=0x{report_id:02X} 回傳空資料")
                    else:
                        self._stress_log_append(f"  → Get {tag} ID=0x{report_id:02X} ({len(recv)} bytes):")
                        self._log_bytes(recv, self._stress_log_append)
                except Exception as exc:
                    self._stress_log_append(f"  → [錯誤] {exc}")
        else:
            try:
                data = parse_hex_bytes(self._stress_data_var.get().strip())
            except ValueError:
                self._stress_log_append("[錯誤] Data 格式錯誤")
                return

            def worker():
                try:
                    if rtype == "Feature":
                        required = self._report_length(report_id, REPORT_TYPE_FEATURE) - 1
                        padded   = (data + [0] * required)[:required]
                        sent = send_feature_report(path, report_id, padded)
                        if sent < 0:
                            self._stress_log_append(f"  → 發送 Feature 失敗 (回傳 {sent})")
                        else:
                            self._stress_log_append(f"  → 發送 Feature ID=0x{report_id:02X} OK ({sent} bytes)")
                    elif rtype == "Input":
                        required = self._report_length(report_id, REPORT_TYPE_OUTPUT) - 1
                        padded   = (data + [0] * required)[:required]
                        set_output_report_cmd(path, report_id, padded)
                        self._stress_log_append(f"  → SET_REPORT(Output) ID=0x{report_id:02X} OK")
                    else:
                        sent = send_output_report(path, report_id, data)
                        if sent < 0:
                            self._stress_log_append(f"  → 發送 Output 失敗 (回傳 {sent})")
                        else:
                            self._stress_log_append(f"  → 發送 Output ID=0x{report_id:02X} OK ({sent} bytes)")
                except Exception as exc:
                    self._stress_log_append(f"  → [錯誤] {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _stress_update_stats(self):
        pass_count = self._stress_count - self._stress_fail_count
        self._stress_count_var.set(str(self._stress_count))
        self._stress_fail_var.set(str(self._stress_fail_count))
        self._stress_pass_var.set(str(pass_count))
        if self._stress_running:
            elapsed = time.monotonic() - self._stress_start_time
            self._stress_elapsed_var.set(f"{elapsed:.1f}s")
        else:
            self._stress_elapsed_var.set("0.0s")

    def _stress_update_timer(self):
        if not self._stress_running:
            return
        elapsed = time.monotonic() - self._stress_start_time
        self._stress_count_var.set(str(self._stress_count))
        self._stress_elapsed_var.set(f"{elapsed:.1f}s")

        try:
            max_time = float(self._stress_max_time_var.get())
        except ValueError:
            max_time = 0
        if max_time > 0 and elapsed >= max_time and not self._stress_pending:
            self._stress_log_append(f"[完成] 達到目標時間 {max_time:.0f}s")
            self._stress_stop()
            return

        self.after(500, self._stress_update_timer)

    _STRESS_LOG_MAX_LINES = 500

    def _stress_log_append(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {msg}"
        def _do():
            self._stress_log.configure(state="normal")
            self._stress_log.insert("end", line + "\n")
            # 超過上限時刪除最舊的 100 行，避免長時間累積
            line_count = int(self._stress_log.index("end-1c").split(".")[0])
            if line_count > self._STRESS_LOG_MAX_LINES:
                self._stress_log.delete("1.0", "101.0")
            self._stress_log.see("end")
            self._stress_log.configure(state="disabled")
        self.after(0, _do)

    def _stress_log_clear(self):
        self._stress_log.configure(state="normal")
        self._stress_log.delete("1.0", "end")
        self._stress_log.configure(state="disabled")

    def _stress_export_csv(self):
        if not self._stress_records:
            messagebox.showinfo("提示", "尚無壓測記錄可匯出")
            return
        default_name = f"stress_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=default_name,
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["timestamp", "count", "result", "reason"])
                writer.writeheader()
                writer.writerows(self._stress_records)
            messagebox.showinfo("完成", f"已匯出 {len(self._stress_records)} 筆\n{path}")
        except Exception as e:
            messagebox.showerror("錯誤", f"匯出失敗: {e}")

    # ------------------------------------------------------------------
    # Heatmap tab — 韌體觸控矩陣 TXT 轉熱圖
    # ------------------------------------------------------------------

    def _build_heatmap_tab(self, parent):
        pad = {"padx": 8, "pady": 6}

        # 檔案選擇
        file_row = ttk.LabelFrame(parent, text="Differ 資料來源", padding=(8, 6),
                                  style="Section.TLabelframe")
        file_row.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(file_row, text="TXT 檔案:", width=8, anchor="e").pack(side=tk.LEFT)
        self._hm_path_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self._hm_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 6))
        ttk.Button(file_row, text="瀏覽…", command=self._hm_browse).pack(side=tk.LEFT)

        # 參數
        param_row = ttk.LabelFrame(parent, text="熱圖設定", padding=(8, 6),
                                   style="Section.TLabelframe")
        param_row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(param_row, text="vmin:").pack(side=tk.LEFT)
        self._hm_vmin_var = tk.StringVar(value="-1000")
        e_vmin = ttk.Entry(param_row, textvariable=self._hm_vmin_var, width=8)
        e_vmin.pack(side=tk.LEFT, padx=(2, 12))
        ttk.Label(param_row, text="vmax:").pack(side=tk.LEFT)
        self._hm_vmax_var = tk.StringVar(value="1000")
        e_vmax = ttk.Entry(param_row, textvariable=self._hm_vmax_var, width=8)
        e_vmax.pack(side=tk.LEFT, padx=(2, 12))

        ttk.Label(param_row, text="colormap:").pack(side=tk.LEFT)
        self._hm_cmap_var = tk.StringVar(value="seismic")
        cmap_cb = ttk.Combobox(param_row, textvariable=self._hm_cmap_var, width=12,
                               state="readonly", values=heatmap_frame.CMAP_NAMES)
        cmap_cb.pack(side=tk.LEFT, padx=(2, 12))

        ttk.Label(param_row, text="每檔 Frames:").pack(side=tk.LEFT, padx=(8, 0))
        self._hm_chunk_var = tk.StringVar(value="500")
        ttk.Spinbox(param_row, from_=10, to=99999, increment=50,
                    textvariable=self._hm_chunk_var, width=7).pack(side=tk.LEFT, padx=(2, 0))

        for w in (e_vmin, e_vmax):
            w.bind("<Return>", lambda _e: self._hm_on_range_changed())
            w.bind("<FocusOut>", lambda _e: self._hm_on_range_changed())
        cmap_cb.bind("<<ComboboxSelected>>", lambda _e: self._hm_on_cmap_changed())

        # colormap 預覽色帶
        self._hm_cmap_canvas = tk.Canvas(parent, height=38, bg=self._SURFACE,
                                         highlightthickness=1, highlightbackground=self._BORDER)
        self._hm_cmap_canvas.pack(fill=tk.X, padx=8, pady=(0, 2))
        self._hm_cmap_canvas.bind("<Configure>", lambda _e: self._hm_redraw_cmap_preview())

        # frame 導覽
        nav_row = ttk.Frame(parent)
        nav_row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(nav_row, text="◀", width=3,
                   command=lambda: self._hm_step_frame(-1)).pack(side=tk.LEFT)
        self._hm_frame_var = tk.IntVar(value=0)
        self._hm_scale = ttk.Scale(nav_row, from_=0, to=0, orient=tk.HORIZONTAL,
                                   command=self._hm_on_scale)
        self._hm_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(nav_row, text="▶", width=3,
                   command=lambda: self._hm_step_frame(1)).pack(side=tk.LEFT)
        self._hm_frame_lbl = tk.StringVar(value="Frame - / -")
        ttk.Label(nav_row, textvariable=self._hm_frame_lbl, width=18,
                  font=("Consolas", 9)).pack(side=tk.LEFT, padx=(8, 0))

        self._hm_play_btn = ttk.Button(nav_row, text="▶ 播放", width=8,
                                       command=self._hm_toggle_play)
        self._hm_play_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(nav_row, text="速度:").pack(side=tk.LEFT, padx=(8, 2))
        self._hm_speed_var = tk.StringVar(value="10 fps")
        ttk.Combobox(nav_row, textvariable=self._hm_speed_var, width=8, state="readonly",
                     values=("2 fps", "5 fps", "10 fps", "15 fps", "20 fps", "30 fps"),
                     ).pack(side=tk.LEFT)
        self._hm_loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(nav_row, text="循環", variable=self._hm_loop_var).pack(side=tk.LEFT, padx=(8, 0))

        # frame 熱圖畫布
        self._hm_canvas = tk.Canvas(parent, bg=self._SURFACE, highlightthickness=1,
                                    highlightbackground=self._BORDER)
        self._hm_canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._hm_canvas.bind("<Configure>", lambda _e: self._hm_request_redraw())

        # 匯出 / 進度
        bottom = ttk.Frame(parent)
        bottom.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._hm_export_btn = ttk.Button(bottom, text="匯出 HTML", command=self._hm_export)
        self._hm_export_btn.pack(side=tk.LEFT)
        self._hm_cancel_btn = ttk.Button(bottom, text="取消", command=self._hm_cancel_export,
                                         state="disabled")
        self._hm_cancel_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(bottom, text="開啟輸出資料夾",
                   command=self._hm_open_outdir).pack(side=tk.LEFT, padx=6)
        self._hm_progress = tk.DoubleVar(value=0.0)
        ttk.Progressbar(bottom, variable=self._hm_progress, maximum=100).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 6))
        self._hm_status_var = tk.StringVar(value="尚未載入檔案")
        ttk.Label(bottom, textvariable=self._hm_status_var, style="Muted.TLabel",
                  width=22, anchor=tk.E).pack(side=tk.LEFT)

        self._hm_redraw_cmap_preview()

    def _hm_browse(self):
        path = filedialog.askopenfilename(
            title="選擇觸控矩陣 TXT",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self._hm_path_var.set(path)
            self._hm_load_file(path)

    def _hm_load_file(self, path: str):
        self._hm_stop_play()
        self._hm_status_var.set("解析中…")

        def worker():
            try:
                frames, used_tx, stats = heatmap_frame.parse_frames(path)
            except Exception as exc:
                self.after(0, lambda: self._hm_status_var.set(f"解析失敗: {exc}"))
                return

            def apply():
                self._hm_frames = frames
                self._hm_used_tx = used_tx
                self._hm_cur_frame = 0
                self._hm_output_files = []
                n = len(frames)
                if n == 0:
                    self._hm_scale.configure(to=0)
                    self._hm_frame_lbl.set("Frame - / -")
                    self._hm_canvas.delete("all")
                    self._hm_status_var.set("檔案中沒有 frame")
                    return
                self._hm_scale.configure(to=n - 1)
                self._hm_scale.set(0)
                msg = f"{n} frames, Used_Tx={used_tx}"
                if stats["skipped_lines"]:
                    msg += f", 略過 {stats['skipped_lines']} 行"
                self._hm_status_var.set(msg)
                self._hm_redraw_frame()

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _hm_parse_range(self) -> Optional[Tuple[float, float]]:
        try:
            vmin = float(self._hm_vmin_var.get())
            vmax = float(self._hm_vmax_var.get())
        except ValueError:
            return None
        if vmax <= vmin:
            return None
        return vmin, vmax

    def _hm_on_range_changed(self):
        self._hm_redraw_cmap_preview()
        self._hm_request_redraw()

    def _hm_on_cmap_changed(self):
        self._hm_lut = heatmap_frame.build_lut(self._hm_cmap_var.get())
        self._hm_redraw_cmap_preview()
        self._hm_request_redraw()

    def _hm_redraw_cmap_preview(self):
        c = self._hm_cmap_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return
        rng = self._hm_parse_range()
        if rng is None:
            c.create_text(8, h // 2, anchor="w", fill=self._RED,
                          text="vmin / vmax 無效（需數值且 vmax > vmin）")
            return
        vmin, vmax = rng
        pad_x = 8
        bar_w = max(1, w - 2 * pad_x)
        bar_h = 14
        y0 = 4
        lut = self._hm_lut
        n = len(lut)
        for i in range(bar_w):
            bg = lut[int(i / bar_w * (n - 1))][0]
            x = pad_x + i
            c.create_line(x, y0, x, y0 + bar_h, fill=bg)
        c.create_rectangle(pad_x, y0, pad_x + bar_w, y0 + bar_h, outline="#888")
        ly = y0 + bar_h + 3
        c.create_text(pad_x, ly, anchor="nw", fill=self._TEXT_MUTED,
                      font=("Consolas", 8), text=f"{vmin:g}")
        c.create_text(pad_x + bar_w // 2, ly, anchor="n", fill=self._TEXT_MUTED,
                      font=("Consolas", 8), text=f"{(vmin + vmax) / 2:g}")
        c.create_text(pad_x + bar_w, ly, anchor="ne", fill=self._TEXT_MUTED,
                      font=("Consolas", 8), text=f"{vmax:g}")

    def _hm_on_scale(self, _val):
        idx = int(float(self._hm_scale.get()))
        if idx != self._hm_cur_frame:
            self._hm_cur_frame = idx
            self._hm_request_redraw()

    def _hm_step_frame(self, delta: int):
        if not self._hm_frames:
            return
        idx = max(0, min(len(self._hm_frames) - 1, self._hm_cur_frame + delta))
        if idx != self._hm_cur_frame:
            self._hm_cur_frame = idx
            self._hm_scale.set(idx)
            self._hm_request_redraw()

    def _hm_toggle_play(self):
        if self._hm_playing:
            self._hm_stop_play()
        else:
            self._hm_start_play()

    def _hm_start_play(self):
        if not self._hm_frames or len(self._hm_frames) < 2:
            return
        # 已在最後一張且不循環 → 從頭播
        if not self._hm_loop_var.get() and self._hm_cur_frame >= len(self._hm_frames) - 1:
            self._hm_cur_frame = 0
            self._hm_scale.set(0)
            self._hm_request_redraw()
        self._hm_playing = True
        self._hm_play_btn.config(text="⏸ 暫停")
        self._hm_play_tick()

    def _hm_stop_play(self):
        self._hm_playing = False
        if self._hm_play_id:
            self.after_cancel(self._hm_play_id)
            self._hm_play_id = None
        self._hm_play_btn.config(text="▶ 播放")

    def _hm_play_tick(self):
        self._hm_play_id = None
        if not self._hm_playing or not self._hm_frames:
            return
        last = len(self._hm_frames) - 1
        nxt = self._hm_cur_frame + 1
        if nxt > last:
            if self._hm_loop_var.get():
                nxt = 0
            else:
                self._hm_stop_play()
                return
        self._hm_cur_frame = nxt
        self._hm_scale.set(nxt)
        self._hm_request_redraw()
        try:
            fps = int(self._hm_speed_var.get().split()[0])
        except (ValueError, IndexError):
            fps = 10
        self._hm_play_id = self.after(max(16, int(1000 / max(1, fps))), self._hm_play_tick)

    def _hm_request_redraw(self):
        """節流：合併連續的重畫請求到 idle 時做一次。"""
        if not self._hm_redraw_pending:
            self._hm_redraw_pending = True
            self.after_idle(self._hm_redraw_frame)

    def _hm_redraw_frame(self):
        self._hm_redraw_pending = False
        c = self._hm_canvas
        c.delete("all")
        if not self._hm_frames:
            return
        idx = self._hm_cur_frame
        self._hm_frame_lbl.set(f"Frame {idx} / {len(self._hm_frames) - 1}")
        frame = self._hm_frames[idx]
        rows = len(frame)
        cols = max((len(r) for r in frame), default=0)
        if rows == 0 or cols == 0:
            return
        rng = self._hm_parse_range()
        if rng is None:
            c.create_text(10, 10, anchor="nw", fill=self._RED,
                          text="vmin / vmax 無效")
            return
        vmin, vmax = rng

        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return
        pad = 6
        gap = 4 if (self._hm_used_tx is not None and 0 < self._hm_used_tx < rows) else 0
        cell_w = (w - 2 * pad) / cols
        cell_h = (h - 2 * pad - gap) / rows
        if cell_w <= 0 or cell_h <= 0:
            return
        show_text = cell_w >= 14 and cell_h >= 8
        font_sz = 8 if (cell_w >= 22 and cell_h >= 13) else 7
        lut = self._hm_lut

        for r in range(rows):
            row = frame[r]
            y_off = gap if (gap and r >= self._hm_used_tx) else 0
            y0 = pad + r * cell_h + y_off
            for col in range(cols):
                val = row[col] if col < len(row) else None
                bg, txt = heatmap_frame.cell_color(val, vmin, vmax, lut)
                x0 = pad + col * cell_w
                c.create_rectangle(x0, y0, x0 + cell_w, y0 + cell_h,
                                   fill=bg, outline=bg)
                if show_text and val is not None:
                    c.create_text(x0 + cell_w - 2, y0 + cell_h / 2, anchor="e",
                                  text=str(val), fill=txt, font=("Consolas", font_sz))

    def _hm_compute_workers(self) -> int:
        cores = os.cpu_count() or 4
        return max(1, min(4, cores - 1))

    def _hm_export(self):
        if self._hm_worker and self._hm_worker.is_alive():
            return
        if not self._hm_frames:
            messagebox.showinfo("匯出 HTML", "請先載入有資料的 TXT 檔。")
            return
        rng = self._hm_parse_range()
        if rng is None:
            messagebox.showerror("匯出 HTML", "vmin / vmax 無效（需數值且 vmax > vmin）。")
            return
        try:
            chunk = max(1, int(self._hm_chunk_var.get()))
        except ValueError:
            messagebox.showerror("匯出 HTML", "每檔 Frames 需為正整數。")
            return
        vmin, vmax = rng
        path = self._hm_path_var.get().strip()
        lut = self._hm_lut
        frames = self._hm_frames
        used_tx = self._hm_used_tx
        workers = self._hm_compute_workers()

        self._hm_set_running(True)
        self._hm_progress.set(0)
        self._hm_status_var.set("匯出中… 0%")
        self._hm_cancel = threading.Event()
        cancel = self._hm_cancel

        def progress_cb(done, total):
            self._hm_progress_q.put(("progress", done, total))

        def worker():
            try:
                outs = heatmap_frame.export_html(
                    frames, used_tx, vmin, vmax, lut, path,
                    chunk_size=chunk, progress_cb=progress_cb,
                    cancel_event=cancel, max_workers=workers,
                )
                if cancel.is_set():
                    self._hm_progress_q.put(("done", False, "已取消", []))
                else:
                    self._hm_progress_q.put(("done", True, f"完成 {len(outs)} 個檔", outs))
            except Exception as exc:
                self._hm_progress_q.put(("done", False, f"失敗: {exc}", []))

        self._hm_worker = threading.Thread(target=worker, daemon=True)
        self._hm_worker.start()
        self._hm_schedule_drain()

    def _hm_cancel_export(self):
        if self._hm_cancel:
            self._hm_cancel.set()
            self._hm_status_var.set("取消中…")

    def _hm_schedule_drain(self):
        if self._hm_drain_id is None:
            self._hm_drain_id = self.after(100, self._hm_drain_queue)

    def _hm_drain_queue(self):
        self._hm_drain_id = None
        try:
            while True:
                kind, *rest = self._hm_progress_q.get_nowait()
                if kind == "progress":
                    done, total = rest
                    pct = done * 100.0 / max(1, total)
                    self._hm_progress.set(pct)
                    self._hm_status_var.set(f"匯出中… {pct:.0f}%")
                elif kind == "done":
                    ok, msg, outs = rest
                    self._hm_output_files = outs
                    self._hm_set_running(False)
                    self._hm_progress.set(100 if ok else 0)
                    self._hm_status_var.set(msg)
                    if ok and outs:
                        try:
                            webbrowser.open(outs[0])
                        except Exception:
                            pass
        except queue.Empty:
            pass
        if self._hm_worker and self._hm_worker.is_alive():
            self._hm_schedule_drain()

    def _hm_open_outdir(self):
        if not self._hm_output_files:
            messagebox.showinfo("提示", "尚未匯出任何 HTML。")
            return
        outdir = os.path.dirname(self._hm_output_files[0])
        try:
            webbrowser.open(outdir)
        except Exception:
            messagebox.showwarning("警告", f"無法開啟資料夾：{outdir}")

    def _hm_set_running(self, running: bool):
        if running:
            self._hm_export_btn.configure(state="disabled")
            self._hm_cancel_btn.configure(state="normal")
        else:
            self._hm_export_btn.configure(state="normal")
            self._hm_cancel_btn.configure(state="disabled")
            self._hm_cancel = None
            self._hm_worker = None

    # ------------------------------------------------------------------
    # DigiInfo XML 軌跡分頁
    # ------------------------------------------------------------------

    def _build_digi_tab(self, parent):
        pad = {"padx": 8, "pady": 6}

        # 檔案載入
        file_row = ttk.LabelFrame(parent, text="DigiInfo 資料來源", padding=(8, 6),
                                  style="Section.TLabelframe")
        file_row.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(file_row, text="XML 檔案:", width=8, anchor="e").pack(side=tk.LEFT)
        self._digi_path_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self._digi_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 6))
        ttk.Button(file_row, text="瀏覽…", command=self._digi_browse).pack(side=tk.LEFT)
        ttk.Button(file_row, text="載入", command=self._digi_load).pack(side=tk.LEFT, padx=(4, 0))

        # 繪圖控制
        ctrl = ttk.LabelFrame(parent, text="顯示設定", padding=(8, 6),
                              style="Section.TLabelframe")
        ctrl.pack(fill=tk.X, padx=8, pady=4)
        self._digi_highlight = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="標示 palm point", variable=self._digi_highlight,
                        command=self._digi_request_redraw).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(ctrl, text="標示大小:").pack(side=tk.LEFT)
        self._digi_marker_var = tk.StringVar(value="16")
        ttk.Spinbox(ctrl, from_=6, to=60, increment=2, width=5,
                    textvariable=self._digi_marker_var,
                    command=self._digi_request_redraw).pack(side=tk.LEFT, padx=(2, 12))
        ttk.Label(ctrl, text="X max:").pack(side=tk.LEFT)
        self._digi_xmax_var = tk.StringVar()
        e_x = ttk.Entry(ctrl, textvariable=self._digi_xmax_var, width=7)
        e_x.pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(ctrl, text="Y max:").pack(side=tk.LEFT)
        self._digi_ymax_var = tk.StringVar()
        e_y = ttk.Entry(ctrl, textvariable=self._digi_ymax_var, width=7)
        e_y.pack(side=tk.LEFT, padx=(2, 8))
        ttk.Button(ctrl, text="重設座標軸", command=self._digi_reset_axes).pack(side=tk.LEFT)
        for w in (e_x, e_y):
            w.bind("<Return>", lambda _e: self._digi_request_redraw())
            w.bind("<FocusOut>", lambda _e: self._digi_request_redraw())

        # 導覽 + 播放
        nav = ttk.Frame(parent)
        nav.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(nav, text="◀", width=3,
                   command=lambda: self._digi_step(-1)).pack(side=tk.LEFT)
        self._digi_scale = ttk.Scale(nav, from_=0, to=0, orient=tk.HORIZONTAL,
                                     command=self._digi_on_scale)
        self._digi_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(nav, text="▶", width=3,
                   command=lambda: self._digi_step(1)).pack(side=tk.LEFT)
        self._digi_frame_lbl = tk.StringVar(value="Frame - / -")
        ttk.Label(nav, textvariable=self._digi_frame_lbl, width=16,
                  font=("Consolas", 9)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(nav, text="設目前幀", width=8,
                   command=self._digi_set_start_to_cur).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(nav, text="起始幀:").pack(side=tk.LEFT, padx=(8, 2))
        self._digi_start_var = tk.StringVar(value="0")
        self._digi_start_spin = ttk.Spinbox(nav, from_=0, to=0, width=6,
                                            textvariable=self._digi_start_var)
        self._digi_start_spin.pack(side=tk.LEFT)
        # 打字/按箭頭即時套用，不必按 Enter
        self._digi_start_var.trace_add("write", lambda *_a: self._digi_on_start_changed())
        self._digi_play_btn = ttk.Button(nav, text="▶ 播放", width=8,
                                         command=self._digi_toggle_play)
        self._digi_play_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(nav, text="速度:").pack(side=tk.LEFT, padx=(8, 2))
        self._digi_speed_var = tk.StringVar(value="30 fps")
        ttk.Combobox(nav, textvariable=self._digi_speed_var, width=8, state="readonly",
                     values=("5 fps", "10 fps", "20 fps", "30 fps", "60 fps",
                             "120 fps", "240 fps", "480 fps"),
                     ).pack(side=tk.LEFT)
        self._digi_loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(nav, text="循環", variable=self._digi_loop_var).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(nav, text="顯示全部",
                   command=self._digi_show_all).pack(side=tk.LEFT, padx=(8, 0))

        # 底部：表格切換 / 匯出 / 進度
        bottom = ttk.Frame(parent)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 8))
        self._digi_table_btn = ttk.Button(bottom, text="顯示表格 ▼",
                                          command=self._digi_toggle_table)
        self._digi_table_btn.pack(side=tk.LEFT)
        self._digi_preview_mode = tk.StringVar(value="wide")
        ttk.Radiobutton(bottom, text="frame(寬)", value="wide", variable=self._digi_preview_mode,
                        command=self._digi_refresh_table).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Radiobutton(bottom, text="log(長)", value="long", variable=self._digi_preview_mode,
                        command=self._digi_refresh_table).pack(side=tk.LEFT)
        ttk.Button(bottom, text="匯出 CSV", command=self._digi_export_csv).pack(side=tk.LEFT, padx=10)
        self._digi_progress = tk.DoubleVar(value=0.0)
        ttk.Progressbar(bottom, variable=self._digi_progress, maximum=100).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 6))
        self._digi_status_var = tk.StringVar(value="尚未載入檔案")
        ttk.Label(bottom, textvariable=self._digi_status_var, style="Muted.TLabel",
                  width=26, anchor=tk.E).pack(side=tk.LEFT)

        # 表格（預設隱藏，想看再開）
        self._digi_table_frame = ttk.Frame(parent)
        tbl_inner = ttk.Frame(self._digi_table_frame)
        tbl_inner.pack(fill=tk.BOTH, expand=True)
        self._digi_tree = ttk.Treeview(tbl_inner, show="headings", selectmode="browse",
                                       style="Mono.Treeview", height=8)
        dvsb = ttk.Scrollbar(tbl_inner, orient=tk.VERTICAL, command=self._digi_tree.yview)
        dhsb = ttk.Scrollbar(self._digi_table_frame, orient=tk.HORIZONTAL, command=self._digi_tree.xview)
        self._digi_tree.configure(yscrollcommand=dvsb.set, xscrollcommand=dhsb.set)
        dvsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._digi_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dhsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._digi_tree.bind("<<TreeviewSelect>>", self._digi_on_tree_select)
        # 注意：_digi_table_frame 尚未 pack（隱藏狀態）

        # 軌跡畫布：手（觸控）與筆各一張，座標軸獨立（兩者座標範圍差很多）
        canv_wrap = ttk.Frame(parent)
        canv_wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        touch_col = ttk.Frame(canv_wrap)
        touch_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        ttk.Label(touch_col, text="手 (Touch)", style="Muted.TLabel").pack(anchor="w")
        self._digi_canvas_touch = tk.Canvas(touch_col, bg=self._SURFACE, highlightthickness=1,
                                            highlightbackground=self._BORDER)
        self._digi_canvas_touch.pack(fill=tk.BOTH, expand=True)
        self._digi_canvas_touch.bind("<Configure>", lambda _e: self._digi_request_redraw())

        pen_col = ttk.Frame(canv_wrap)
        pen_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        ttk.Label(pen_col, text="筆 (Pen)", style="Muted.TLabel").pack(anchor="w")
        self._digi_canvas_pen = tk.Canvas(pen_col, bg=self._SURFACE, highlightthickness=1,
                                          highlightbackground=self._BORDER)
        self._digi_canvas_pen.pack(fill=tk.BOTH, expand=True)
        self._digi_canvas_pen.bind("<Configure>", lambda _e: self._digi_request_redraw())

    # ---- 載入 / 解析 ----

    def _digi_browse(self):
        path = filedialog.askopenfilename(
            title="選擇 DigiInfo XML",
            filetypes=[("All files", "*.*"), ("XML files", "*.xml")],
        )
        if path:
            self._digi_path_var.set(path)
            self._digi_load()

    def _digi_load(self):
        path = self._digi_path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("載入", "請先選擇有效的 XML 檔。")
            return
        self._digi_stop_play()
        self._digi_status_var.set("解析中… 0%")
        self._digi_progress.set(0)

        def pcb(pct):
            self.after(0, lambda: (self._digi_progress.set(pct),
                                   self._digi_status_var.set(f"解析中… {pct}%")))

        def worker():
            try:
                res = digiinfo_parse.parse_digiinfo_xml(path, progress_cb=pcb)
            except Exception as exc:
                self.after(0, lambda: self._digi_status_var.set(f"解析失敗: {exc}"))
                return
            self.after(0, lambda: self._digi_apply(res))

        threading.Thread(target=worker, daemon=True).start()

    def _digi_apply(self, res: dict):
        self._digi_frames = res["frames"]
        self._digi_wide_rows = res["wide_rows"]
        self._digi_wide_cols = res["wide_cols"]
        self._digi_long_rows = res["long_rows"]
        self._digi_long_cols = res["long_cols"]
        self._digi_stats = res["stats"]
        self._digi_cur = 0
        n = len(self._digi_frames)
        self._digi_progress.set(100)
        if n == 0:
            self._digi_scale.configure(to=0)
            self._digi_frame_lbl.set("Frame - / -")
            self._digi_canvas_touch.delete("all")
            self._digi_canvas_pen.delete("all")
            self._digi_status_var.set("沒有解析到觸控資料")
            return
        self._digi_compute_bounds()   # 先用資料範圍當後備
        # 有 <digitizers> 表頭宣告的範圍時，改用裝置真實座標空間（較準）
        bt = res.get("bounds_touch")
        bp = res.get("bounds_pen")
        if bt:
            self._digi_bounds_touch = bt
        if bp:
            self._digi_bounds_pen = bp
        self._digi_scale.configure(to=n - 1)
        self._digi_scale.set(0)
        self._digi_start = 0
        self._digi_start_var.set("0")
        self._digi_start_spin.configure(to=n - 1)
        st = self._digi_stats
        self._digi_status_var.set(
            f"{n} frames | 點數 {st['total_points']} | 觸控 ID 數 {self._digi_contact_count()}"
        )
        self._digi_cur = 0   # 載入後從第一幀開始（不直接顯示全部）
        self._digi_scale.set(0)
        self._digi_redraw()
        if self._digi_table_visible:
            self._digi_refresh_table()

    def _digi_contact_count(self) -> int:
        cids = set()
        for fr in self._digi_frames:
            cids.update(fr["contacts"].keys())
        return len(cids)

    # 合成 contactid 的起點（無 contactid 的裝置如手寫筆 = 90+digitizer），
    # 用來把「筆」與「觸控」分到不同畫布。
    _DIGI_PEN_CID_BASE = 90

    @classmethod
    def _digi_is_pen_cid(cls, cid) -> bool:
        try:
            return int(cid) >= cls._DIGI_PEN_CID_BASE
        except (TypeError, ValueError):
            return False

    def _digi_compute_bounds(self):
        def _bounds(is_pen: bool):
            xs, ys = [], []
            for fr in self._digi_frames:
                for cid, c in fr["contacts"].items():
                    if self._digi_is_pen_cid(cid) == is_pen:
                        xs.append(c["x"]); ys.append(c["y"])
            if not xs:
                return (0.0, 1.0, 0.0, 1.0)
            # 從 0 起算、上界留 5% 邊
            return (0.0, max(1.0, max(xs) * 1.05), 0.0, max(1.0, max(ys) * 1.05))
        self._digi_bounds_touch = _bounds(False)
        self._digi_bounds_pen = _bounds(True)

    def _digi_axis(self, bounds) -> Tuple[float, float, float, float]:
        x0, x1, y0, y1 = bounds
        try:
            ux = float(self._digi_xmax_var.get())
            if ux > 0:
                x1 = ux
        except ValueError:
            pass
        try:
            uy = float(self._digi_ymax_var.get())
            if uy > 0:
                y1 = uy
        except ValueError:
            pass
        return x0, x1, y0, y1

    def _digi_reset_axes(self):
        self._digi_xmax_var.set("")
        self._digi_ymax_var.set("")
        self._digi_request_redraw()

    def _digi_get_start(self) -> int:
        n = len(self._digi_frames)
        if n == 0:
            return 0
        try:
            s = int(float(self._digi_start_var.get()))
        except ValueError:
            s = 0
        return max(0, min(n - 1, s))

    def _digi_on_start_changed(self):
        self._digi_start = self._digi_get_start()
        # 目前幀若落在起點之前，拉到起點
        if self._digi_cur < self._digi_start:
            self._digi_cur = self._digi_start
            self._digi_scale.set(self._digi_cur)
        self._digi_request_redraw()

    def _digi_set_start_to_cur(self):
        # 播放中先暫停，擷取目前顯示的那一幀（避免抓到正在跑的幀）
        self._digi_stop_play()
        self._digi_start_var.set(str(self._digi_cur))   # trace 會套用

    # ---- 導覽 / 播放 ----

    def _digi_on_scale(self, _val):
        idx = int(float(self._digi_scale.get()))
        if idx != self._digi_cur:
            self._digi_cur = idx
            self._digi_request_redraw()

    def _digi_step(self, delta: int):
        if not self._digi_frames:
            return
        idx = max(0, min(len(self._digi_frames) - 1, self._digi_cur + delta))
        if idx != self._digi_cur:
            self._digi_cur = idx
            self._digi_scale.set(idx)
            self._digi_request_redraw()

    def _digi_show_all(self):
        if not self._digi_frames:
            return
        self._digi_stop_play()
        self._digi_cur = len(self._digi_frames) - 1
        self._digi_scale.set(self._digi_cur)
        self._digi_request_redraw()

    def _digi_toggle_play(self):
        if self._digi_playing:
            self._digi_stop_play()
        else:
            self._digi_start_play()

    def _digi_start_play(self):
        if not self._digi_frames or len(self._digi_frames) < 2:
            return
        # 從起始幀開始播放，逐幀累積軌跡
        start = self._digi_get_start()
        self._digi_cur = start
        self._digi_scale.set(start)
        self._digi_request_redraw()
        self._digi_playing = True
        self._digi_play_btn.config(text="⏸ 暫停")
        self._digi_play_tick()

    def _digi_stop_play(self):
        self._digi_playing = False
        if self._digi_play_id:
            self.after_cancel(self._digi_play_id)
            self._digi_play_id = None
        self._digi_play_btn.config(text="▶ 播放")

    def _digi_play_tick(self):
        self._digi_play_id = None
        if not self._digi_playing or not self._digi_frames:
            return
        try:
            fps = int(self._digi_speed_var.get().split()[0])
        except (ValueError, IndexError):
            fps = 30
        # tkinter 計時器約 60fps 是實務上限：超過就改成一次跳多幀，
        # interval 固定 16ms，step 隨速度放大
        if fps <= 60:
            step, interval = 1, max(16, int(1000 / max(1, fps)))
        else:
            step, interval = max(1, round(fps / 60)), 16

        last = len(self._digi_frames) - 1
        start = self._digi_get_start()
        nxt = self._digi_cur + step
        if nxt > last:
            if self._digi_loop_var.get():
                nxt = start   # 循環回到起始幀
            else:
                self._digi_cur = last
                self._digi_scale.set(last)
                self._digi_request_redraw()
                self._digi_stop_play()
                return
        self._digi_cur = nxt
        self._digi_scale.set(nxt)
        self._digi_request_redraw()
        self._digi_play_id = self.after(interval, self._digi_play_tick)

    # ---- 繪圖 ----

    def _digi_request_redraw(self):
        if not self._digi_redraw_pending:
            self._digi_redraw_pending = True
            self.after_idle(self._digi_redraw)

    def _digi_data_to_canvas(self, x, y, geo):
        # geo = (s, off_x, off_y, x0, y0)；等比例縮放，避免 x/y 各自拉伸變形
        s, off_x, off_y, x0, y0 = geo
        return off_x + (x - x0) * s, off_y + (y - y0) * s   # y 不反轉：data 原點在左上

    def _digi_redraw(self):
        self._digi_redraw_pending = False
        if not self._digi_frames:
            try:
                self._digi_canvas_touch.delete("all")
                self._digi_canvas_pen.delete("all")
            except Exception:
                pass
            return
        cur = self._digi_cur
        self._digi_frame_lbl.set(f"Frame {cur} / {len(self._digi_frames) - 1}")
        self._digi_draw_group(self._digi_canvas_touch, self._digi_bounds_touch, is_pen=False)
        self._digi_draw_group(self._digi_canvas_pen, self._digi_bounds_pen, is_pen=True)

    def _digi_draw_group(self, c, bounds, is_pen: bool):
        """把符合 is_pen 分類的 contact 畫到指定畫布（手/筆各一張，座標軸獨立）。"""
        c.delete("all")
        cur = self._digi_cur
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return
        pad = 18
        x0, x1, y0, y1 = self._digi_axis(bounds)
        if x1 <= x0 or y1 <= y0:
            return
        # 等比例縮放（x、y 共用同一比例），讓軌跡形狀不變形、座標大小匹配
        avail_w = w - 2 * pad
        avail_h = h - 2 * pad
        s = min(avail_w / (x1 - x0), avail_h / (y1 - y0))
        off_x = pad + (avail_w - (x1 - x0) * s) / 2
        off_y = pad + (avail_h - (y1 - y0) * s) / 2
        geo = (s, off_x, off_y, x0, y0)

        # 外框 + 角落座標：對齊實際映射範圍（letterbox 後的子矩形）
        bx0, by0 = off_x, off_y
        bx1, by1 = off_x + (x1 - x0) * s, off_y + (y1 - y0) * s
        c.create_rectangle(bx0, by0, bx1, by1,
                           outline="#cccccc", width=1, dash=(3, 3))
        c.create_text(bx0 + 2, by0 + 2, text=f"({x0:g},{y0:g})", anchor="nw",
                      font=("Consolas", 7), fill="#aaaaaa")
        c.create_text(bx1 - 2, by1 - 2, text=f"({x1:g},{y1:g})", anchor="se",
                      font=("Consolas", 7), fill="#aaaaaa")

        # 收集每個 contact 在 起始幀..cur 範圍的軌跡點（之前的不顯示）
        # tuple = (frame_idx, x, y, down, conf)
        lo = self._digi_get_start()
        per_cid: Dict[int, List[Tuple[int, float, float, bool, bool]]] = {}
        for i in range(lo, cur + 1):
            for cid, ct in self._digi_frames[i]["contacts"].items():
                if self._digi_is_pen_cid(cid) != is_pen:
                    continue
                per_cid.setdefault(cid, []).append(
                    (i, ct["x"], ct["y"], bool(ct["down"]), bool(ct["conf"])))

        if not per_cid:
            c.create_text(w // 2, h // 2, text="（無資料）",
                          fill="#bbbbbb", font=("Microsoft JhengHei", 10))
            return

        total_pts = sum(len(v) for v in per_cid.values())
        draw_dots = total_pts <= 1500   # 點太多時略過每點小圓，只留線與重點
        try:
            hsize = max(6, min(60, int(self._digi_marker_var.get())))
        except ValueError:
            hsize = 16
        highlight = self._digi_highlight.get()

        for cid in sorted(per_cid.keys()):
            pts = per_cid[cid]
            color = self._SLOT_COLORS[cid % len(self._SLOT_COLORS)]

            # 軌跡線：只連續接「down=True 且幀連續」的點；抬起(down=False)或
            # 缺幀就斷筆，避免抬起後又按下被連成一條直線
            segment: List[float] = []
            last_idx: Optional[int] = None

            def flush_segment(seg=segment):
                if len(seg) >= 4:   # 至少兩點（每點 2 座標）
                    c.create_line(list(seg), fill=color, width=1.5,
                                  capstyle=tk.ROUND, joinstyle=tk.ROUND)

            for idx, x, y, d, _cf in pts:
                if not d:
                    flush_segment()
                    segment.clear()
                    last_idx = idx
                    continue
                # 容忍小幀間隔：筆與觸控在 log 內交錯時，同一裝置的點不會幀幀相連，
                # 間隔 <=6 視為同一筆軌跡；真正抬起由 down=False 斷筆。
                if last_idx is not None and idx - last_idx > 6 and segment:
                    flush_segment()
                    segment.clear()
                cx, cy = self._digi_data_to_canvas(x, y, geo)
                segment += [cx, cy]
                last_idx = idx
            flush_segment()

            # 每點小圓
            if draw_dots:
                for _, x, y, _, _ in pts:
                    cx, cy = self._digi_data_to_canvas(x, y, geo)
                    c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                                  fill=color, outline=color)
            # palm point 標示：down=True 但 confidence 非 True
            if highlight:
                r = hsize / 2
                for _, x, y, d, cf in pts:
                    if d and not cf:
                        cx, cy = self._digi_data_to_canvas(x, y, geo)
                        c.create_oval(cx - r, cy - r, cx + r, cy + r,
                                      outline="black", width=1.5, fill=color)

        # 目前位置強調 + ID 標籤：每個 contact 取「最近一次出現」的點，
        # 避免手/筆交錯時當前幀沒有該裝置封包而閃爍；太久沒出現（已抬起）則不標示
        for cid in sorted(per_cid.keys()):
            idx, x, y, _d, _cf = per_cid[cid][-1]
            if cur - idx > 6:
                continue
            color = self._SLOT_COLORS[cid % len(self._SLOT_COLORS)]
            cx, cy = self._digi_data_to_canvas(x, y, geo)
            c.create_oval(cx - 6, cy - 6, cx + 6, cy + 6,
                          fill=color, outline="white", width=2)
            c.create_text(cx, cy - 12, text=str(cid), fill=color,
                          font=("Arial", 8, "bold"))

    # ---- 表格（按需顯示）----

    def _digi_toggle_table(self):
        if self._digi_table_visible:
            self._digi_table_frame.pack_forget()
            self._digi_table_visible = False
            self._digi_table_btn.config(text="顯示表格 ▼")
        else:
            # 插在 canvas 之前、底部列之後
            self._digi_table_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))
            self._digi_table_visible = True
            self._digi_table_btn.config(text="隱藏表格 ▲")
            self._digi_refresh_table()

    def _digi_refresh_table(self):
        if not self._digi_table_visible:
            return
        mode = self._digi_preview_mode.get()
        cols = self._digi_wide_cols if mode == "wide" else self._digi_long_cols
        rows = self._digi_wide_rows if mode == "wide" else self._digi_long_rows
        tree = self._digi_tree
        for iid in tree.get_children():
            tree.delete(iid)
        tree["columns"] = cols
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=self._sx(70), anchor="center", stretch=False)
        if not rows:
            return

        self._digi_render_token += 1
        token = self._digi_render_token
        total = len(rows)
        CHUNK = 500

        def insert_chunk(start=0):
            if token != self._digi_render_token:
                return
            end = min(start + CHUNK, total)
            for r in range(start, end):
                row = rows[r]
                tree.insert("", "end", values=[
                    digiinfo_parse._fmt_cell(c, row.get(c)) for c in cols])
            if end < total:
                self.after(1, lambda: insert_chunk(end))
            else:
                self._digi_status_var.set(f"表格 {total} 列 × {len(cols)} 欄")

        insert_chunk(0)

    def _digi_on_tree_select(self, _event=None):
        sel = self._digi_tree.selection()
        if not sel:
            return
        cols = list(self._digi_tree["columns"])
        if "row_id" not in cols:
            return
        idx = cols.index("row_id")
        vals = self._digi_tree.item(sel[0], "values")
        if idx >= len(vals):
            return
        rid = digiinfo_parse._to_int(vals[idx])
        if rid is None:
            return
        # 跳到該 row_id 對應的 frame
        for i, fr in enumerate(self._digi_frames):
            if fr["row_id"] == rid:
                self._digi_stop_play()
                self._digi_cur = i
                self._digi_scale.set(i)
                self._digi_request_redraw()
                break

    def _digi_export_csv(self):
        mode = self._digi_preview_mode.get()
        cols = self._digi_wide_cols if mode == "wide" else self._digi_long_cols
        rows = self._digi_wide_rows if mode == "wide" else self._digi_long_rows
        if not rows:
            messagebox.showinfo("匯出 CSV", "沒有可匯出的資料，請先載入 XML。")
            return
        default_name = f"digiinfo_{mode}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(
            title="匯出 CSV", defaultextension=".csv", initialfile=default_name,
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            digiinfo_parse.write_csv(path, rows, cols)
        except Exception as exc:
            messagebox.showerror("匯出失敗", f"無法匯出 CSV:\n{exc}")
            return
        self._digi_status_var.set(f"已匯出 {len(rows)} 列到 {os.path.basename(path)}")
        messagebox.showinfo("匯出 CSV", f"已匯出 {len(rows)} 列（{mode}）。")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def destroy(self):
        self._hide_dev_tooltip()
        self._stop_listen()
        if getattr(self, "_devchange_thread", None):
            self._devchange_thread.stop()
            self._devchange_thread = None
        if getattr(self, "_hm_cancel", None):
            self._hm_cancel.set()
        if getattr(self, "_replay_after_id", None):
            try:
                self.after_cancel(self._replay_after_id)
            except Exception:
                pass
        super().destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()   # 熱圖匯出用 ProcessPool；打包後必要，否則子行程會重開視窗
    HIDToolApp().mainloop()
