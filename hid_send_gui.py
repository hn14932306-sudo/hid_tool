"""
HID Send Tool - GUI 版本
Send Report ID + Data to HID devices
"""

import hid
import tkinter as tk
from tkinter import ttk, scrolledtext
import threading


def parse_hex_bytes(s: str) -> list[int]:
    """解析 hex 字串，例如 'AA BB CC' 或 'AABBCC'"""
    s = s.replace(" ", "").replace(",", "").replace("0x", "").replace("0X", "")
    if not s:
        return []
    if len(s) % 2 != 0:
        s = "0" + s
    return [int(s[i:i+2], 16) for i in range(0, len(s), 2)]


class HIDSendApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HID Send Tool")
        self.resizable(False, False)
        self.devices = []
        self._build_ui()
        self.refresh_devices()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ── 裝置清單 ──────────────────────────────────────────────────
        dev_frame = ttk.LabelFrame(self, text="HID 裝置", padding=6)
        dev_frame.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        self.device_list = tk.Listbox(dev_frame, height=8, width=80,
                                      font=("Consolas", 9))
        sb = ttk.Scrollbar(dev_frame, orient="vertical",
                            command=self.device_list.yview)
        self.device_list.configure(yscrollcommand=sb.set)
        self.device_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="e", padx=8, pady=2)
        ttk.Button(btn_frame, text="重新整理", command=self.refresh_devices).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="查看 Report Descriptor", command=self._show_descriptor).pack(side="left", padx=2)

        # ── 參數 ──────────────────────────────────────────────────────
        param_frame = ttk.LabelFrame(self, text="傳送參數", padding=6)
        param_frame.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        # Report 類型
        ttk.Label(param_frame, text="Report 類型:").grid(
            row=0, column=0, sticky="w", padx=4)
        self.report_type = tk.StringVar(value="Output")
        ttk.Radiobutton(param_frame, text="Output", variable=self.report_type,
                        value="Output").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(param_frame, text="Feature", variable=self.report_type,
                        value="Feature").grid(row=0, column=2, sticky="w")

        # Report ID
        ttk.Label(param_frame, text="Report ID (hex):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        self.report_id_var = tk.StringVar(value="01")
        ttk.Entry(param_frame, textvariable=self.report_id_var, width=10).grid(
            row=1, column=1, columnspan=2, sticky="w")

        # Data
        ttk.Label(param_frame, text="Data (hex):").grid(
            row=2, column=0, sticky="w", padx=4)
        self.data_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.data_var, width=50).grid(
            row=2, column=1, columnspan=2, sticky="w")

        # ── 傳送按鈕 ──────────────────────────────────────────────────
        ttk.Button(self, text="傳送", command=self._on_send).grid(
            row=3, column=0, columnspan=2, pady=6)

        # ── 日誌 ─────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="日誌", padding=6)
        log_frame.grid(row=4, column=0, columnspan=2, sticky="ew", **pad)

        self.log = scrolledtext.ScrolledText(log_frame, height=12, width=80,
                                             state="disabled",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        ttk.Button(self, text="清除日誌", command=self._clear_log).grid(
            row=5, column=0, columnspan=2, pady=(0, 6))

    # ── 裝置 ──────────────────────────────────────────────────────────
    def refresh_devices(self):
        self.device_list.delete(0, "end")
        self.devices = hid.enumerate()
        if not self.devices:
            self._log("未找到任何 HID 裝置")
            return
        for i, dev in enumerate(self.devices):
            line = (f"[{i:02d}] "
                    f"VID={dev['vendor_id']:04X}  PID={dev['product_id']:04X}  "
                    f"Usage={dev['usage_page']:04X}/{dev['usage']:04X}  "
                    f"| {dev['manufacturer_string']} - {dev['product_string']}")
            self.device_list.insert("end", line)
        self._log(f"找到 {len(self.devices)} 個 HID 裝置")

    def _show_descriptor(self):
        sel = self.device_list.curselection()
        if not sel:
            self._log("[錯誤] 請先選擇一個裝置")
            return
        device_info = self.devices[sel[0]]
        try:
            dev = hid.device()
            dev.open_path(device_info["path"])
            desc = dev.get_report_descriptor()
            dev.close()
            self._log(f"\nReport Descriptor ({len(desc)} bytes):")
            hex_str = " ".join(f"{b:02X}" for b in desc)
            for i in range(0, len(hex_str), 48):
                self._log(f"  {hex_str[i:i+48]}")
        except Exception as e:
            self._log(f"  [錯誤] {e}")

    # ── 傳送 ──────────────────────────────────────────────────────────
    def _on_send(self):
        sel = self.device_list.curselection()
        if not sel:
            self._log("[錯誤] 請先選擇一個裝置")
            return

        rid_str = self.report_id_var.get().strip()
        try:
            report_id = int(rid_str, 16)
            if not (0 <= report_id <= 255):
                raise ValueError
        except ValueError:
            self._log("[錯誤] Report ID 必須是 0x00~0xFF 的十六進位數值")
            return

        try:
            data = parse_hex_bytes(self.data_var.get().strip())
        except ValueError:
            self._log("[錯誤] Data 格式錯誤，請使用十六進位")
            return

        device_info = self.devices[sel[0]]
        use_feature = self.report_type.get() == "Feature"

        threading.Thread(target=self._send_report,
                         args=(device_info, report_id, data, use_feature),
                         daemon=True).start()

    def _send_report(self, device_info: dict, report_id: int,
                     data: list[int], use_feature: bool):
        path = device_info["path"]
        # Adapter 自動加 I2C HID framing，payload 只放原始 data
        payload = data
        self._log(f"\n發送 {'Feature' if use_feature else 'Output'} Report (HID over I2C)")
        self._log(f"  Report ID : 0x{report_id:02X} ({report_id})")
        self._log(f"  Data      : {' '.join(f'{b:02X}' for b in data)}")
        self._log(f"  完整 payload: {' '.join(f'{b:02X}' for b in payload)}")
        try:
            dev = hid.device()
            dev.open_path(path)
            dev.set_nonblocking(1)
            sent = dev.send_feature_report([report_id] + payload)
            if sent < 0:
                self._log(f"  [錯誤] 發送失敗 (回傳 {sent})")
            else:
                self._log(f"  [成功] 已發送 {sent} bytes")
            dev.close()
        except Exception as e:
            self._log(f"  [錯誤] {e}")

    # ── 日誌工具 ──────────────────────────────────────────────────────
    def _log(self, msg: str):
        def _append():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _append)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


if __name__ == "__main__":
    app = HIDSendApp()
    app.mainloop()
