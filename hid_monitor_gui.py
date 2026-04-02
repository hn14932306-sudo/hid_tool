"""
HID Monitor GUI — Windows RawInput + hidapi descriptor parser
Self-contained tkinter application.
"""

import collections
import ctypes
import ctypes.wintypes
import queue
import re
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

try:
    import hid  # hidapi Python bindings
except ImportError:
    hid = None

# ---------------------------------------------------------------------------
# HID Descriptor Parser
# ---------------------------------------------------------------------------

# bType constants
BTYPE_MAIN   = 0
BTYPE_GLOBAL = 1
BTYPE_LOCAL  = 2

# bTag for Main items
TAG_INPUT    = 8
TAG_OUTPUT   = 9
TAG_FEATURE  = 11
TAG_BEGIN_COLLECTION = 10
TAG_END_COLLECTION   = 12

# bTag for Global items
TAG_USAGE_PAGE    = 0
TAG_LOG_MIN       = 1
TAG_LOG_MAX       = 2
TAG_PHY_MIN       = 3
TAG_PHY_MAX       = 4
TAG_UNIT_EXP      = 5
TAG_UNIT          = 6
TAG_REPORT_SIZE   = 7
TAG_REPORT_ID     = 8
TAG_REPORT_COUNT  = 9
TAG_PUSH          = 10
TAG_POP           = 11

# bTag for Local items
TAG_USAGE         = 0
TAG_USAGE_MIN     = 1
TAG_USAGE_MAX     = 2

REPORT_TYPE_INPUT   = "Input"
REPORT_TYPE_OUTPUT  = "Output"
REPORT_TYPE_FEATURE = "Feature"


@dataclass
class HIDField:
    report_id: int
    report_type: str
    bit_offset: int        # relative to data after report-ID byte
    bit_size: int          # total bits = report_size * report_count
    report_count: int
    usage_page: int
    usages: List[int]
    logical_min: int
    logical_max: int
    flags: int
    is_const: bool

    @property
    def is_vendor(self) -> bool:
        return self.usage_page >= 0xFF00

    @property
    def label(self) -> str:
        if self.usages:
            u = self.usages[0]
            return f"UP={self.usage_page:#06x} U={u:#06x}"
        return f"UP={self.usage_page:#06x}"

    @property
    def per_bit_size(self) -> int:
        if self.report_count == 0:
            return 0
        return self.bit_size // self.report_count


def _signed(value: int, bits: int) -> int:
    """Sign-extend a value to a Python int."""
    if bits <= 0:
        return 0
    sign_bit = 1 << (bits - 1)
    if value & sign_bit:
        value -= (1 << bits)
    return value


def _decode_item_value(data: bytes, size: int) -> int:
    """Decode bSize bytes as unsigned int (signed extension handled by callers)."""
    if size == 0:
        return 0
    if size == 1:
        return data[0]
    if size == 2:
        return struct.unpack_from("<H", data)[0]
    if size == 4:
        return struct.unpack_from("<I", data)[0]
    return 0


def _decode_signed_item_value(data: bytes, size: int) -> int:
    """Decode bSize bytes as signed int."""
    if size == 0:
        return 0
    if size == 1:
        return struct.unpack_from("<b", data)[0]
    if size == 2:
        return struct.unpack_from("<h", data)[0]
    if size == 4:
        return struct.unpack_from("<i", data)[0]
    return 0


