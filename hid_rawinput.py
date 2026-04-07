"""
Windows RawInput structures, constants, and background listener thread.
"""

import ctypes
import ctypes.wintypes
import queue
import threading
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WM_INPUT        = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEHID     = 2
RIDI_DEVICENAME = 0x20000007

HWND_MESSAGE = ctypes.wintypes.HWND(-3)

# ---------------------------------------------------------------------------
# Win32 structures
# ---------------------------------------------------------------------------

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
    ]


# On 64-bit Windows, LRESULT/WPARAM/LPARAM are pointer-sized (8 bytes).
# ctypes.wintypes.LPARAM is c_long (4 bytes on Windows), so use c_ssize_t instead.
WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,   # LRESULT
    ctypes.wintypes.HWND,
    ctypes.c_uint,
    ctypes.c_size_t,    # WPARAM
    ctypes.c_ssize_t,   # LPARAM
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def get_device_name_from_handle(hDevice) -> str:
    """Use GetRawInputDeviceInfoW to retrieve the device name string."""
    user32 = ctypes.windll.user32
    size   = ctypes.wintypes.UINT(0)
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
    Creates a hidden message-only window, registers RawInput HID devices,
    and pumps the Windows message loop. Incoming HID packets are placed
    into `packet_queue` as dicts:
        {"device_handle", "device_name", "data": bytes, "rx_time": float}
    """

    def __init__(
        self,
        packet_queue: queue.Queue,
        extra_usage_page: int = 0,
        extra_usage: int = 0,
    ):
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

        # Declare DefWindowProcW with pointer-sized types to avoid 64-bit overflow
        user32.DefWindowProcW.restype  = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.c_uint,
            ctypes.c_size_t,    # WPARAM
            ctypes.c_ssize_t,   # LPARAM
        ]

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                self._handle_wm_input(hwnd, lparam)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_cb = WNDPROCTYPE(wnd_proc)

        class_name = "HIDToolMsgWnd"
        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
        wc.style         = 0
        wc.lpfnWndProc   = ctypes.cast(self._wnd_proc_cb, ctypes.c_void_p)
        wc.hInstance     = hInstance
        wc.lpszClassName = class_name

        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if kernel32.GetLastError() != 1410:   # ERROR_CLASS_ALREADY_EXISTS
                self._ready_event.set()
                return

        hwnd = user32.CreateWindowExW(
            0, class_name, "HIDTool", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, hInstance, None,
        )
        if not hwnd:
            self._ready_event.set()
            return

        self._hwnd = hwnd

        # Register HID usage pages to capture
        usages = [(0x000D, 0x04), (0x000D, 0x05), (0x000D, 0x01)]
        if self.extra_usage_page and self.extra_usage:
            pair = (self.extra_usage_page, self.extra_usage)
            if pair not in usages:
                usages.append(pair)

        devices = []
        for up, u in usages:
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = up
            rid.usUsage     = u
            rid.dwFlags     = RIDEV_INPUTSINK
            rid.hwndTarget  = hwnd
            devices.append(rid)

        arr = (RAWINPUTDEVICE * len(devices))(*devices)
        user32.RegisterRawInputDevices(arr, len(devices), ctypes.sizeof(RAWINPUTDEVICE))

        self._ready_event.set()

        msg = ctypes.wintypes.MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_wm_input(self, hwnd, lparam):
        user32    = ctypes.windll.user32
        hRawInput = ctypes.wintypes.HANDLE(lparam)

        dw_size = ctypes.wintypes.UINT(0)
        user32.GetRawInputData(hRawInput, 0x10000003, None, ctypes.byref(dw_size),
                               ctypes.sizeof(RAWINPUTHEADER))
        if dw_size.value == 0:
            return

        buf          = ctypes.create_string_buffer(dw_size.value)
        bytes_copied = user32.GetRawInputData(hRawInput, 0x10000003, buf,
                                              ctypes.byref(dw_size),
                                              ctypes.sizeof(RAWINPUTHEADER))
        if bytes_copied == 0:
            return

        raw_bytes = bytes(buf.raw[:bytes_copied])
        hdr_size  = ctypes.sizeof(RAWINPUTHEADER)
        if len(raw_bytes) < hdr_size:
            return

        hdr = RAWINPUTHEADER.from_buffer_copy(raw_bytes[:hdr_size])
        if hdr.dwType != RIM_TYPEHID:
            return

        rawhid_offset = hdr_size
        if len(raw_bytes) < rawhid_offset + ctypes.sizeof(RAWHID):
            return

        rawhid      = RAWHID.from_buffer_copy(
            raw_bytes[rawhid_offset: rawhid_offset + ctypes.sizeof(RAWHID)]
        )
        data_offset = rawhid_offset + ctypes.sizeof(RAWHID)
        size_hid    = rawhid.dwSizeHid
        count       = rawhid.dwCount

        if size_hid == 0 or count == 0:
            return

        try:
            device_name = get_device_name_from_handle(hdr.hDevice)
        except Exception:
            device_name = ""

        for idx in range(count):
            start = data_offset + idx * size_hid
            end   = start + size_hid
            if end > len(raw_bytes):
                break
            self.packet_queue.put({
                "device_handle": hdr.hDevice,
                "device_name":   device_name,
                "data":          raw_bytes[start:end],
                "rx_time":       time.monotonic(),
            })

    def stop(self):
        self._stop_event.set()
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT
