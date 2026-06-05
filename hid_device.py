"""
hidapi device enumeration, descriptor reading, VID/PID matching,
and HID report send/receive helpers.
"""

import re
import sys
import ctypes
import ctypes.wintypes as wintypes
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:
    hid = None

# ---------------------------------------------------------------------------
# Windows-only ctypes setup for HidD_GetInputReport
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _hid_dll  = ctypes.WinDLL("hid",      use_last_error=True)

    _kernel32.CreateFileW.restype  = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CloseHandle.restype  = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _hid_dll.HidD_GetInputReport.restype  = wintypes.BOOL
    _hid_dll.HidD_GetInputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]

    _hid_dll.HidD_GetPreparsedData.restype  = wintypes.BOOL
    _hid_dll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    _hid_dll.HidD_FreePreparsedData.restype  = wintypes.BOOL
    _hid_dll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    _hid_dll.HidP_GetCaps.restype  = wintypes.LONG
    _hid_dll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    _HIDP_STATUS_SUCCESS = 0x00110000

    class _HIDP_CAPS(ctypes.Structure):
        _fields_ = [
            ("Usage",                      wintypes.USHORT),
            ("UsagePage",                  wintypes.USHORT),
            ("InputReportByteLength",      wintypes.USHORT),
            ("OutputReportByteLength",     wintypes.USHORT),
            ("FeatureReportByteLength",    wintypes.USHORT),
            ("Reserved",                   wintypes.USHORT * 17),
            ("NumberLinkCollectionNodes",  wintypes.USHORT),
            ("NumberInputButtonCaps",      wintypes.USHORT),
            ("NumberInputValueCaps",       wintypes.USHORT),
            ("NumberInputDataIndices",     wintypes.USHORT),
            ("NumberOutputButtonCaps",     wintypes.USHORT),
            ("NumberOutputValueCaps",      wintypes.USHORT),
            ("NumberOutputDataIndices",    wintypes.USHORT),
            ("NumberFeatureButtonCaps",    wintypes.USHORT),
            ("NumberFeatureValueCaps",     wintypes.USHORT),
            ("NumberFeatureDataIndices",   wintypes.USHORT),
        ]

    _hid_dll.HidD_SetOutputReport.restype  = wintypes.BOOL
    _hid_dll.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]

    _kernel32.DeviceIoControl.restype  = wintypes.BOOL
    _kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]

    _GENERIC_READ       = 0x80000000
    _GENERIC_WRITE      = 0x40000000
    _FILE_SHARE_READ    = 0x00000001
    _FILE_SHARE_WRITE   = 0x00000002
    _OPEN_EXISTING      = 3
    _INVALID_HANDLE_VAL = ctypes.c_void_p(-1).value
    # CTL_CODE(FILE_DEVICE_KEYBOARD=0xB, func=105, METHOD_IN_DIRECT=1, FILE_ANY_ACCESS=0)
    _IOCTL_HID_SET_OUTPUT_REPORT = 0x000B01A5


def _open_win_handle(path):
    path_str = path.decode("utf-8", errors="replace") if isinstance(path, bytes) else path
    h = _kernel32.CreateFileW(
        path_str,
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, 0, None,
    )
    if h is None or h == _INVALID_HANDLE_VAL:
        err = ctypes.get_last_error()
        raise RuntimeError(f"CreateFileW: ({err:#010x}) {ctypes.FormatError(err)}")
    return h

# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def enumerate_hid_devices() -> List[dict]:
    """Return list of dicts from hid.enumerate(), or empty list if unavailable."""
    if hid is None:
        return []
    try:
        return hid.enumerate()
    except Exception:
        return []


_COL_RE = re.compile(r"&Col(\d+)", re.IGNORECASE)


def format_device_label(dev: dict) -> str:
    vid  = dev.get("vendor_id",  0)
    pid  = dev.get("product_id", 0)
    mfr  = dev.get("manufacturer_string", "") or ""
    prod = dev.get("product_string",      "") or ""
    path = dev.get("path", b"")
    if isinstance(path, bytes):
        path = path.decode("utf-8", errors="replace")
    m = _COL_RE.search(path)
    col_tag = f" [Col{int(m.group(1)):02d}]" if m else " [TLC]"
    return f"VID={vid:04X} PID={pid:04X}{col_tag}  {mfr} {prod}  [{path}]".strip()


# ---------------------------------------------------------------------------
# Descriptor reading
# ---------------------------------------------------------------------------

def read_descriptor_via_hidapi(path) -> Optional[bytes]:
    """Open a HID device by path and read its report descriptor."""
    if hid is None:
        return None
    path_bytes = path.encode("utf-8") if isinstance(path, str) else path
    try:
        dev = hid.device()
        dev.open_path(path_bytes)
        try:
            desc = dev.get_report_descriptor()
            return bytes(desc) if isinstance(desc, list) else desc
        finally:
            dev.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# VID/PID matching
# ---------------------------------------------------------------------------

_VID_PID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", re.IGNORECASE)