def parse_report_descriptor(raw: bytes) -> List[HIDField]:
    """Parse a HID Report Descriptor and return a list of HIDField objects."""

    fields: List[HIDField] = []

    # Global state
    usage_page   = 0
    logical_min  = 0
    logical_max  = 0
    physical_min = 0
    physical_max = 0
    unit_exp     = 0
    unit         = 0
    report_size  = 0
    report_id    = 0
    report_count = 0

    # Global state stack (Push/Pop)
    global_stack = []

    # Local state
    usages    : List[int] = []
    usage_min : Optional[int] = None
    usage_max : Optional[int] = None

    # bit offsets keyed by (report_id, report_type)
    bit_offsets: Dict[Tuple[int, str], int] = {}

    def reset_local():
        nonlocal usages, usage_min, usage_max
        usages    = []
        usage_min = None
        usage_max = None

    i = 0
    n = len(raw)
    while i < n:
        prefix = raw[i]
        i += 1

        # Long item check (prefix == 0xFE)
        if prefix == 0xFE:
            if i < n:
                long_size = raw[i]
                i += 1
                i += 1 + long_size  # skip bLongItemTag + data
            continue

        b_size = prefix & 0x03
        b_type = (prefix >> 2) & 0x03
        b_tag  = (prefix >> 4) & 0x0F

        # actual size: bSize==3 means 4 bytes
        actual_size = b_size if b_size < 3 else 4

        item_data = raw[i: i + actual_size]
        i += actual_size

        if b_type == BTYPE_GLOBAL:
            val_u = _decode_item_value(item_data, actual_size)
            val_s = _decode_signed_item_value(item_data, actual_size)

            if b_tag == TAG_USAGE_PAGE:
                usage_page = val_u
            elif b_tag == TAG_LOG_MIN:
                logical_min = val_s
            elif b_tag == TAG_LOG_MAX:
                logical_max = val_s
            elif b_tag == TAG_PHY_MIN:
                physical_min = val_s
            elif b_tag == TAG_PHY_MAX:
                physical_max = val_s
            elif b_tag == TAG_UNIT_EXP:
                unit_exp = val_s
            elif b_tag == TAG_UNIT:
                unit = val_u
            elif b_tag == TAG_REPORT_SIZE:
                report_size = val_u
            elif b_tag == TAG_REPORT_ID:
                report_id = val_u
            elif b_tag == TAG_REPORT_COUNT:
                report_count = val_u
            elif b_tag == TAG_PUSH:
                global_stack.append((
                    usage_page, logical_min, logical_max,
                    physical_min, physical_max, unit_exp, unit,
                    report_size, report_id, report_count
                ))
            elif b_tag == TAG_POP:
                if global_stack:
                    (usage_page, logical_min, logical_max,
                     physical_min, physical_max, unit_exp, unit,
                     report_size, report_id, report_count) = global_stack.pop()

        elif b_type == BTYPE_LOCAL:
            val_u = _decode_item_value(item_data, actual_size)

            if b_tag == TAG_USAGE:
                # If the usage_page portion is embedded (32-bit usage)
                if actual_size == 4:
                    usages.append(val_u & 0xFFFF)
                    # also update usage_page from upper word
                    # per HID spec extended usage
                    # usage_page = (val_u >> 16) & 0xFFFF  # don't override; descriptor already has it
                else:
                    usages.append(val_u)
            elif b_tag == TAG_USAGE_MIN:
                usage_min = val_u
            elif b_tag == TAG_USAGE_MAX:
                usage_max = val_u

        elif b_type == BTYPE_MAIN:
            if b_tag in (TAG_INPUT, TAG_OUTPUT, TAG_FEATURE):
                flags = _decode_item_value(item_data, actual_size)
                is_const = bool(flags & 0x01)

                if b_tag == TAG_INPUT:
                    rtype = REPORT_TYPE_INPUT
                elif b_tag == TAG_OUTPUT:
                    rtype = REPORT_TYPE_OUTPUT
                else:
                    rtype = REPORT_TYPE_FEATURE

                # Build effective usage list
                effective_usages: List[int] = list(usages)
                if usage_min is not None and usage_max is not None:
                    for u in range(usage_min, usage_max + 1):
                        effective_usages.append(u)

                # Pad or truncate to report_count
                if len(effective_usages) < report_count:
                    if effective_usages:
                        last = effective_usages[-1]
                        effective_usages += [last] * (report_count - len(effective_usages))
                    else:
                        effective_usages = [0] * report_count
                # (don't truncate — keep all for reference)

                key = (report_id, rtype)
                bit_offset = bit_offsets.get(key, 0)

                total_bits = report_size * report_count

                hf = HIDField(
                    report_id=report_id,
                    report_type=rtype,
                    bit_offset=bit_offset,
                    bit_size=total_bits,
                    report_count=report_count,
                    usage_page=usage_page,
                    usages=effective_usages[:report_count] if effective_usages else [],
                    logical_min=logical_min,
                    logical_max=logical_max,
                    flags=flags,
                    is_const=is_const,
                )
                fields.append(hf)

                bit_offsets[key] = bit_offset + total_bits

            # Always reset local state after a Main item
            reset_local()

    return fields


# ---------------------------------------------------------------------------
# Field value extractor
# ---------------------------------------------------------------------------

