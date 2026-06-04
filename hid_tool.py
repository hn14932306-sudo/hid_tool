"""
HID Tool — Monitor + Send GUI
Imports backend modules: hid_descriptor, hid_rawinput, hid_device
"""

import collections
import csv
import datetime
import os
import queue
import threading
import time
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, ttk, scrolledtext
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from hid_descriptor import (
    HIDField,
    REPORT_TYPE_INPUT,
    REPORT_TYPE_OUTPUT,
    REPORT_TYPE_FEATURE,
    parse_report_descriptor,
    extract_field_value,
    get_usage_name,
)
from hid_rawinput import RawInputThread
from hid_device import (
    enumerate_hid_devices,
    format_device_label,
    read_descriptor_via_hidapi,
    send_output_report,
    send_feature_report,
    get_feature_report,
    get_input_report,
    set_output_report_cmd,
    parse_hex_bytes,
)


# ---------------------------------------------------------------------------
# Unified GUI Application
# ---------------------------------------------------------------------------

class HIDToolApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HID Tool — Monitor + Send")
        self.geometry("1300x780")
        self.minsize(1000, 620)

        # Shared state
        self._hidapi_devices:    List[dict]                = []
        self._selected_dev:      Optional[dict]            = None
        self._descriptors:       Dict[str, List[HIDField]] = {}
        self._raw_descriptors:   Dict[str, bytes]          = {}  # path_str -> raw descriptor bytes

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
        self._canvas_trails:           Dict[int, collections.deque] = {}
        self._canvas_prev_active:      set                          = set()
        self._canvas_item_ids:         Dict[int, Tuple[int, int]]   = {}
        self._canvas_trail_line_ids:   Dict[int, int]               = {}
        self._table_pending:           List[tuple]                   = []   # (row, tags, errs)
        self._table_flush_pending:     bool                         = False
        self._canvas_flush_pending:    bool                         = False
        self._canvas_dirty_keys:       set                          = set()  # 有新資料的 cid
        self._canvas_trail_reset_keys: set                          = set()  # 需清除舊軌跡線
        self._canvas_circle_del_keys:  set                          = set()  # 需刪除圓圈

        # Error-detection state
        self._scan_time_delta:  int                      = 0   # delta of last scan time change
        self._error_count:      int                      = 0

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

        self._build_ui()
        self._refresh_devices()
        self.after(20, self._poll_queue)
        self.after(50, self._toggle_desc_panel)   # start collapsed

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ---- Top bar (shared) ----
        top = tk.Frame(self, bd=2, relief=tk.RAISED, padx=4, pady=4)
        top.pack(side=tk.TOP, fill=tk.X)

        # Row 1: 監聽裝置
        row1 = tk.Frame(top)
        row1.pack(fill=tk.X)

        tk.Label(row1, text="監聽裝置:", width=8, anchor="e").pack(side=tk.LEFT)
        self._dev_var = tk.StringVar()
        self._dev_combo = ttk.Combobox(row1, textvariable=self._dev_var, width=120, state="readonly")
        self._dev_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 8))
        self._dev_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        self._dev_combo.bind("<Enter>", self._show_dev_tooltip)
        self._dev_combo.bind("<Motion>", self._move_dev_tooltip)
        self._dev_combo.bind("<Leave>", self._hide_dev_tooltip)
        self._dev_combo.bind("<ButtonPress-1>", self._hide_dev_tooltip)

        tk.Button(row1, text="重新整理", command=self._refresh_devices).pack(side=tk.LEFT, padx=2)

        self._listen_btn = tk.Button(
            row1, text="開始監聽", command=self._toggle_listen,
            bg="#4CAF50", fg="white", font=("Arial", 10, "bold"),
        )
        self._listen_btn.pack(side=tk.LEFT, padx=8)

        # Row 2: 指令裝置
        row2 = tk.Frame(top)
        row2.pack(fill=tk.X, pady=(2, 0))

        tk.Label(row2, text="指令裝置:", width=8, anchor="e").pack(side=tk.LEFT)
        self._cmd_dev_var = tk.StringVar()
        self._cmd_dev_combo = ttk.Combobox(row2, textvariable=self._cmd_dev_var, width=120, state="readonly")
        self._cmd_dev_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 8))
        self._cmd_dev_combo.bind("<<ComboboxSelected>>", self._on_cmd_device_selected)

        # ---- Main PanedWindow ----
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._paned = paned
        self._desc_panel_width = 300   # remembered width when expanded

        # -- Left panel: collapsible descriptor panel --
        left_outer = tk.Frame(paned)
        paned.add(left_outer, minsize=0)

        # Toggle button row
        toggle_row = tk.Frame(left_outer)
        toggle_row.pack(side=tk.TOP, fill=tk.X)
        self._desc_collapsed = False
        self._desc_toggle_btn = tk.Button(
            toggle_row, text="◀ Report Descriptor 欄位",
            anchor=tk.W, relief=tk.FLAT, bg="#dde3ea",
            font=("Arial", 9, "bold"),
            command=self._toggle_desc_panel,
        )
        self._desc_toggle_btn.pack(fill=tk.X)

        # Inner content (tree + raw button) — hidden when collapsed
        self._desc_inner = tk.Frame(left_outer)
        self._desc_inner.pack(fill=tk.BOTH, expand=True)

        left_frame = tk.Frame(self._desc_inner, padx=2, pady=2)
        left_frame.pack(fill=tk.BOTH, expand=True)

        self._desc_tree = ttk.Treeview(
            left_frame,
            columns=("bit_size", "logical_range"),
            show="tree headings",
        )
        self._desc_tree.heading("#0",            text="欄位 / 名稱")
        self._desc_tree.heading("bit_size",      text="位元大小")
        self._desc_tree.heading("logical_range", text="Logical 範圍")
        self._desc_tree.column("#0",            width=200, stretch=True)
        self._desc_tree.column("bit_size",      width=70,  stretch=False)
        self._desc_tree.column("logical_range", width=100, stretch=False)

        tk.Button(left_frame, text="顯示原始 Descriptor Bytes",
                  command=self._show_raw_descriptor).pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

        desc_sb = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self._desc_tree.yview)
        self._desc_tree.configure(yscrollcommand=desc_sb.set)
        desc_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._desc_tree.pack(fill=tk.BOTH, expand=True)

        self._desc_tree.tag_configure("vendor", foreground="darkorange", font=("Consolas", 9, "bold"))
        self._desc_tree.tag_configure("touch",  foreground="darkgreen")
        self._desc_tree.tag_configure("const",  foreground="gray")

        # -- Right panel: Notebook --
        right_frame = tk.Frame(paned)
        paned.add(right_frame, minsize=560)

        self._notebook = ttk.Notebook(right_frame)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        monitor_tab = tk.Frame(self._notebook)
        self._notebook.add(monitor_tab, text="  監聽  ")
        self._build_monitor_tab(monitor_tab)

        send_tab = tk.Frame(self._notebook)
        self._notebook.add(send_tab, text="  發送  ")
        self._build_send_tab(send_tab)

        canvas_tab = tk.Frame(self._notebook)
        self._notebook.add(canvas_tab, text="  畫布  ")
        self._build_canvas_tab(canvas_tab)

        stress_tab = tk.Frame(self._notebook)
        self._notebook.add(stress_tab, text="  壓測  ")
        self._build_stress_tab(stress_tab)

        # ---- Status bar ----
        sb_frame = tk.Frame(self, bd=1, relief=tk.SUNKEN)
        sb_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self._status_var = tk.StringVar(value="就緒")
        tk.Label(sb_frame, textvariable=self._status_var,
                 anchor=tk.W, font=("Arial", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._rate_var = tk.StringVar(value="")
        tk.Label(sb_frame, textvariable=self._rate_var, anchor=tk.E,
                 font=("Consolas", 9, "bold"), width=14).pack(side=tk.RIGHT)

        self._error_var = tk.StringVar(value="")
        tk.Label(sb_frame, textvariable=self._error_var, anchor=tk.E,
                 font=("Consolas", 11, "bold"), fg="red", width=34).pack(side=tk.RIGHT, padx=(8, 4))

    def _build_monitor_tab(self, parent):
        ctrl_row = tk.Frame(parent)
        ctrl_row.pack(side=tk.TOP, fill=tk.X, padx=2, pady=2)

        self._show_raw    = tk.BooleanVar(value=False)
        self._view_mode   = tk.StringVar(value="Hybrid")

        tk.Label(ctrl_row, text="Report ID:").pack(side=tk.LEFT)
        self._rid_filter_var = tk.StringVar(value="全部")
        self._rid_combo = ttk.Combobox(ctrl_row, textvariable=self._rid_filter_var,
                                       width=8, state="readonly")
        self._rid_combo["values"] = ["全部"]
        self._rid_combo.current(0)
        self._rid_combo.pack(side=tk.LEFT, padx=(2, 12))
        self._rid_combo.bind("<<ComboboxSelected>>", lambda _: self._rebuild_table_columns())

        tk.Label(ctrl_row, text="Frame gap(ms):").pack(side=tk.LEFT, padx=(8, 0))
        self._gap_ms_var = tk.StringVar(value="4")
        tk.Spinbox(ctrl_row, from_=1, to=50, textvariable=self._gap_ms_var,
                   width=4).pack(side=tk.LEFT, padx=(2, 8))

        tk.Label(ctrl_row, text="View:").pack(side=tk.LEFT, padx=(8, 0))
        self._view_combo = ttk.Combobox(ctrl_row, textvariable=self._view_mode,
                                        width=10, state="readonly",
                                        values=("Hybrid", "Parallel"))
        self._view_combo.pack(side=tk.LEFT, padx=(2, 8))
        self._view_combo.bind("<<ComboboxSelected>>", lambda _: self._rebuild_table_columns())

        tk.Label(ctrl_row, text="最大 scan Δ:").pack(side=tk.LEFT, padx=(8, 0))
        self._max_scan_delta_var = tk.StringVar(value="200")
        tk.Spinbox(ctrl_row, from_=0, to=9999, textvariable=self._max_scan_delta_var,
                   width=5).pack(side=tk.LEFT, padx=(2, 8))

        tk.Label(ctrl_row, text="保留筆數:").pack(side=tk.LEFT, padx=(8, 0))
        self._max_rows_var = tk.StringVar(value="200")
        tk.Spinbox(ctrl_row, from_=50, to=5000, increment=50,
                   textvariable=self._max_rows_var,
                   width=5).pack(side=tk.LEFT, padx=(2, 8))

        tk.Checkbutton(ctrl_row, text="顯示 RAW 欄位",
                       variable=self._show_raw,
                       command=self._rebuild_table_columns).pack(side=tk.LEFT, padx=8)
        tk.Button(ctrl_row, text="清除", command=self._clear_log).pack(side=tk.RIGHT, padx=4)
        tk.Button(ctrl_row, text="Export Excel", command=self._export_monitor_to_excel).pack(side=tk.RIGHT, padx=4)

        # FF01 usage filter row
        self._ff01_filter_frame = ttk.LabelFrame(parent, text="Usage Page FF01 欄位顯示")
        self._ff01_filter_frame.pack(side=tk.TOP, fill=tk.X, padx=2, pady=(0, 2))

        # 格式選擇器永久固定在右側
        fmt_right = tk.Frame(self._ff01_filter_frame)
        fmt_right.pack(side=tk.RIGHT, padx=4, pady=1)
        tk.Label(fmt_right, text="格式:").pack(side=tk.LEFT)
        fmt_combo = ttk.Combobox(
            fmt_right, textvariable=self._ff01_fmt,
            values=("Hex", "Dec", "Bin"), state="readonly", width=5,
        )
        fmt_combo.pack(side=tk.LEFT, padx=2)
        fmt_combo.bind("<<ComboboxSelected>>", lambda _: self._rebuild_table_columns())

        # 動態 checkbox 區域（每次換裝置時重建）
        self._ff01_check_frame = tk.Frame(self._ff01_filter_frame)
        self._ff01_check_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tbl_frame = tk.Frame(parent)
        tbl_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self._table = ttk.Treeview(tbl_frame, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(tbl_frame, orient=tk.VERTICAL,   command=self._table.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient=tk.HORIZONTAL, command=self._table.xview)
        self._table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._table.pack(fill=tk.BOTH, expand=True)
        self._table.tag_configure("scan_error", background="#ffd6d6")


    def _build_send_tab(self, parent):
        pad = {"padx": 8, "pady": 4}

        param_frame = ttk.LabelFrame(parent, text="發送參數", padding=8)
        param_frame.pack(fill=tk.X, **pad)

        ttk.Label(param_frame, text="Report 類型:").grid(row=0, column=0, sticky="w", padx=4)
        self._report_type = tk.StringVar(value="Output")
        self._report_type.trace_add("write", self._on_report_type_changed)
        ttk.Radiobutton(param_frame, text="Output",  variable=self._report_type,
                        value="Output").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(param_frame, text="Feature", variable=self._report_type,
                        value="Feature").grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(param_frame, text="Input",   variable=self._report_type,
                        value="Input").grid(row=0, column=3, sticky="w")

        ttk.Label(param_frame, text="Report ID (hex):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self._report_id_var = tk.StringVar(value="01")
        ttk.Entry(param_frame, textvariable=self._report_id_var, width=10).grid(
            row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(param_frame, text="Data (hex):").grid(row=2, column=0, sticky="w", padx=4)
        self._send_data_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self._send_data_var, width=55).grid(
            row=2, column=1, columnspan=3, sticky="w")

        btn_row = tk.Frame(parent)
        btn_row.pack(pady=6)
        ttk.Button(btn_row, text="發送 (Set Report)", command=self._on_send).pack(side=tk.LEFT, padx=4)
        self._get_btn = ttk.Button(btn_row, text="Get Report", command=self._on_get_report)
        # 初始隱藏，切到 Feature 才顯示

        log_frame = ttk.LabelFrame(parent, text="操作記錄", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)

        self._send_log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", font=("Consolas", 9))
        self._send_log.pack(fill=tk.BOTH, expand=True)

        ttk.Button(parent, text="清除記錄", command=self._clear_send_log).pack(pady=(0, 6))

    # ------------------------------------------------------------------
    # Shared Device Management
    # ------------------------------------------------------------------

    _CMD_SAME_LABEL = "（同監聽裝置）"

    def _refresh_devices(self):
        self._descriptors.clear()
        self._raw_descriptors.clear()
        self._hidapi_devices = sorted(
            enumerate_hid_devices(),
            key=lambda d: (d.get("vendor_id", 0), d.get("product_id", 0)),
        )
        labels = [format_device_label(d) for d in self._hidapi_devices]
        self._dev_combo["values"] = labels
        self._hide_dev_tooltip()
        if labels:
            self._dev_combo.current(0)
            self._on_device_selected(None)

        cmd_labels = [self._CMD_SAME_LABEL] + labels
        self._cmd_dev_combo["values"] = cmd_labels
        if self._cmd_dev_combo.current() < 0:
            self._cmd_dev_combo.current(0)
            self._cmd_dev = None

        self._status_var.set(f"找到 {len(self._hidapi_devices)} 個 HID 裝置")

    def _on_device_selected(self, event):
        idx = self._dev_combo.current()
        if idx < 0 or idx >= len(self._hidapi_devices):
            return
        self._selected_dev = self._hidapi_devices[idx]
        self._hide_dev_tooltip()
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
        if 0 <= idx < len(self._hidapi_devices):
            return format_device_label(self._hidapi_devices[idx])
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
            tk.Label(self._ff01_check_frame, text="（無 FF01 欄位）", fg="gray").pack(side=tk.LEFT, padx=4)
            return

        for usage in ff01_usages:
            var = tk.BooleanVar(value=True)
            self._ff01_usage_vars[usage] = var
            cb = tk.Checkbutton(
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
            self._desc_toggle_btn.config(text="◀ Report Descriptor 欄位")
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
        win.geometry("820x560")
        win.minsize(600, 400)

        # Toolbar
        tb = tk.Frame(win)
        tb.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)
        tk.Label(tb, text=f"共 {len(raw)} bytes", font=("Arial", 9)).pack(side=tk.LEFT)
        tk.Button(tb, text="複製 Hex", command=lambda: self._copy_to_clipboard(
            win, " ".join(f"{b:02X}" for b in raw)
        )).pack(side=tk.RIGHT, padx=4)
        tk.Button(tb, text="複製 C Array", command=lambda: self._copy_to_clipboard(
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
        hex_frame = tk.Frame(nb)
        nb.add(hex_frame, text="  Hex 檢視  ")

        hex_text = scrolledtext.ScrolledText(
            hex_frame, font=("Consolas", 10), wrap=tk.NONE,
            state=tk.NORMAL, relief=tk.FLAT,
        )
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
        parsed_frame = tk.Frame(nb)
        nb.add(parsed_frame, text="  解析檢視  ")

        parsed_text = scrolledtext.ScrolledText(
            parsed_frame, font=("Consolas", 10), wrap=tk.NONE,
            state=tk.NORMAL, relief=tk.FLAT,
        )
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

    # Usage Page 0x09 / Usage 0xC5 以外、UP=0xFF01 以外的特殊展開欄位
    _FF01_LIKE = {(0x09, 0xC5)}

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
            "ContactID": (0x0D, 0x51),
            "X": (0x01, 0x30),
            "Y": (0x01, 0x31),
            "Width": (0x0D, 0x48),
            "Height": (0x0D, 0x49),
        }
        entries: Dict[str, List[Tuple[HIDField, int]]] = {name: [] for name in usage_keys}
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
                "ContactID": pick("ContactID", idx),
                "X": pick("X", idx),
                "Y": pick("Y", idx),
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
            {"col_id": "ContactID", "label": "ContactID", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "X", "label": "X", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "Y", "label": "Y", "width": 80, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "Width", "label": "Width", "width": 70, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
            {"col_id": "Height", "label": "Height", "width": 70, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1},
        ]
        if "ContactCount" in self._hybrid_common:
            col_defs.append({"col_id": "ContactCount", "label": "Count", "width": 60, "kind": "common", "field_ref": None, "value_index": -1, "byte_index": -1})
        if "ScanTime" in self._hybrid_common:
            col_defs.append({"col_id": "ScanTime", "label": "ScanTime", "width": 85, "kind": "common", "field_ref": None, "value_index": -1, "byte_index": -1})

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

        # ConfTip column goes here — after touch/common fields, before FF01
        col_defs.append({"col_id": "ConfTip", "label": "Conf/Tip", "width": 130, "kind": "group", "field_ref": None, "value_index": -1, "byte_index": -1})

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

    def _merge_conf_tip(self, conf, tip, btn=None) -> str:
        parts = []
        try:
            if int(conf):
                parts.append("Confidence")
        except (TypeError, ValueError):
            pass
        try:
            if int(tip):
                parts.append("Tip")
        except (TypeError, ValueError):
            pass
        try:
            if btn is not None and int(btn):
                parts.append("phyButton")
        except (TypeError, ValueError):
            pass
        return " ".join(parts)

    def _fmt_ff01_byte(self, val: int) -> str:
        fmt = self._ff01_fmt.get()
        if fmt == "Dec":
            return str(val)
        if fmt == "Bin":
            return f"{val:08b}"
        return f"{val:02X}"

    def _rebuild_table_columns(self):
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

        if self._view_mode.get() == "Hybrid" and self._setup_hybrid_columns(input_fields):
            ids = [c["col_id"] for c in self._col_defs]
            self._table["columns"] = ids
            self._table["show"] = "headings"
            for c in self._col_defs:
                self._table.heading(c["col_id"], text=c["label"])
                self._table.column(c["col_id"], width=c["width"],
                                   stretch=(c["col_id"] == "__raw__"), anchor="center")
            return

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
            self._table.column(c["col_id"], width=c["width"],
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

        if self._selected_dev:
            path_str = self._get_dev_path_str(self._selected_dev)
            self._descriptors.pop(path_str, None)
            self._raw_descriptors.pop(path_str, None)
            self._load_descriptor(self._selected_dev)

        extra_up = self._selected_dev.get("usage_page", 0) if self._selected_dev else 0
        extra_u  = self._selected_dev.get("usage",      0) if self._selected_dev else 0

        self._raw_thread = RawInputThread(
            self._packet_queue, extra_usage_page=extra_up, extra_usage=extra_u,
        )
        self._raw_thread.start()
        self._raw_thread._ready_event.wait(timeout=3.0)

        self._listening = True
        self._listen_btn.config(text="停止監聽", bg="#f44336")
        self._status_var.set("監聽中...")

    def _stop_listen(self):
        if self._raw_thread:
            self._raw_thread.stop()
            self._raw_thread = None
        self._listening = False
        self._listen_btn.config(text="開始監聽", bg="#4CAF50")
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
                        if self._last_scan_time >= 0:
                            wrap_base = max(1, hf.logical_max - hf.logical_min + 1)
                            self._scan_time_delta = (st - self._last_scan_time) % wrap_base
                        else:
                            self._scan_time_delta = 0
                        self._last_scan_time = st
                        return True
                    return False
        return rx_time - self._last_pkt_rx_time >= gap_threshold

    def _poll_queue(self):
        pkts = []
        try:
            while len(pkts) < 64:
                pkts.append(self._packet_queue.get_nowait())
        except queue.Empty:
            pass

        if pkts:
            try:
                gap_threshold = float(self._gap_ms_var.get()) / 1000.0
            except ValueError:
                gap_threshold = 0.004

            for pkt in pkts:
                rx_time = pkt.get("rx_time", time.monotonic())
                is_new_frame = self._is_new_frame(pkt, rx_time, gap_threshold)
                if is_new_frame:
                    self._frame_deque.append(rx_time)
                self._last_pkt_rx_time = rx_time
                self._handle_packet(
                    pkt,
                    is_new_frame=is_new_frame,
                )

            now    = time.monotonic()
            cutoff = now - 1.0
            while self._frame_deque and self._frame_deque[0] < cutoff:
                self._frame_deque.popleft()
            self._rate_var.set(f"{len(self._frame_deque):4d} scan/s")

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
  <dc:creator>HID Tool</dc:creator>
  <cp:lastModifiedBy>HID Tool</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>
</cp:coreProperties>"""
        app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>HID Tool</Application>
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
            self._table.insert("", 0, values=row, tags=row_tags)
        try:
            max_rows = max(50, int(self._max_rows_var.get()))
        except (ValueError, AttributeError):
            max_rows = 200
        children = self._table.get_children()
        if len(children) > max_rows:
            for iid in children[max_rows:]:
                self._table.delete(iid)

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
            if not current_touch_active:
                self._last_scan_time = -1
                self._scan_time_delta = 0
            else:
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
            _btn1_val = next(
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

                row_map = {
                    "__frame__": self._frame_seq,
                    "__rid__": f"0x{report_id:02X}",
                    "__slot__": group["slot"],
                    "ConfTip": self._merge_conf_tip(confidence, tip, _btn1_val),
                    "ContactID": cid_val,
                    "X": x,
                    "Y": y,
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
                    "ConfTip": self._merge_conf_tip("", "", _btn1_val),
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

        row = []
        for col in self._col_defs:
            cid = col["col_id"]
            if cid == "__rid__":
                row.append(f"0x{report_id:02X}")
            elif cid == "__raw__":
                row.append(" ".join(f"{b:02X}" for b in data))
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
        self._error_count = 0
        self._error_var.set("")

    # ------------------------------------------------------------------
    # Send tab
    # ------------------------------------------------------------------

    def _on_report_type_changed(self, *_):
        rtype = self._report_type.get()
        if rtype in ("Feature", "Input"):
            self._get_btn.pack(side=tk.LEFT, padx=4)
        else:
            self._get_btn.pack_forget()

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

    def _log_bytes(self, data, append_fn):
        """顯示 bytes：第一行 64 bytes（對齊 I2C block 去掉 2-byte Length field），後續每行 66 bytes。"""
        if not data:
            return
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

    def _build_canvas_tab(self, parent):
        info_row = tk.Frame(parent)
        info_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)

        self._canvas_info_var = tk.StringVar(value="（尚未載入裝置）")
        tk.Label(info_row, textvariable=self._canvas_info_var,
                 font=("Consolas", 9), fg="#555").pack(side=tk.LEFT)

        tk.Button(info_row, text="清除", command=self._clear_canvas).pack(side=tk.RIGHT, padx=4)
        tk.Button(info_row, text="Export Excel",
                  command=self._export_monitor_to_excel).pack(side=tk.RIGHT, padx=4)

        self._touch_canvas = tk.Canvas(
            parent, bg="white", cursor="crosshair",
            highlightthickness=1, highlightbackground="#aaaaaa",
        )
        self._touch_canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self._touch_canvas.bind("<Configure>", lambda *_: self._redraw_canvas())

    def _build_stress_tab(self, parent):
        pad = {"padx": 8, "pady": 4}

        cmd_frame = ttk.LabelFrame(parent, text="壓測後發送指令", padding=8)
        cmd_frame.pack(fill=tk.X, **pad)

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
        self._stress_dir_lbl.grid(row=0, column=3, sticky="w", padx=(20, 4))
        self._stress_dir_set_rb = ttk.Radiobutton(cmd_frame, text="Set", variable=self._stress_feature_dir,
                                                   value="Set", command=self._stress_update_cmd_ui)
        self._stress_dir_set_rb.grid(row=0, column=4, sticky="w")
        self._stress_dir_get_rb = ttk.Radiobutton(cmd_frame, text="Get", variable=self._stress_feature_dir,
                                                   value="Get", command=self._stress_update_cmd_ui)
        self._stress_dir_get_rb.grid(row=0, column=5, sticky="w")

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

        settings_frame = ttk.LabelFrame(parent, text="壓測設定", padding=8)
        settings_frame.pack(fill=tk.X, **pad)

        ttk.Label(settings_frame, text="抬起後延遲 (ms):").grid(row=0, column=0, sticky="w", padx=4)
        self._stress_delay_var = tk.StringVar(value="200")
        ttk.Spinbox(settings_frame, from_=0, to=9999, textvariable=self._stress_delay_var, width=6).grid(row=0, column=1, sticky="w", padx=(2, 20))

        ttk.Label(settings_frame, text="最大次數 (0=無限):").grid(row=0, column=2, sticky="w", padx=4)
        self._stress_max_count_var = tk.StringVar(value="0")
        ttk.Spinbox(settings_frame, from_=0, to=99999, textvariable=self._stress_max_count_var, width=7).grid(row=0, column=3, sticky="w", padx=(2, 20))

        ttk.Label(settings_frame, text="最大時間 (s, 0=無限):").grid(row=0, column=4, sticky="w", padx=4)
        self._stress_max_time_var = tk.StringVar(value="0")
        ttk.Spinbox(settings_frame, from_=0, to=99999, textvariable=self._stress_max_time_var, width=7).grid(row=0, column=5, sticky="w")

        poll_frame = ttk.LabelFrame(parent, text="定時讀取指令", padding=8)
        poll_frame.pack(fill=tk.X, **pad)

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

        ctrl_row = tk.Frame(parent)
        ctrl_row.pack(fill=tk.X, padx=8, pady=4)
        self._stress_start_btn = tk.Button(
            ctrl_row, text="開始壓測", command=self._stress_toggle,
            bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), padx=12, pady=4,
        )
        self._stress_start_btn.pack(side=tk.LEFT, padx=4)
        tk.Button(ctrl_row, text="清除記錄", command=self._stress_log_clear).pack(side=tk.LEFT, padx=4)
        tk.Button(ctrl_row, text="Export CSV", command=self._stress_export_csv).pack(side=tk.LEFT, padx=4)

        stats_frame = ttk.LabelFrame(parent, text="統計", padding=6)
        stats_frame.pack(fill=tk.X, padx=8, pady=2)

        ttk.Label(stats_frame, text="總次數:").pack(side=tk.LEFT, padx=(4, 2))
        self._stress_count_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self._stress_count_var,
                  font=("Consolas", 12, "bold"), width=6, foreground="#2196F3").pack(side=tk.LEFT, padx=(0, 12))

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

        log_frame = ttk.LabelFrame(parent, text="壓測記錄", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))
        self._stress_log = scrolledtext.ScrolledText(
            log_frame, height=12, state="disabled", font=("Consolas", 9))
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

    def _canvas_update_slot(self, track_key: int, tip, lx, ly, cid_val, confidence=""):
        """純資料更新，不呼叫任何 tkinter — 畫面由 _canvas_flush 統一處理。"""
        if tip and lx is not None and ly is not None:
            if track_key not in self._canvas_prev_active:
                self._canvas_trails.pop(track_key, None)
                self._canvas_trail_reset_keys.add(track_key)
            trail = self._canvas_trails.setdefault(track_key, collections.deque(maxlen=500))
            if not trail or lx != trail[-1][0] or ly != trail[-1][1]:
                trail.append((lx, ly))
                self._canvas_dirty_keys.add(track_key)
            try:
                conf_val = int(confidence)
            except (TypeError, ValueError):
                conf_val = 1
            self._canvas_contacts[track_key] = {
                "x": lx, "y": ly, "cid": cid_val, "conf": conf_val,
            }
        else:
            self._canvas_contacts.pop(track_key, None)
            self._canvas_circle_del_keys.add(track_key)
            self._canvas_dirty_keys.discard(track_key)
        self._schedule_canvas_flush()

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
            color  = self._SLOT_COLORS[track_key % len(self._SLOT_COLORS)]
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
                if line_id is not None:
                    c.coords(line_id, flat)
                    c.itemconfig(line_id, width=line_width)
                else:
                    line_id = c.create_line(
                        flat, fill=color, width=line_width,
                        capstyle=tk.ROUND, joinstyle=tk.ROUND,
                        tags=(f"trail_{track_key}", "trail"),
                    )
                    self._canvas_trail_line_ids[track_key] = line_id

            ids = self._canvas_item_ids.get(track_key)
            if ids:
                oval_id, text_id = ids
                c.coords(oval_id, new_cx - r, new_cy - r, new_cx + r, new_cy + r)
                c.coords(text_id, new_cx, new_cy)
            else:
                contact_tag = f"contact_{track_key}"
                oval_id = c.create_oval(
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
            color = self._SLOT_COLORS[slot % len(self._SLOT_COLORS)]
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
        for slot, contact in self._canvas_contacts.items():
            try:
                lx = float(contact["x"])
                ly = float(contact["y"])
            except (TypeError, ValueError):
                continue
            cx = pad + (lx - x_min) * xs
            cy = pad + (ly - y_min) * ys
            color = self._SLOT_COLORS[slot % len(self._SLOT_COLORS)]
            contact_tag = f"contact_{slot}"
            oval_id = c.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=color, outline="white", width=2,
                                    tags=(contact_tag, "contact"))
            cid_label = str(contact.get("cid", slot))
            text_id = c.create_text(cx, cy, text=cid_label,
                                    fill="white", font=("Arial", 10, "bold"),
                                    tags=(contact_tag, "contact"))
            new_ids[slot] = (oval_id, text_id)
        self._canvas_item_ids = new_ids

    def _clear_canvas(self):
        self._canvas_contacts.clear()
        self._canvas_trails.clear()
        self._canvas_prev_active.clear()
        self._canvas_item_ids.clear()
        self._canvas_trail_line_ids.clear()
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
        self._stress_start_btn.config(text="停止壓測", bg="#e74c3c")
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
        self._stress_start_btn.config(text="開始壓測", bg="#4CAF50")
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
    # Cleanup
    # ------------------------------------------------------------------

    def destroy(self):
        self._hide_dev_tooltip()
        self._stop_listen()
        super().destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    HIDToolApp().mainloop()