def extract_vid_pid(name: str) -> Optional[Tuple[str, str]]:
    m = _VID_PID_RE.search(name)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    return None


def match_device_name_to_hidapi(
    windows_name: str,
    hidapi_devices: List[dict],
) -> Optional[dict]:
    """
    Match a Windows RawInput device name to a hidapi device dict
    using VID/PID substring match.
    """
    vp = extract_vid_pid(windows_name)
    if vp is None:
        return None
    vid, pid = vp
    for dev in hidapi_devices:
        path = dev.get("path", b"")
        path_str = path.decode("utf-8", errors="replace") if isinstance(path, bytes) else path
        dvp = extract_vid_pid(path_str)
        if dvp and dvp[0] == vid and dvp[1] == pid:
            return dev
    return None


# ---------------------------------------------------------------------------
# Report send / receive
# ---------------------------------------------------------------------------

def _hid_error(dev) -> str:
    try:
        return dev.error() or "unknown error"
    except Exception:
        return "unknown error"


def send_output_report(path, report_id: int, data: List[int]) -> int:
    """Send an Output report via hid.write(). Raises RuntimeError on failure."""
    dev = hid.device()
    dev.open_path(path if isinstance(path, bytes) else path.encode())
    try:
        result = dev.write([report_id] + data)
        if result < 0:
            raise RuntimeError(_hid_error(dev))
        return result
    finally:
        dev.close()


def send_feature_report(path, report_id: int, data: List[int]) -> int:
    """Send a Feature report via hid.send_feature_report(). Raises RuntimeError on failure."""
    dev = hid.device()
    dev.open_path(path if isinstance(path, bytes) else path.encode())
    try:
        result = dev.send_feature_report([report_id] + data)
        if result < 0:
            raise RuntimeError(_hid_error(dev))
        return result
    finally:
        dev.close()


def get_feature_report(path, report_id: int, length: int) -> bytes:
    """Read a Feature report. Returns raw bytes (including report-ID byte)."""
    dev = hid.device()
    dev.open_path(path if isinstance(path, bytes) else path.encode())
    try:
        result = dev.get_feature_report(report_id, length)
        return bytes(result) if result else b""
    finally:
        dev.close()


def set_output_report_cmd(path, report_id: int, data: List[int]) -> None:
    """Send a SET_REPORT(Output) via HidD_SetOutputReport (generates 05 00 26 03 ... on I2C bus)."""
    if sys.platform != "win32":
        raise RuntimeError("set_output_report_cmd 僅支援 Windows")
    h = _open_win_handle(path)
    try:
        buf_data = bytes([report_id] + data)
        buf = (ctypes.c_ubyte * len(buf_data))(*buf_data)
        ok = _hid_dll.HidD_SetOutputReport(h, buf, len(buf))
        if not ok:
            err = ctypes.get_last_error()
            raise RuntimeError(f"HidD_SetOutputReport: ({err:#010x}) {ctypes.FormatError(err)}")
    finally:
        _kernel32.CloseHandle(h)


def _get_input_report_byte_length(h) -> int:
    """取得此 HID collection 的 InputReportByteLength（HidP_GetCaps）。"""
    preparsed = ctypes.c_void_p()
    if not _hid_dll.HidD_GetPreparsedData(h, ctypes.byref(preparsed)):
        return 0
    try:
        caps = _HIDP_CAPS()
        if _hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps)) == _HIDP_STATUS_SUCCESS:
            return caps.InputReportByteLength
        return 0
    finally:
        _hid_dll.HidD_FreePreparsedData(preparsed)


def get_input_report(path, report_id: int, length: int) -> bytes:
    """Read an Input report via HidD_GetInputReport (Windows only).
    Returns raw bytes including the report-ID byte."""
    if sys.platform != "win32":
        raise RuntimeError("HidD_GetInputReport 僅支援 Windows")
    h = _open_win_handle(path)
    try:
        # HidD_GetInputReport 需要 buffer >= collection 最大 Input report 大小
        # 先從 HidP_GetCaps 取得正確大小，取不到就用 4096 保底
        caps_len = _get_input_report_byte_length(h)
        buf_len  = max(length, caps_len if caps_len > 0 else 4096)
        buf = (ctypes.c_ubyte * buf_len)()
        buf[0] = report_id
        ok = _hid_dll.HidD_GetInputReport(h, buf, buf_len)
        if not ok:
            err = ctypes.get_last_error()
            raise RuntimeError(f"HidD_GetInputReport: ({err:#010x}) {ctypes.FormatError(err)}")
        return bytes(buf[:length])
    finally:
        _kernel32.CloseHandle(h)


# ---------------------------------------------------------------------------
# Hex parsing utility
# ---------------------------------------------------------------------------

def parse_hex_bytes(s: str) -> List[int]:
    """Parse a hex string such as 'AA BB CC' or 'AABBCC' into a list of ints."""
    s = s.replace(" ", "").replace(",", "").replace("0x", "").replace("0X", "")
    if not s:
        return []
    if len(s) % 2:
        s = "0" + s
    return [int(s[i:i+2], 16) for i in range(0, len(s), 2)]