def extract_field_value(
    data: bytes,
    bit_offset: int,
    per_bit_size: int,
    count: int,
    logical_min: int,
) -> List[int]:
    """
    Extract `count` values of `per_bit_size` bits each from `data`
    starting at `bit_offset`. Sign-extend if logical_min < 0.
    """
    results = []
    if per_bit_size <= 0 or count <= 0:
        return results

    do_sign = logical_min < 0

    for idx in range(count):
        start_bit = bit_offset + idx * per_bit_size
        start_byte = start_bit >> 3
        end_byte   = (start_bit + per_bit_size - 1) >> 3

        if end_byte >= len(data):
            break

        # Accumulate bytes
        raw_val = 0
        for b in range(end_byte, start_byte - 1, -1):
            raw_val = (raw_val << 8) | data[b]

        # Shift out lower bits that belong to earlier fields
        shift = start_bit - (start_byte * 8)
        raw_val >>= shift

        # Mask to per_bit_size
        mask = (1 << per_bit_size) - 1
        raw_val &= mask

        if do_sign:
            raw_val = _signed(raw_val, per_bit_size)

        results.append(raw_val)

    return results


# ---------------------------------------------------------------------------
# Windows RawInput structures & constants
# ---------------------------------------------------------------------------

WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEHID = 2
RIDI_DEVICENAME = 0x20000007
RIDI_PREPARSEDDATA = 0x20000005

HWND_MESSAGE = ctypes.wintypes.HWND(-3)

# WNDCLASSEX
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        ctypes.c_uint),
        ("style",         ctypes.c_uint),
        ("lpfnWndProc",   ctypes.c_void_p),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     ctypes.wintypes.HINSTANCE),
        ("hIcon",         ctypes.wintypes.HICON),
        ("hCursor",       ctypes.c_void_p),
        ("hbrBackground", ctypes.wintypes.HBRUSH),
        ("lpszMenuName",  ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ("hIconSm",       ctypes.wintypes.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_ushort),
        ("usUsage",     ctypes.c_ushort),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("hwndTarget",  ctypes.wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  ctypes.wintypes.DWORD),
        ("dwSize",  ctypes.wintypes.DWORD),
        ("hDevice", ctypes.wintypes.HANDLE),
        ("wParam",  ctypes.wintypes.WPARAM),
    ]


class RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", ctypes.wintypes.DWORD),
        ("dwCount",   ctypes.wintypes.DWORD),
        # raw bytes follow
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        # union: we handle HID manually
    ]


WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.wintypes.HWND,
    ctypes.c_uint,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


def get_device_name_from_handle(hDevice) -> str:
    """Use GetRawInputDeviceInfoW to retrieve the device name string."""
    user32 = ctypes.windll.user32
    size = ctypes.wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value + 1)
    user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value


# ---------------------------------------------------------------------------
# RawInput background thread
# ---------------------------------------------------------------------------

