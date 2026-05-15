"""
hidapi device enumeration, descriptor reading, VID/PID matching,
and HID report send/receive helpers.
"""

import re
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:
    hid = None

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
