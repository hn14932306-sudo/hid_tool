"""
HID Tool — Monitor + Send GUI
Imports backend modules: hid_descriptor, hid_rawinput, hid_device
"""

import collections
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import Dict, List, Optional, Tuple

from hid_descriptor import (
    HIDField,
    REPORT_TYPE_INPUT,
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
    match_device_name_to_hidapi,
    send_output_report,
    send_feature_report,
    get_feature_report,
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
        self._hidapi_devices: List[dict]                = []
        self._selected_dev:   Optional[dict]            = None
        self._descriptors:    Dict[str, List[HIDField]] = {}

        # Monitor state
        self._raw_thread:       Optional[RawInputThread] = None
        self._packet_queue:     queue.Queue              = queue.Queue()
        self._listening:        bool                     = False
        self._col_defs:         List[dict]               = []
        self._table_rid:        int                      = -1
        self._frame_deque:      collections.deque        = collections.deque()
        self._last_pkt_rx_time: float                    = 0.0
        self._last_scan_time:   int                      = -1
        self._scan_time_field:  Optional[HIDField]       = None

        self._build_ui()
        self._refresh_devices()
        self.after(20, self._poll_queue)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ---- Top bar (shared) ----
        top = tk.Frame(self, bd=2, relief=tk.RAISED, padx=4, pady=4)
        top.pack(side=tk.TOP, fill=tk.X)

        tk.Label(top, text="裝置:").pack(side=tk.LEFT)
        self._dev_var = tk.StringVar()
        self._dev_combo = ttk.Combobox(top, textvariable=self._dev_var, width=72, state="readonly")
        self._dev_combo.pack(side=tk.LEFT, padx=(2, 8))
        self._dev_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        tk.Button(top, text="重新整理", command=self._refresh_devices).pack(side=tk.LEFT, padx=2)

        self._listen_btn = tk.Button(
            top, text="開始監聽", command=self._toggle_listen,
            bg="#4CAF50", fg="white", font=("Arial", 10, "bold"),
        )
        self._listen_btn.pack(side=tk.LEFT, padx=8)

        # ---- Main PanedWindow ----
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # -- Left panel: descriptor tree --
        left_frame = tk.LabelFrame(paned, text="Report Descriptor 欄位", padx=2, pady=2)
        paned.add(left_frame, minsize=300)

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

        # ---- Status bar ----
        sb_frame = tk.Frame(self, bd=1, relief=tk.SUNKEN)
        sb_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self._status_var = tk.StringVar(value="就緒")
        tk.Label(sb_frame, textvariable=self._status_var,
                 anchor=tk.W, font=("Arial", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._rate_var = tk.StringVar(value="")
        tk.Label(sb_frame, textvariable=self._rate_var, anchor=tk.E,
                 font=("Consolas", 9, "bold"), width=14).pack(side=tk.RIGHT)

    def _build_monitor_tab(self, parent):
        ctrl_row = tk.Frame(parent)
        ctrl_row.pack(side=tk.TOP, fill=tk.X, padx=2, pady=2)

        self._only_vendor = tk.BooleanVar(value=False)
        self._show_raw    = tk.BooleanVar(value=False)

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

        tk.Checkbutton(ctrl_row, text="只顯示含 Vendor 資料",
                       variable=self._only_vendor).pack(side=tk.LEFT)
        tk.Checkbutton(ctrl_row, text="顯示 RAW 欄位",
                       variable=self._show_raw,
                       command=self._rebuild_table_columns).pack(side=tk.LEFT, padx=8)
        tk.Button(ctrl_row, text="清除", command=self._clear_log).pack(side=tk.RIGHT, padx=4)

        tbl_frame = tk.Frame(parent)
        tbl_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self._table = ttk.Treeview(tbl_frame, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(tbl_frame, orient=tk.VERTICAL,   command=self._table.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient=tk.HORIZONTAL, command=self._table.xview)
        self._table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._table.pack(fill=tk.BOTH, expand=True)


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

    def _refresh_devices(self):
        self._hidapi_devices = enumerate_hid_devices()
        labels = [format_device_label(d) for d in self._hidapi_devices]
        self._dev_combo["values"] = labels
        if labels:
            self._dev_combo.current(0)
            self._on_device_selected(None)
        self._status_var.set(f"找到 {len(self._hidapi_devices)} 個 HID 裝置")

    def _on_device_selected(self, event):
        idx = self._dev_combo.current()
        if idx < 0 or idx >= len(self._hidapi_devices):
            return
        self._selected_dev = self._hidapi_devices[idx]
        self._load_descriptor(self._selected_dev)

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
                try:
                    fields = parse_report_descriptor(raw)
                except Exception as e:
                    fields = []
                    self.after(0, lambda: self._status_var.set(f"Descriptor 解析錯誤: {e}"))
                self._descriptors[path_str] = fields
                self.after(0, lambda: self._populate_desc_tree(path_str))
            else:
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
        self._rebuild_table_columns()

    # ------------------------------------------------------------------
    # Monitor: Table columns
    # ------------------------------------------------------------------

    @staticmethod
    def _is_byte_expanded_field(hf: HIDField) -> bool:
        """UP=0x09 且 bit_size >= 8 的欄位按 byte 展開顯示（排除 1-bit button）。"""
        return hf.usage_page == 0x09 and hf.bit_size >= 8

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
            and (not f.is_const or (f.usage_page == 0x09 and f.bit_size >= 8))
        ]

        col_defs: List[dict] = [
            {"col_id": "__rid__", "label": "RID", "width": 50,
             "field_ref": None, "value_index": -1, "byte_index": -1},
        ]
        if self._show_raw.get():
            col_defs.append({"col_id": "__raw__", "label": "RAW", "width": 220,
                             "field_ref": None, "value_index": -1, "byte_index": -1})

        usage_total: Dict[Tuple[int, int], int] = {}
        for hf in input_fields:
            if not hf.is_vendor and not self._is_byte_expanded_field(hf):
                for i in range(hf.report_count):
                    u = hf.usages[i] if i < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                    k = (hf.usage_page, u)
                    usage_total[k] = usage_total.get(k, 0) + 1

        usage_seen:      Dict[Tuple[int, int], int] = {}
        vendor_byte_idx: int = 0
        byte_field_idx:  int = 0

        for hf in input_fields:
            if hf.is_vendor:
                for b in range(max(1, (hf.bit_size + 7) // 8)):
                    col_defs.append({
                        "col_id":      f"vnd_{id(hf)}_{b}",
                        "label":       f"V{vendor_byte_idx}",
                        "width":       40,
                        "field_ref":   hf,
                        "value_index": -1,
                        "byte_index":  b,
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
                    })
                    byte_field_idx += 1
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
                    })

        self._col_defs  = col_defs
        self._table_rid = target_rid if target_rid is not None else -1


        self._last_pkt_rx_time = 0.0
        self._last_scan_time   = -1
        self._scan_time_field  = next(
            (hf for hf in input_fields if not hf.is_vendor
             and any((hf.usage_page, u) == (0x0D, 0x56) for u in hf.usages)), None
        )

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
                if self._is_new_frame(pkt, rx_time, gap_threshold):
                    self._frame_deque.append(rx_time)
                self._last_pkt_rx_time = rx_time
                self._handle_packet(pkt)

            now    = time.monotonic()
            cutoff = now - 1.0
            while self._frame_deque and self._frame_deque[0] < cutoff:
                self._frame_deque.popleft()
            self._rate_var.set(f"{len(self._frame_deque):4d} scan/s")

        self.after(20, self._poll_queue)

    _MAX_ROWS = 300

    def _handle_packet(self, pkt: dict):
        data: bytes = pkt.get("data", b"")
        if not data:
            return

        report_id = data[0]
        payload   = data[1:] if len(data) > 1 else b""

        if self._table_rid != -1 and report_id != self._table_rid:
            return

        descriptor_fields: Optional[List[HIDField]] = None
        matched_dev = match_device_name_to_hidapi(pkt.get("device_name", ""), self._hidapi_devices)
        if matched_dev:
            descriptor_fields = self._descriptors.get(self._get_dev_path_str(matched_dev))

        if self._only_vendor.get():
            if not descriptor_fields:
                return
            if not any(
                f.is_vendor for f in descriptor_fields
                if f.report_type == REPORT_TYPE_INPUT
                and f.report_id == report_id and not f.is_const
            ):
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
                row.append(f"{val:02X}")
            else:
                hf   = col["field_ref"]
                idx  = col["value_index"]
                vals = get_vals(hf)
                row.append(vals[idx] if idx < len(vals) else "")

        self._table.insert("", 0, values=row)

        children = self._table.get_children()
        if len(children) > self._MAX_ROWS:
            for iid in children[self._MAX_ROWS:]:
                self._table.delete(iid)

    def _clear_log(self):
        for iid in self._table.get_children():
            self._table.delete(iid)

    # ------------------------------------------------------------------
    # Send tab
    # ------------------------------------------------------------------

    def _on_report_type_changed(self, *_):
        if self._report_type.get() == "Feature":
            self._get_btn.pack(side=tk.LEFT, padx=4)
        else:
            self._get_btn.pack_forget()

    def _on_send(self):
        if not self._selected_dev:
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

        use_feature = self._report_type.get() == "Feature"
        threading.Thread(
            target=self._send_report,
            args=(self._selected_dev["path"], report_id, data, use_feature),
            daemon=True,
        ).start()

    def _send_report(self, path, report_id: int, data: list, use_feature: bool):
        rtype = "Feature" if use_feature else "Output"
        self._send_log_append(f"\n發送 {rtype} Report")
        self._send_log_append(f"  Report ID : 0x{report_id:02X}")
        self._send_log_append(f"  Data      : {' '.join(f'{b:02X}' for b in data)}")
        try:
            sent = send_feature_report(path, report_id, data) if use_feature \
                   else send_output_report(path, report_id, data)
            if sent < 0:
                self._send_log_append(f"  [錯誤] 發送失敗 (回傳 {sent})")
            else:
                self._send_log_append(f"  [成功] 已發送 {sent} bytes")
        except Exception as e:
            self._send_log_append(f"  [錯誤] {e}")

    def _on_get_report(self):
        if not self._selected_dev:
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

        length = self._feature_report_length(report_id)
        threading.Thread(
            target=self._do_get_report,
            args=(self._selected_dev["path"], report_id, length),
            daemon=True,
        ).start()

    def _feature_report_length(self, report_id: int) -> int:
        path_str   = self._get_dev_path_str(self._selected_dev) if self._selected_dev else ""
        fields     = self._descriptors.get(path_str, [])
        total_bits = sum(
            f.bit_size for f in fields
            if f.report_type == REPORT_TYPE_FEATURE and f.report_id == report_id
        )
        return (total_bits + 7) // 8 if total_bits > 0 else 64

    def _do_get_report(self, path, report_id: int, length: int):
        self._send_log_append(f"\nGet Feature Report  ID=0x{report_id:02X}  Length={length}")
        try:
            data = get_feature_report(path, report_id, length)
            if not data:
                self._send_log_append("  [錯誤] 回傳空資料")
            else:
                self._send_log_append(f"  [成功] {len(data)} bytes:")
                for off in range(0, len(data), 16):
                    chunk = data[off:off + 16]
                    self._send_log_append(
                        f"    {off:04X}:  {' '.join(f'{b:02X}' for b in chunk)}"
                    )
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
    # Cleanup
    # ------------------------------------------------------------------

    def destroy(self):
        self._stop_listen()
        super().destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    HIDToolApp().mainloop()