class RawInputThread(threading.Thread):
    """
    Background thread that creates a hidden message-only window,
    registers RawInput devices, and pumps the Windows message loop.
    Incoming HID packets are placed into `packet_queue`.
    """

    def __init__(self, packet_queue: queue.Queue, extra_usage_page: int = 0, extra_usage: int = 0):
        super().__init__(daemon=True)
        self.packet_queue     = packet_queue
        self.extra_usage_page = extra_usage_page
        self.extra_usage      = extra_usage
        self._hwnd            = None
        self._stop_event      = threading.Event()
        self._ready_event     = threading.Event()

    def run(self):
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hInstance = kernel32.GetModuleHandleW(None)

        # Define window procedure
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                self._handle_wm_input(hwnd, lparam)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_cb = WNDPROCTYPE(wnd_proc)

        class_name = "HIDMonitorMsgWnd"
        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
        wc.style         = 0
        wc.lpfnWndProc   = ctypes.cast(self._wnd_proc_cb, ctypes.c_void_p)
        wc.hInstance     = hInstance
        wc.lpszClassName = class_name

        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            # class may already be registered; ignore ERROR_CLASS_ALREADY_EXISTS (1410)
            if err != 1410:
                self._ready_event.set()
                return

        hwnd = user32.CreateWindowExW(
            0,                    # dwExStyle
            class_name,           # lpClassName
            "HIDMonitor",         # lpWindowName
            0,                    # dwStyle
            0, 0, 0, 0,           # x, y, w, h
            HWND_MESSAGE,         # hWndParent (message-only)
            None,                 # hMenu
            hInstance,            # hInstance
            None,                 # lpParam
        )
        if not hwnd:
            self._ready_event.set()
            return

        self._hwnd = hwnd

        # Register RawInput devices
        devices = []
        usages = [
            (0x000D, 0x04),  # Touch Screen
            (0x000D, 0x05),  # Touch Pad
            (0x000D, 0x01),  # Digitizer
        ]
        if self.extra_usage_page and self.extra_usage:
            pair = (self.extra_usage_page, self.extra_usage)
            if pair not in usages:
                usages.append(pair)

        for up, u in usages:
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = up
            rid.usUsage     = u
            rid.dwFlags     = RIDEV_INPUTSINK
            rid.hwndTarget  = hwnd
            devices.append(rid)

        arr = (RAWINPUTDEVICE * len(devices))(*devices)
        ok = user32.RegisterRawInputDevices(
            arr,
            len(devices),
            ctypes.sizeof(RAWINPUTDEVICE),
        )
        if not ok:
            pass  # non-fatal; some usages may not be present

        self._ready_event.set()

        # Message loop
        msg = ctypes.wintypes.MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_wm_input(self, hwnd, lparam):
        user32 = ctypes.windll.user32

        hRawInput = ctypes.wintypes.HANDLE(lparam)

        # First call: get required size
        dw_size = ctypes.wintypes.UINT(0)
        user32.GetRawInputData(
            hRawInput,
            0x10000003,        # RID_INPUT
            None,
            ctypes.byref(dw_size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if dw_size.value == 0:
            return

        buf = ctypes.create_string_buffer(dw_size.value)
        bytes_copied = user32.GetRawInputData(
            hRawInput,
            0x10000003,
            buf,
            ctypes.byref(dw_size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if bytes_copied == 0:
            return

        raw_bytes = bytes(buf.raw[:bytes_copied])

        # Parse RAWINPUTHEADER (16 bytes on 32-bit, 24 bytes on 64-bit)
        hdr_size = ctypes.sizeof(RAWINPUTHEADER)
        if len(raw_bytes) < hdr_size:
            return

        hdr = RAWINPUTHEADER.from_buffer_copy(raw_bytes[:hdr_size])

        if hdr.dwType != RIM_TYPEHID:
            return  # only interested in HID

        # After header: RAWHID struct
        rawhid_offset = hdr_size
        if len(raw_bytes) < rawhid_offset + ctypes.sizeof(RAWHID):
            return

        rawhid = RAWHID.from_buffer_copy(raw_bytes[rawhid_offset: rawhid_offset + ctypes.sizeof(RAWHID)])
        data_offset = rawhid_offset + ctypes.sizeof(RAWHID)

        size_hid = rawhid.dwSizeHid
        count    = rawhid.dwCount

        if size_hid == 0 or count == 0:
            return

        # Get device name
        try:
            device_name = get_device_name_from_handle(hdr.hDevice)
        except Exception:
            device_name = ""

        # Extract each HID report
        for idx in range(count):
            start = data_offset + idx * size_hid
            end   = start + size_hid
            if end > len(raw_bytes):
                break
            report_data = raw_bytes[start:end]
            self.packet_queue.put({
                "device_handle": hdr.hDevice,
                "device_name":   device_name,
                "data":          report_data,
                "rx_time":       time.monotonic(),
            })

    def stop(self):
        self._stop_event.set()
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT


# ---------------------------------------------------------------------------
# HID device enumeration helpers
# ---------------------------------------------------------------------------

def enumerate_hid_devices():
    """Return list of dicts from hid.enumerate(), or empty list if unavailable."""
    if hid is None:
        return []
    try:
        return hid.enumerate()
    except Exception:
        return []


def format_device_label(dev: dict) -> str:
    vid  = dev.get("vendor_id",  0)
    pid  = dev.get("product_id", 0)
    mfr  = dev.get("manufacturer_string", "") or ""
    prod = dev.get("product_string",      "") or ""
    path = dev.get("path", b"")
    if isinstance(path, bytes):
        path = path.decode("utf-8", errors="replace")
    label = f"VID={vid:04X} PID={pid:04X}  {mfr} {prod}  [{path}]"
    return label.strip()


def read_descriptor_via_hidapi(path) -> Optional[bytes]:
    """Open a HID device by path and read its report descriptor."""
    if hid is None:
        return None
    if isinstance(path, str):
        path_bytes = path.encode("utf-8")
    else:
        path_bytes = path
    try:
        dev = hid.device()
        dev.open_path(path_bytes)
        try:
            desc = dev.get_report_descriptor()
            if isinstance(desc, list):
                desc = bytes(desc)
            return desc
        finally:
            dev.close()
    except Exception as e:
        return None


# ---------------------------------------------------------------------------
# Usage name lookup
# ---------------------------------------------------------------------------

_USAGE_NAME: Dict[Tuple[int, int], str] = {
    (0x01, 0x30): "X",
    (0x01, 0x31): "Y",
    (0x01, 0x32): "Z",
    (0x01, 0x33): "Rx",
    (0x01, 0x34): "Ry",
    (0x01, 0x35): "Rz",
    (0x0D, 0x30): "TipPressure",
    (0x0D, 0x32): "InRange",
    (0x0D, 0x33): "Touch",
    (0x0D, 0x42): "TipSwitch",
    (0x0D, 0x43): "SecTipSwitch",
    (0x0D, 0x44): "BarrelSwitch",
    (0x0D, 0x47): "Confidence",
    (0x0D, 0x48): "Width",
    (0x0D, 0x49): "Height",
    (0x0D, 0x51): "ContactID",
    (0x0D, 0x52): "DeviceMode",
    (0x0D, 0x54): "ContactCount",
    (0x0D, 0x55): "ContactCountMax",
    (0x0D, 0x56): "ScanTime",
    (0x0D, 0x3D): "XTilt",
    (0x0D, 0x3E): "YTilt",
    (0x0D, 0x41): "Twist",
}


def _get_usage_name(usage_page: int, usage: int) -> str:
    name = _USAGE_NAME.get((usage_page, usage))
    if name:
        return name
    if usage_page >= 0xFF00:
        return f"V{usage:02X}"
    return f"UP{usage_page:02X}_U{usage:02X}"


# ---------------------------------------------------------------------------
# Device name matching
# ---------------------------------------------------------------------------

_VID_PID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", re.IGNORECASE)


def extract_vid_pid(name: str) -> Optional[Tuple[str, str]]:
    m = _VID_PID_RE.search(name)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    return None


def match_device_name_to_hidapi(windows_name: str, hidapi_devices: List[dict]) -> Optional[dict]:
    """
    Match a Windows RawInput device name to a hidapi device dict
    using case-insensitive VID/PID substring match.
    """
    vp = extract_vid_pid(windows_name)
    if vp is None:
        return None
    vid, pid = vp

    for dev in hidapi_devices:
        path = dev.get("path", b"")
        if isinstance(path, bytes):
            path_str = path.decode("utf-8", errors="replace")
        else:
            path_str = path
        dvp = extract_vid_pid(path_str)
        if dvp and dvp[0] == vid and dvp[1] == pid:
            return dev
    return None


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------

class HIDMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HID Monitor — RawInput + Descriptor Parser")
        self.geometry("1200x750")
        self.minsize(900, 600)

        # State
        self._hidapi_devices: List[dict]              = []
        self._selected_dev:   Optional[dict]          = None
        self._descriptors:    Dict[str, List[HIDField]] = {}
        self._raw_thread:     Optional[RawInputThread] = None
        self._packet_queue:   queue.Queue             = queue.Queue()
        self._listening:      bool                    = False
        self._col_defs:       List[dict]              = []   # column definitions for table
        self._table_rid:      int                     = -1   # report ID the table is built for
        self._frame_deque:      collections.deque    = collections.deque()
        self._last_pkt_rx_time: float              = 0.0
        self._last_scan_time:   int                = -1
        self._scan_time_field:  Optional[HIDField] = None

        # Build UI
        self._build_ui()
        self._refresh_devices()
        # Start polling loop
        self.after(20, self._poll_queue)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ---- Top bar ----
        top = tk.Frame(self, bd=2, relief=tk.RAISED, padx=4, pady=4)
        top.pack(side=tk.TOP, fill=tk.X)

        tk.Label(top, text="裝置:").pack(side=tk.LEFT)
        self._dev_var = tk.StringVar()
        self._dev_combo = ttk.Combobox(top, textvariable=self._dev_var, width=70, state="readonly")
        self._dev_combo.pack(side=tk.LEFT, padx=(2, 8))
        self._dev_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        self._refresh_btn = tk.Button(top, text="重新整理", command=self._refresh_devices)
        self._refresh_btn.pack(side=tk.LEFT, padx=2)

        self._listen_btn = tk.Button(top, text="開始監聽", command=self._toggle_listen,
                                     bg="#4CAF50", fg="white", font=("Arial", 10, "bold"))
        self._listen_btn.pack(side=tk.LEFT, padx=8)

        # ---- PanedWindow (left descriptor | right log) ----
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # -- Left panel: descriptor tree --
        left_frame = tk.LabelFrame(paned, text="Report Descriptor 欄位", padx=2, pady=2)
        paned.add(left_frame, minsize=320)

        self._desc_tree = ttk.Treeview(
            left_frame,
            columns=("bit_size", "logical_range"),
            show="tree headings",
        )
        self._desc_tree.heading("#0",           text="欄位 / 名稱")
        self._desc_tree.heading("bit_size",     text="位元大小")
        self._desc_tree.heading("logical_range",text="Logical 範圍")
        self._desc_tree.column("#0",            width=200, stretch=True)
        self._desc_tree.column("bit_size",      width=70,  stretch=False)
        self._desc_tree.column("logical_range", width=100, stretch=False)

        desc_sb = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self._desc_tree.yview)
        self._desc_tree.configure(yscrollcommand=desc_sb.set)
        desc_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._desc_tree.pack(fill=tk.BOTH, expand=True)

        # Tag colours for descriptor tree
        self._desc_tree.tag_configure("vendor", foreground="darkorange", font=("Consolas", 9, "bold"))
        self._desc_tree.tag_configure("touch",  foreground="darkgreen")
        self._desc_tree.tag_configure("const",  foreground="gray")

        # -- Right panel --
        right_frame = tk.Frame(paned)
        paned.add(right_frame, minsize=500)

        # Control row
        ctrl_row = tk.Frame(right_frame)
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

        tk.Checkbutton(ctrl_row, text="只顯示含 Vendor 資料", variable=self._only_vendor).pack(side=tk.LEFT)
        tk.Checkbutton(ctrl_row, text="顯示 RAW 欄位", variable=self._show_raw,
                       command=self._rebuild_table_columns).pack(side=tk.LEFT, padx=8)
        tk.Button(ctrl_row, text="清除", command=self._clear_log).pack(side=tk.RIGHT, padx=4)

        # Table (Treeview)
        tbl_frame = tk.Frame(right_frame)
        tbl_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self._table = ttk.Treeview(tbl_frame, show="headings",
                                   selectmode="browse")
        vsb = ttk.Scrollbar(tbl_frame, orient=tk.VERTICAL,   command=self._table.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient=tk.HORIZONTAL, command=self._table.xview)
        self._table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._table.pack(fill=tk.BOTH, expand=True)

        # Row colour tags
        self._table.tag_configure("vendor_row", background="#3a2800")
        self._table.tag_configure("normal_row", background="")

        # ---- Status bar ----
        sb_frame = tk.Frame(self, bd=1, relief=tk.SUNKEN)
        sb_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self._status_var = tk.StringVar(value="就緒")
        tk.Label(sb_frame, textvariable=self._status_var,
                 anchor=tk.W, font=("Arial", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._rate_var = tk.StringVar(value="")
        tk.Label(sb_frame, textvariable=self._rate_var, anchor=tk.E,
                 font=("Consolas", 9, "bold"), width=14).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Device Management
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
        # Clear existing
        for item in self._desc_tree.get_children():
            self._desc_tree.delete(item)

        fields = self._descriptors.get(path_str, [])
        if not fields:
            self._desc_tree.insert("", tk.END, text="（無 Descriptor 資料）")
            return

        # Group: report_id -> report_type -> fields
        from collections import OrderedDict
        groups: Dict[int, Dict[str, List[HIDField]]] = OrderedDict()
        for f in fields:
            if f.report_id not in groups:
                groups[f.report_id] = OrderedDict()
            if f.report_type not in groups[f.report_id]:
                groups[f.report_id][f.report_type] = []
            groups[f.report_id][f.report_type].append(f)

        for rid, types in groups.items():
            rid_node = self._desc_tree.insert(
                "", tk.END,
                text=f"Report ID = {rid:#04x}",
                open=True,
            )
            for rtype, flist in types.items():
                type_node = self._desc_tree.insert(
                    rid_node, tk.END,
                    text=rtype,
                    open=True,
                )
                for hf in flist:
                    tag = "const" if hf.is_const else ("vendor" if hf.is_vendor else "touch")
                    label = hf.label
                    lrange = f"[{hf.logical_min}, {hf.logical_max}]"
                    self._desc_tree.insert(
                        type_node, tk.END,
                        text=label,
                        values=(hf.bit_size, lrange),
                        tags=(tag,),
                    )

        count = len(fields)
        vendor_count = sum(1 for f in fields if f.is_vendor)
        self._status_var.set(
            f"Descriptor 載入完成: {count} 個欄位 (其中 {vendor_count} 個 Vendor 欄位)"
        )

        # Populate Report ID combobox and build table columns
        input_rids = sorted(set(
            f.report_id for f in fields
            if f.report_type == REPORT_TYPE_INPUT and not f.is_const
        ))
        options = ["全部"] + [f"0x{r:02X}" for r in input_rids]
        self._rid_combo["values"] = options
        # Default to first specific RID so columns are well-defined
        if len(input_rids) == 1:
            self._rid_filter_var.set(f"0x{input_rids[0]:02X}")
        else:
            self._rid_filter_var.set("全部")
        self._rebuild_table_columns()

    def _rebuild_table_columns(self):
        """Re-configure Treeview columns from the current descriptor + RID filter."""
        path_str = self._get_dev_path_str(self._selected_dev) if self._selected_dev else ""
        fields = self._descriptors.get(path_str, [])
        self._setup_table_columns(fields)

    def _setup_table_columns(self, fields: List[HIDField]):
        """Build Treeview columns from Input fields, respecting the RID filter."""
        # Determine which report ID(s) to build columns for
        rid_sel = self._rid_filter_var.get()
        if rid_sel == "全部":
            target_rid = None          # accept all
        else:
            try:
                target_rid = int(rid_sel, 16)
            except ValueError:
                target_rid = None

        input_fields = [
            f for f in fields
            if f.report_type == REPORT_TYPE_INPUT and not f.is_const
            and (target_rid is None or f.report_id == target_rid)
        ]

        col_defs: List[dict] = []

        # Always show RID column
        col_defs.append({"col_id": "__rid__", "label": "RID", "width": 50,
                         "field_ref": None, "value_index": -1, "byte_index": -1})

        # Optional RAW column
        if self._show_raw.get():
            col_defs.append({"col_id": "__raw__", "label": "RAW", "width": 220,
                             "field_ref": None, "value_index": -1, "byte_index": -1})

        # Count total occurrences per (usage_page, usage) for numbering
        usage_total: Dict[Tuple[int, int], int] = {}
        for hf in input_fields:
            if not hf.is_vendor:
                for i in range(hf.report_count):
                    u = hf.usages[i] if i < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                    k = (hf.usage_page, u)
                    usage_total[k] = usage_total.get(k, 0) + 1

        usage_seen: Dict[Tuple[int, int], int] = {}
        vendor_byte_idx = 0

        for hf in input_fields:
            if hf.is_vendor:
                total_bytes = max(1, (hf.bit_size + 7) // 8)
                for b in range(total_bytes):
                    col_defs.append({
                        "col_id":      f"vnd_{id(hf)}_{b}",
                        "label":       f"V{vendor_byte_idx}",
                        "width":       40,
                        "field_ref":   hf,
                        "value_index": -1,
                        "byte_index":  b,
                    })
                    vendor_byte_idx += 1
            else:
                for i in range(hf.report_count):
                    u = hf.usages[i] if i < len(hf.usages) else (hf.usages[-1] if hf.usages else 0)
                    k = (hf.usage_page, u)
                    occ = usage_seen.get(k, 0)
                    usage_seen[k] = occ + 1
                    base = _get_usage_name(hf.usage_page, u)
                    label = f"{base}[{occ}]" if usage_total.get(k, 1) > 1 else base
                    col_defs.append({
                        "col_id":      f"fld_{id(hf)}_{i}",
                        "label":       label,
                        "width":       65,
                        "field_ref":   hf,
                        "value_index": i,
                        "byte_index":  -1,
                    })

        self._col_defs = col_defs
        self._table_rid = target_rid if target_rid is not None else -1

        self._last_pkt_rx_time = 0.0
        self._last_scan_time   = -1
        # Find ScanTime field for frame boundary detection
        self._scan_time_field = next(
            (hf for hf in input_fields if not hf.is_vendor
             and any((hf.usage_page, u) == (0x0D, 0x56) for u in hf.usages)), None)

        # Apply to Treeview
        ids = [c["col_id"] for c in col_defs]
        self._table["columns"] = ids
        self._table["show"] = "headings"
        for c in col_defs:
            self._table.heading(c["col_id"], text=c["label"])
            self._table.column(c["col_id"], width=c["width"],
                               stretch=(c["col_id"] == "__raw__"), anchor="center")

    # ------------------------------------------------------------------
    # Listen / Stop
    # ------------------------------------------------------------------

    def _toggle_listen(self):
        if self._listening:
            self._stop_listen()
        else:
            self._start_listen()

    def _start_listen(self):
        if self._raw_thread and self._raw_thread.is_alive():
            return

        extra_up = 0
        extra_u  = 0
        if self._selected_dev:
            extra_up = self._selected_dev.get("usage_page", 0)
            extra_u  = self._selected_dev.get("usage",      0)

        self._raw_thread = RawInputThread(
            self._packet_queue,
            extra_usage_page=extra_up,
            extra_usage=extra_u,
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
    # Queue Polling
    # ------------------------------------------------------------------

    def _is_new_frame(self, pkt: dict, rx_time: float, gap_threshold: float) -> bool:
        """
        Detect frame boundary.
        Primary: ScanTime value changed (reliable across hybrid/parallel/serial).
        Fallback: rx_time gap when no ScanTime field available.
        """
        if self._scan_time_field is not None:
            data = pkt.get("data", b"")
            rid  = data[0] if data else 0
            if rid == self._scan_time_field.report_id:
                payload = data[1:] if len(data) > 1 else b""
                hf = self._scan_time_field
                vals = extract_field_value(payload, hf.bit_offset,
                                           hf.per_bit_size, hf.report_count,
                                           hf.logical_min)
                if vals:
                    st = vals[0]
                    if st != self._last_scan_time:
                        self._last_scan_time = st
                        return True
                    return False
        # Fallback: time gap
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

            now = time.monotonic()
            cutoff = now - 1.0
            while self._frame_deque and self._frame_deque[0] < cutoff:
                self._frame_deque.popleft()
            self._rate_var.set(f"{len(self._frame_deque):4d} scan/s")

        self.after(20, self._poll_queue)

    # ------------------------------------------------------------------
    # Packet Handling & Display
    # ------------------------------------------------------------------

    _MAX_ROWS = 300

    def _handle_packet(self, pkt: dict):
        device_name: str = pkt.get("device_name", "")
        data: bytes      = pkt.get("data", b"")
        if not data:
            return

        report_id = data[0]
        payload   = data[1:] if len(data) > 1 else b""

        # Report ID filter
        if self._table_rid != -1 and report_id != self._table_rid:
            return

        # Match descriptor
        descriptor_fields: Optional[List[HIDField]] = None
        matched_dev = match_device_name_to_hidapi(device_name, self._hidapi_devices)
        if matched_dev:
            descriptor_fields = self._descriptors.get(self._get_dev_path_str(matched_dev))

        # Vendor-only filter
        if self._only_vendor.get():
            if not descriptor_fields:
                return
            has_vendor = any(
                f.is_vendor
                for f in descriptor_fields
                if f.report_type == REPORT_TYPE_INPUT
                and f.report_id == report_id
                and not f.is_const
            )
            if not has_vendor:
                return

        if not self._col_defs:
            return

        # Cache per-field extracted values
        field_cache: Dict[int, List[int]] = {}

        def get_field_values(hf: HIDField) -> List[int]:
            key = id(hf)
            if key not in field_cache:
                per_bit = hf.per_bit_size
                field_cache[key] = extract_field_value(
                    payload, hf.bit_offset, per_bit, hf.report_count, hf.logical_min
                ) if per_bit > 0 else []
            return field_cache[key]

        # Build row
        row = []
        has_vendor_data = False
        for col in self._col_defs:
            cid = col["col_id"]
            if cid == "__rid__":
                row.append(f"0x{report_id:02X}")
            elif cid == "__raw__":
                row.append(" ".join(f"{b:02X}" for b in data))
            elif col["byte_index"] >= 0:
                # Vendor byte
                hf = col["field_ref"]
                byte_pos = (hf.bit_offset // 8) + col["byte_index"]
                val = payload[byte_pos] if byte_pos < len(payload) else 0
                row.append(f"{val:02X}")
                if val != 0:
                    has_vendor_data = True
            else:
                hf  = col["field_ref"]
                idx = col["value_index"]
                vals = get_field_values(hf)
                row.append(vals[idx] if idx < len(vals) else "")

        tag = "vendor_row" if has_vendor_data else "normal_row"
        self._table.insert("", 0, values=row, tags=(tag,))

        # Trim excess rows
        children = self._table.get_children()
        if len(children) > self._MAX_ROWS:
            for iid in children[self._MAX_ROWS:]:
                self._table.delete(iid)

    def _clear_log(self):
        for iid in self._table.get_children():
            self._table.delete(iid)

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
    HIDMonitorApp().mainloop()
