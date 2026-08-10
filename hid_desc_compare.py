"""
HID Report Descriptor 比對核心。

兩個面向（對應 GUI「Descriptor 比對」視窗的分頁）：

  1. 上報資料格式：把每個 report 展開成逐欄位 layout，兩邊對齊後逐欄位／逐 bit
     比對，標成 OK / INFO（面板參數：logical、physical、unit）/ DIFF（結構：
     usage、size、flags）/ OFFSET（bit 位置錯開）/ 只有單邊。
  2. Descriptor 寫法：以 item 為單位做序列 diff，看得出「同樣的資料格式用不同寫法」
     （例如 3-bit padding 寫成 Size=1 Count=3 或 Size=3 Count=1）。

一側可由多個來源合併（例如同一顆 IC 的 touch screen + stylus 兩份 descriptor），
top-level collection 以 (usage page, usage) 配對，report 先比 Report ID、
ID 對不上時再依內容相似度配對。
"""

import difflib
import os
import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Item / Usage 對照表
# ---------------------------------------------------------------------------

USAGE_PAGES = {
    0x01: "Generic Desktop", 0x02: "Simulation", 0x03: "VR", 0x04: "Sport",
    0x05: "Game", 0x06: "Generic Device", 0x07: "Keyboard", 0x08: "LED",
    0x09: "Button", 0x0A: "Ordinal", 0x0B: "Telephony", 0x0C: "Consumer",
    0x0D: "Digitizer", 0x0E: "Haptics", 0x0F: "PID", 0x10: "Unicode",
    0x20: "Sensor", 0x84: "Power Device", 0x85: "Battery System",
}

MAIN_TAGS = {
    0x08: "Input", 0x09: "Output", 0x0A: "Collection",
    0x0B: "Feature", 0x0C: "End Collection",
}
GLOBAL_TAGS = {
    0x00: "Usage Page", 0x01: "Logical Minimum", 0x02: "Logical Maximum",
    0x03: "Physical Minimum", 0x04: "Physical Maximum",
    0x05: "Unit Exponent", 0x06: "Unit",
    0x07: "Report Size", 0x08: "Report ID", 0x09: "Report Count",
    0x0A: "Push", 0x0B: "Pop",
}
LOCAL_TAGS = {
    0x00: "Usage", 0x01: "Usage Minimum", 0x02: "Usage Maximum",
    0x03: "Designator Index", 0x04: "Designator Minimum",
    0x05: "Designator Maximum", 0x07: "String Index",
    0x08: "String Minimum", 0x09: "String Maximum", 0x0A: "Delimiter",
}
COLLECTION_TYPES = {
    0: "Physical", 1: "Application", 2: "Logical", 3: "Report",
    4: "Named Array", 5: "Usage Switch", 6: "Usage Modifier",
}

# top-level collection 的裝置類型（配對與分頁標題用）
DEVICE_USAGES = {
    (0x0D, 0x01): "Digitizer", (0x0D, 0x02): "Pen", (0x0D, 0x04): "Touch Screen",
    (0x0D, 0x05): "Touch Pad", (0x0D, 0x0E): "Device Config",
    (0x01, 0x01): "Pointer", (0x01, 0x02): "Mouse", (0x01, 0x06): "Keyboard",
    (0x01, 0x80): "System Control",
}

# 欄位名稱：與 hid_descriptor.get_usage_name 同名（監聽表格欄名一致），另補齊
# collection 用的 usage 與較少見的 digitizer 欄位。
USAGE_NAMES = {
    (0x01, 0x01): "Pointer", (0x01, 0x02): "Mouse", (0x01, 0x06): "Keyboard",
    (0x01, 0x30): "X", (0x01, 0x31): "Y", (0x01, 0x32): "Z",
    (0x01, 0x33): "Rx", (0x01, 0x34): "Ry", (0x01, 0x35): "Rz",
    (0x01, 0x36): "Slider", (0x01, 0x37): "Dial", (0x01, 0x38): "Wheel",
    (0x0D, 0x01): "Digitizer", (0x0D, 0x02): "Pen", (0x0D, 0x04): "TouchScreen",
    (0x0D, 0x05): "TouchPad", (0x0D, 0x0E): "DeviceConfiguration",
    (0x0D, 0x20): "Stylus", (0x0D, 0x21): "Puck", (0x0D, 0x22): "Finger",
    (0x0D, 0x23): "DeviceSettings",
    (0x0D, 0x30): "TipPressure", (0x0D, 0x31): "BarrelPressure",
    (0x0D, 0x32): "InRange", (0x0D, 0x33): "Touch", (0x0D, 0x34): "Untouch",
    (0x0D, 0x35): "Tap", (0x0D, 0x37): "DataValid",
    (0x0D, 0x38): "TransducerIndex", (0x0D, 0x3B): "BatteryStrength",
    (0x0D, 0x3C): "Invert", (0x0D, 0x3D): "XTilt", (0x0D, 0x3E): "YTilt",
    (0x0D, 0x3F): "Azimuth", (0x0D, 0x40): "Altitude", (0x0D, 0x41): "Twist",
    (0x0D, 0x42): "TipSwitch", (0x0D, 0x43): "SecTipSwitch",
    (0x0D, 0x44): "BarrelSwitch", (0x0D, 0x45): "Eraser",
    (0x0D, 0x46): "TabletPick", (0x0D, 0x47): "Confidence",
    (0x0D, 0x48): "Width", (0x0D, 0x49): "Height",
    (0x0D, 0x51): "ContactID", (0x0D, 0x52): "DeviceMode",
    (0x0D, 0x53): "DeviceIdentifier", (0x0D, 0x54): "ContactCount",
    (0x0D, 0x55): "ContactCountMax", (0x0D, 0x56): "ScanTime",
    (0x0D, 0x57): "SurfaceSwitch", (0x0D, 0x58): "ButtonSwitch",
    (0x0D, 0x59): "PadType", (0x0D, 0x5A): "SecBarrelSwitch",
    (0x0D, 0x5B): "SerialNumber", (0x0D, 0x5C): "PreferredColor",
    (0x0D, 0x5E): "PreferredLineWidth", (0x0D, 0x6E): "SerialNumberPart2",
    (0x0D, 0x91): "VendorID", (0x0D, 0x92): "ProductID",
}

# 常見 Unit 編碼 → 可讀單位
UNIT_NAMES = {0x00: "none", 0x11: "cm", 0x12: "rad", 0x13: "in", 0x14: "deg",
              0x1001: "s", 0xF0D121: "g"}


def usage_name(page: int, usage: int) -> str:
    """回傳 usage 的可讀名稱；查不到時回 'page:usage' 十六進位。"""
    name = USAGE_NAMES.get((page, usage))
    if name:
        return name
    if page == 0x09:
        return f"Button{usage}"
    if page >= 0xFF00:
        return f"Vendor{usage:02X}"
    return f"{page:02X}:{usage:02X}"


def unit_text(unit: int, exp: int) -> str:
    if unit == 0 and exp == 0:
        return ""
    name = UNIT_NAMES.get(unit, f"0x{unit:X}")
    return f"{name}^{exp}" if exp else name


# ---------------------------------------------------------------------------
# Hex 文字 → bytes
# ---------------------------------------------------------------------------

def clean_hex_string(text: str) -> Optional[bytes]:
    """把 hex 文字轉成 bytes。

    支援純 ``05 0D``、C-style ``0x05, 0x0D,``、大括號／分號，以及 ``//`` 行註解
    與 ``/* */`` 區塊註解。無法解析時回傳 None（與 Focal_ParserTool 同慣例）。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    tokens: List[str] = []
    for line in text.splitlines():
        line = line.split("//")[0]
        for ch in ("0x", "0X", ",", "{", "}", ";", ":"):
            line = line.replace(ch, " ")
        tokens += line.split()
    if not tokens:
        return b""
    try:
        return bytes(int(t, 16) for t in tokens)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass
class DescItem:
    """descriptor 內的一個 short/long item（寫法比對的最小單位）。"""
    offset: int          # 在 descriptor 內的 byte 位置
    prefix: int
    data: bytes
    btype: int           # 0=Main 1=Global 2=Local 3=Reserved/Long
    btag: int
    depth: int           # collection 巢狀深度（顯示縮排用）
    kind: str            # main / global / local / long
    text: str            # 正規化描述（比對用的 key，不含 offset 與 hex）
    col_index: int = -1  # 所屬 top-level collection 索引，-1 表示在 collection 外

    @property
    def hex_bytes(self) -> str:
        return " ".join(f"{b:02X}" for b in bytes([self.prefix]) + self.data)


@dataclass
class Field:
    """report 內的一個欄位實例（Report Count 已展開，一個實例一筆）。"""
    kind: str            # input / output / feature
    report_id: int
    offset: int          # payload 內的 bit 位置
    size: int            # bit 數
    usage_page: int
    usage: int
    usage_min: Optional[int]
    usage_max: Optional[int]
    flags: int
    logical_min: int
    logical_max: int
    physical_min: Optional[int]
    physical_max: Optional[int]
    unit: int
    unit_exp: int
    item_offset: int     # 產生此欄位的 Main item 在 descriptor 內的 byte 位置
    path: Tuple[str, ...] = ()   # collection 巢狀路徑

    @property
    def is_const(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def is_variable(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def is_padding(self) -> bool:
        return self.is_const and self.usage == 0

    def struct_key(self) -> Tuple:
        """對齊用的結構指紋：不含 logical/physical/unit（面板參數不影響 bit 意義）。"""
        return (self.usage_page, self.usage, self.usage_min, self.usage_max,
                self.size, self.flags)

    def bit_key(self) -> Tuple:
        """逐 bit 比對用的完整指紋（含面板參數）。"""
        return self.struct_key() + (self.logical_min, self.logical_max,
                                    self.physical_min, self.physical_max,
                                    self.unit, self.unit_exp)


@dataclass
class Collection:
    """一個 top-level collection。"""
    source: str
    page: int
    usage: int
    index: int
    reports: Dict[str, Dict[int, List[Field]]] = dc_field(default_factory=dict)
    bits: Dict[Tuple[str, int], int] = dc_field(default_factory=dict)
    items: List[DescItem] = dc_field(default_factory=list)

    @property
    def label(self) -> str:
        return DEVICE_USAGES.get((self.page, self.usage)) or \
            f"{usage_name(self.page, self.usage)} ({self.page:02X}:{self.usage:02X})"


@dataclass
class Source:
    """一份 descriptor（一個檔案或一個裝置介面）。"""
    name: str
    raw: bytes
    items: List[DescItem] = dc_field(default_factory=list)
    collections: List[Collection] = dc_field(default_factory=list)
    diagnostics: List[str] = dc_field(default_factory=list)


@dataclass
class Side:
    """比對的一側，可由多份 descriptor 合併。"""
    name: str
    sources: List[Source] = dc_field(default_factory=list)

    @property
    def collections(self) -> List[Collection]:
        return [c for s in self.sources for c in s.collections]

    @property
    def total_bytes(self) -> int:
        return sum(len(s.raw) for s in self.sources)

    @property
    def diagnostics(self) -> List[str]:
        return [f"{s.name}: {d}" for s in self.sources for d in s.diagnostics]


# ---------------------------------------------------------------------------
# 解析：一次走訪同時產生 items 與 top-level collections
# ---------------------------------------------------------------------------

def _signed(value: int, nbytes: int) -> int:
    if nbytes and value >= 1 << (8 * nbytes - 1):
        value -= 1 << (8 * nbytes)
    return value


def flags_text(flags: int) -> str:
    """Input/Output/Feature 的 Main item flags 文字。"""
    parts = ["Const" if flags & 0x01 else "Data",
             "Var" if flags & 0x02 else "Array",
             "Rel" if flags & 0x04 else "Abs"]
    if flags & 0x008: parts.append("Wrap")
    if flags & 0x010: parts.append("NonLinear")
    if flags & 0x020: parts.append("NoPreferred")
    if flags & 0x040: parts.append("Null")
    if flags & 0x080: parts.append("Volatile")
    if flags & 0x100: parts.append("BufferedBytes")
    return ",".join(parts)


def _item_text(btype: int, btag: int, value: int, data_len: int,
               usage_page: int, pending_usage: Optional[Tuple[int, int]]) -> str:
    """單一 item 的正規化描述（寫法比對就是比這串字）。"""
    if btype == 0:      # Main
        name = MAIN_TAGS.get(btag, f"Main(0x{btag:X})")
        if btag == 0x0A:
            ctype = COLLECTION_TYPES.get(value, f"0x{value:02X}")
            if pending_usage:
                return f"Collection ({ctype}: {usage_name(*pending_usage)})"
            return f"Collection ({ctype})"
        if btag in (0x08, 0x09, 0x0B):
            return f"{name} ({flags_text(value)})"
        return name

    if btype == 1:      # Global
        name = GLOBAL_TAGS.get(btag, f"Global(0x{btag:X})")
        if btag == 0x00:
            pg = USAGE_PAGES.get(value, "Vendor" if value >= 0xFF00 else None)
            return f"{name}: 0x{value:04X}" + (f" ({pg})" if pg else "")
        if btag == 0x05:                       # Unit Exponent：4-bit 二補數
            exp = value & 0x0F
            return f"{name}: {exp - 16 if exp & 0x08 else exp}"
        if btag == 0x06:
            u = unit_text(value, 0)
            return f"{name}: 0x{value:X}" + (f" ({u})" if u and u != "none" else "")
        if btag == 0x08:
            return f"{name}: 0x{value:02X}"
        if btag in (0x07, 0x09):               # Report Size / Count：無號
            return f"{name}: {value}"
        if btag in (0x0A, 0x0B):               # Push / Pop
            return name
        return f"{name}: {_signed(value, data_len)}"

    if btype == 2:      # Local
        name = LOCAL_TAGS.get(btag, f"Local(0x{btag:X})")
        if btag in (0x00, 0x01, 0x02):
            page, u = (value >> 16, value & 0xFFFF) if data_len == 4 else (usage_page, value)
            prefix = f"0x{page:02X}:" if data_len == 4 else ""
            return f"{name}: {prefix}0x{u:02X} ({usage_name(page, u)})"
        return f"{name}: 0x{value:X}"

    return f"Reserved(0x{btag:X}): 0x{value:X}"


def parse_source(name: str, raw: bytes) -> Source:
    """解析一份 descriptor，產出 items + top-level collections + 格式診斷。"""
    src = Source(name=name, raw=bytes(raw))
    g = {"usage_page": 0, "report_size": 0, "report_count": 0, "report_id": 0,
         "logical_min": 0, "logical_max": 0, "physical_min": None,
         "physical_max": None, "unit": 0, "unit_exp": 0}
    global_stack: List[dict] = []
    local_usages: List[Tuple[int, int]] = []     # (page, usage)
    usage_min = usage_max = None
    path: List[str] = []
    current: Optional[Collection] = None
    pending_loose: List[DescItem] = []   # 尚未歸屬到 collection 的 item
    depth = 0
    i, n = 0, len(raw)
    MAIN_KINDS = {0x8: "input", 0x9: "output", 0xB: "feature"}

    while i < n:
        prefix = raw[i]

        if prefix == 0xFE:                        # Long item：不解析內容但要正確跳過
            if i + 2 >= n:
                src.diagnostics.append(f"Byte {i}: long item 標頭被截斷")
                break
            long_len = raw[i + 1]
            if i + 3 + long_len > n:
                src.diagnostics.append(f"Byte {i}: long item 資料被截斷")
                break
            src.items.append(DescItem(i, prefix, bytes(raw[i + 1:i + 3 + long_len]),
                                      3, 0, depth, "long", "Long Item",
                                      current.index if current else -1))
            i += 3 + long_len
            continue

        btag = (prefix & 0xF0) >> 4
        btype = (prefix & 0x0C) >> 2
        bsize = prefix & 0x03
        data_len = 4 if bsize == 3 else bsize
        if i + 1 + data_len > n:
            src.diagnostics.append(f"Byte {i}: short item 資料被截斷")
            break
        data = bytes(raw[i + 1:i + 1 + data_len])
        value = int.from_bytes(data, "little") if data_len else 0

        pending = local_usages[0] if local_usages else None
        kind = {0: "main", 1: "global", 2: "local"}.get(btype, "long")
        text = _item_text(btype, btag, value, data_len, g["usage_page"], pending)
        item_depth = depth - 1 if (btype == 0 and btag == 0x0C) else depth
        item = DescItem(i, prefix, data, btype, btag, max(item_depth, 0), kind, text,
                        current.index if current else -1)

        if btype == 0:                            # ---- Main ----
            if btag == 0x0A:                      # Collection
                cpage, cusage = pending if pending else (g["usage_page"], 0)
                if depth == 0:
                    current = Collection(source=name, page=cpage, usage=cusage,
                                         index=len(src.collections))
                    src.collections.append(current)
                    item.col_index = current.index
                    # 緊接在 Collection 前面的 Usage Page / Usage 是這個 collection 的
                    # 開頭寫法，歸到它底下（寫法比對才會從 05 0D 09 04 開始對）
                    head = 0
                    while head < len(pending_loose):
                        it = pending_loose[-(head + 1)]
                        if it.btype == 2 or (it.btype == 1 and it.btag == 0x00):
                            head += 1
                        else:
                            break
                    if head:
                        for it in pending_loose[-head:]:
                            it.col_index = current.index
                        current.items.extend(pending_loose[-head:])
                        del pending_loose[-head:]
                path.append(f"{COLLECTION_TYPES.get(value, value)}:{usage_name(cpage, cusage)}")
                depth += 1
                local_usages, usage_min, usage_max = [], None, None
            elif btag == 0x0C:                    # End Collection
                if depth == 0:
                    src.diagnostics.append(f"Byte {i}: End Collection 沒有對應的 Collection")
                if path:
                    path.pop()
                depth = max(depth - 1, 0)
                if depth == 0 and current is not None:
                    item.col_index = current.index
                    current.items.append(item)
                    src.items.append(item)
                    current = None
                    i += 1 + data_len
                    local_usages, usage_min, usage_max = [], None, None
                    continue
                local_usages, usage_min, usage_max = [], None, None
            elif btag in MAIN_KINDS:              # Input / Output / Feature
                rkind = MAIN_KINDS[btag]
                if current is None:               # 異常保護：main item 出現在 collection 外
                    current = Collection(source=name, page=g["usage_page"], usage=0,
                                         index=len(src.collections))
                    src.collections.append(current)
                    item.col_index = current.index
                    src.diagnostics.append(f"Byte {i}: {rkind} item 不在任何 Collection 內")
                fields = current.reports.setdefault(rkind, {}).setdefault(g["report_id"], [])
                offs = current.bits.get((rkind, g["report_id"]), 0)
                is_var = bool(value & 0x02)
                for c in range(g["report_count"]):
                    if is_var and local_usages:
                        upage, uval = local_usages[c] if c < len(local_usages) else local_usages[-1]
                    elif (is_var and usage_min is not None and usage_max is not None
                          and usage_max >= usage_min):
                        upage, uval = g["usage_page"], min(usage_min + c, usage_max)
                    else:
                        upage, uval = g["usage_page"], 0
                    fields.append(Field(
                        kind=rkind, report_id=g["report_id"], offset=offs,
                        size=g["report_size"], usage_page=upage, usage=uval,
                        usage_min=usage_min, usage_max=usage_max, flags=value,
                        logical_min=g["logical_min"], logical_max=g["logical_max"],
                        physical_min=g["physical_min"], physical_max=g["physical_max"],
                        unit=g["unit"], unit_exp=g["unit_exp"], item_offset=i,
                        path=tuple(path),
                    ))
                    offs += g["report_size"]
                current.bits[(rkind, g["report_id"])] = offs
                local_usages, usage_min, usage_max = [], None, None
            else:
                local_usages, usage_min, usage_max = [], None, None

        elif btype == 1:                          # ---- Global ----
            if   btag == 0x0: g["usage_page"]   = value
            elif btag == 0x1: g["logical_min"]  = _signed(value, data_len)
            elif btag == 0x2: g["logical_max"]  = _signed(value, data_len)
            elif btag == 0x3: g["physical_min"] = _signed(value, data_len)
            elif btag == 0x4: g["physical_max"] = _signed(value, data_len)
            elif btag == 0x5:
                exp = value & 0x0F
                g["unit_exp"] = exp - 16 if exp & 0x08 else exp
            elif btag == 0x6: g["unit"] = value
            elif btag == 0x7: g["report_size"] = value
            elif btag == 0x9: g["report_count"] = value
            elif btag == 0x8:
                if value == 0:
                    src.diagnostics.append(f"Byte {i}: Report ID 0 不合法")
                g["report_id"] = value
            elif btag == 0xA: global_stack.append(g.copy())
            elif btag == 0xB:
                if global_stack:
                    g = global_stack.pop()
                else:
                    src.diagnostics.append(f"Byte {i}: Pop 沒有對應的 Push")

        elif btype == 2:                          # ---- Local ----
            if btag == 0x0:
                if data_len == 4:
                    local_usages.append((value >> 16, value & 0xFFFF))
                else:
                    local_usages.append((g["usage_page"], value))
            elif btag == 0x1: usage_min = value & 0xFFFF
            elif btag == 0x2: usage_max = value & 0xFFFF

        src.items.append(item)
        if current is not None:
            current.items.append(item)
        else:
            pending_loose.append(item)
        i += 1 + data_len

    if depth:
        src.diagnostics.append(f"descriptor 結束時仍有 {depth} 個未關閉的 Collection")
    if global_stack:
        src.diagnostics.append(f"descriptor 結束時仍有 {len(global_stack)} 個未 Pop 的 Push")
    return src


def make_side(name: str, sources: Sequence[Tuple[str, bytes]]) -> Side:
    return Side(name=name, sources=[parse_source(n, b) for n, b in sources])


# ---------------------------------------------------------------------------
# 欄位描述與比對
# ---------------------------------------------------------------------------

def normalize_fields(fields: Sequence[Field]) -> List[Field]:
    """合併相鄰的 Constant padding。

    3-bit padding 可寫成 Size=1/Count=3 或 Size=3/Count=1，資料格式等價，
    不合併會讓後續欄位索引錯開而整片誤判 DIFF。
    """
    out: List[Field] = []
    for f in fields:
        prev = out[-1] if out else None
        if (f.is_padding and prev is not None and prev.is_padding
                and prev.offset + prev.size == f.offset and prev.flags == f.flags):
            merged = Field(**{**prev.__dict__, "size": prev.size + f.size})
            out[-1] = merged
        else:
            out.append(f)
    return out


def _range_text(lo: Optional[int], hi: Optional[int]) -> str:
    """Physical 範圍字串；descriptor 沒宣告時顯示「未宣告」而不是 0。"""
    if lo is None and hi is None:
        return "未宣告"
    return f"[{lo if lo is not None else 0}..{hi if hi is not None else 0}]"


def field_str(f: Optional[Field]) -> str:
    """欄位的一行摘要：名稱 / usage / bit 範圍 / 範圍 / flags。"""
    if f is None:
        return "—"
    if f.is_padding:
        core = "(padding)"
    elif not f.is_variable and f.usage_min is not None:
        core = f"Array {f.usage_page:02X}:{f.usage_min:02X}-{f.usage_max:02X}"
    else:
        core = f"{usage_name(f.usage_page, f.usage)} [{f.usage_page:02X}:{f.usage:02X}]"
    s = f"{core} {f.size}b L[{f.logical_min}..{f.logical_max}]"
    if f.physical_min is not None or f.physical_max is not None:
        pmin = f.physical_min if f.physical_min is not None else 0
        pmax = f.physical_max if f.physical_max is not None else 0
        if (pmin, pmax) != (0, 0):
            s += f" P[{pmin}..{pmax}]"
    u = unit_text(f.unit, f.unit_exp)
    if u and u != "none":
        s += f" {u}"
    return s + f" {flags_text(f.flags)}"


def bit_span(f: Optional[Field], with_report_id: bool = False) -> str:
    """bit 位置字串。with_report_id=True 時換算成含 Report ID 的實際封包位置。"""
    if f is None:
        return "—"
    base = f.offset + (8 if with_report_id and f.report_id else 0)
    end = base + f.size - 1
    if f.size == 1:
        return f"{base // 8}.{base % 8}"
    return f"{base // 8}.{base % 8}-{end // 8}.{end % 8}"


def compare_field_details(a: Optional[Field], b: Optional[Field]) -> Tuple[str, str]:
    """回傳 (status, reason)。

    status：OK / INFO（面板參數）/ OFFSET（bit 位置錯開）/ DIFF（結構）/ LEN（單邊缺）。
    """
    if a is None or b is None:
        return "LEN", "只有 " + ("B" if a is None else "A") + " 有此欄位"
    structural = []
    if (a.usage_page, a.usage, a.usage_min, a.usage_max) != \
       (b.usage_page, b.usage, b.usage_min, b.usage_max):
        structural.append(f"Usage {a.usage_page:02X}:{a.usage:02X}→{b.usage_page:02X}:{b.usage:02X}")
    if a.size != b.size:
        structural.append(f"Size {a.size}→{b.size}")
    if a.flags != b.flags:
        structural.append(f"Flags {flags_text(a.flags)}→{flags_text(b.flags)}")
    if structural:
        return "DIFF", "、".join(structural)
    if a.offset != b.offset:
        return "OFFSET", f"bit 位置 {a.offset}→{b.offset}"
    # 結構相同、只有數值範圍不同 → 面板參數，標 INFO 不算 NG
    params = []
    if (a.logical_min, a.logical_max) != (b.logical_min, b.logical_max):
        params.append(f"Logical [{a.logical_min}..{a.logical_max}]→[{b.logical_min}..{b.logical_max}]")
    if (a.physical_min, a.physical_max) != (b.physical_min, b.physical_max):
        params.append(f"Physical {_range_text(a.physical_min, a.physical_max)}"
                      f"→{_range_text(b.physical_min, b.physical_max)}")
    if (a.unit, a.unit_exp) != (b.unit, b.unit_exp):
        params.append(f"Unit {unit_text(a.unit, a.unit_exp) or '-'}→{unit_text(b.unit, b.unit_exp) or '-'}")
    if params:
        return "INFO", "、".join(params)
    return "OK", ""


# ---------------------------------------------------------------------------
# 逐 bit 比對
# ---------------------------------------------------------------------------

def bit_signatures(fields: Sequence[Field], total_bits: int) -> List[Optional[Tuple]]:
    """展開成長度 total_bits 的陣列，每個 bit 記 (結構指紋, 完整指紋, 在欄位內的第幾個 bit)。"""
    sigs: List[Optional[Tuple]] = [None] * total_bits
    for f in fields:
        skey, bkey = f.struct_key(), f.bit_key()
        for k in range(f.size):
            idx = f.offset + k
            if 0 <= idx < total_bits:
                sigs[idx] = (skey, bkey, k)
    return sigs


@dataclass
class BitStat:
    """逐 bit 統計：同一 bit 位置上的欄位屬性與 bit 序號完全一致才算 same。"""
    same: int = 0
    info: int = 0      # 結構相同、只有 logical/physical/unit 不同
    diff: int = 0      # usage / size / flags 不同
    only_a: int = 0
    only_b: int = 0

    @property
    def total(self) -> int:
        return self.same + self.info + self.diff + self.only_a + self.only_b

    @property
    def is_clean(self) -> bool:
        return self.diff == 0 and self.only_a == 0 and self.only_b == 0

    def text(self) -> str:
        parts = [f"相同 {self.same}"]
        if self.info:
            parts.append(f"參數不同 {self.info}")
        if self.diff:
            parts.append(f"結構不同 {self.diff}")
        if self.only_a:
            parts.append(f"只有 A {self.only_a}")
        if self.only_b:
            parts.append(f"只有 B {self.only_b}")
        return " / ".join(parts) + f"（共 {self.total} bits）"


def compare_bits(a_fields: Sequence[Field], a_bits: int,
                 b_fields: Sequence[Field], b_bits: int) -> BitStat:
    """真正逐 bit 比對，回傳每個 bit 的判定統計。"""
    sa = bit_signatures(a_fields, a_bits)
    sb = bit_signatures(b_fields, b_bits)
    st = BitStat()
    for i in range(max(a_bits, b_bits)):
        va = sa[i] if i < a_bits else None
        vb = sb[i] if i < b_bits else None
        if va is None and vb is None:
            continue
        if va is None:
            st.only_b += 1
        elif vb is None:
            st.only_a += 1
        elif va == vb:
            st.same += 1
        elif (va[0], va[2]) == (vb[0], vb[2]):
            st.info += 1
        else:
            st.diff += 1
    return st


# ---------------------------------------------------------------------------
# report / collection 配對
# ---------------------------------------------------------------------------

def _seq_ratio(a: Sequence[Field], b: Sequence[Field]) -> float:
    ka = [str(f.struct_key()) for f in a]
    kb = [str(f.struct_key()) for f in b]
    if not ka or not kb:
        return 0.0
    return difflib.SequenceMatcher(None, ka, kb, autojunk=False).ratio()


def pair_report_ids(a_reports: Dict[int, List[Field]],
                    b_reports: Dict[int, List[Field]]) -> List[Tuple[Optional[int], Optional[int]]]:
    """先配對相同的 Report ID，剩下的依內容相似度貪婪配對（ID 換號也能對上）。"""
    pairs: List[Tuple[Optional[int], Optional[int]]] = []
    common = sorted(set(a_reports) & set(b_reports))
    pairs += [(rid, rid) for rid in common]

    rest_a = sorted(set(a_reports) - set(common))
    rest_b = sorted(set(b_reports) - set(common))
    cands = sorted(
        ((_seq_ratio(a_reports[x], b_reports[y]), x, y) for x in rest_a for y in rest_b),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_a, used_b = set(), set()
    for ratio, x, y in cands:
        if ratio <= 0 or x in used_a or y in used_b:
            continue
        pairs.append((x, y))
        used_a.add(x)
        used_b.add(y)
    pairs += [(x, None) for x in rest_a if x not in used_a]
    pairs += [(None, y) for y in rest_b if y not in used_b]
    pairs.sort(key=lambda p: (p[0] if p[0] is not None else 0xFFFF,
                              p[1] if p[1] is not None else 0xFFFF))
    return pairs


def pair_collections(a_cols: Sequence[Collection], b_cols: Sequence[Collection]):
    """以 (usage page, usage) 配對 top-level collection，同 key 依出現順序配對。"""
    def group(cols):
        m: Dict[Tuple[int, int], List[Collection]] = {}
        for c in cols:
            m.setdefault((c.page, c.usage), []).append(c)
        return m

    amap, bmap = group(a_cols), group(b_cols)
    keys: List[Tuple[int, int]] = []
    for c in list(a_cols) + list(b_cols):
        if (c.page, c.usage) not in keys:
            keys.append((c.page, c.usage))
    out = []
    for k in keys:
        al, bl = amap.get(k, []), bmap.get(k, [])
        for idx in range(max(len(al), len(bl))):
            out.append((k, al[idx] if idx < len(al) else None,
                        bl[idx] if idx < len(bl) else None))
    return out


# ---------------------------------------------------------------------------
# 比對結果
# ---------------------------------------------------------------------------

@dataclass
class FieldRow:
    a: Optional[Field]
    b: Optional[Field]
    status: str
    reason: str


@dataclass
class ReportGroup:
    kind: str
    rid_a: Optional[int]
    rid_b: Optional[int]
    bits_a: int
    bits_b: int
    rows: List[FieldRow]
    bitstat: BitStat

    @property
    def label(self) -> str:
        def rid(v):
            return "—" if v is None else f"0x{v:02X}"
        ids = rid(self.rid_a) if self.rid_a == self.rid_b else f"A {rid(self.rid_a)} / B {rid(self.rid_b)}"
        return f"{self.kind.title()} Report {ids}"

    def wire_text(self) -> str:
        pa, wa = report_length(self.bits_a, self.rid_a or 0)
        pb, wb = report_length(self.bits_b, self.rid_b or 0)
        same = "" if wa == wb else "  ← 封包長度不同"
        return f"A {self.bits_a} bits / {wa}B　B {self.bits_b} bits / {wb}B{same}"


@dataclass
class ItemRow:
    op: str                      # equal / replace / delete(只有A) / insert(只有B)
    a: Optional[DescItem]
    b: Optional[DescItem]


@dataclass
class CollectionPair:
    key: Tuple[int, int]
    label: str
    a: Optional[Collection]
    b: Optional[Collection]
    groups: List[ReportGroup]
    item_rows: List[ItemRow]

    @property
    def n_diff(self) -> int:
        return sum(1 for g in self.groups for r in g.rows if r.status in ("DIFF", "LEN", "OFFSET"))

    @property
    def n_info(self) -> int:
        return sum(1 for g in self.groups for r in g.rows if r.status == "INFO")

    @property
    def n_item_diff(self) -> int:
        return sum(1 for r in self.item_rows if r.op != "equal")


@dataclass
class CompareResult:
    side_a: Side
    side_b: Side
    pairs: List[CollectionPair]
    loose_items: List[ItemRow]          # 不屬於任何 top-level collection 的 item

    def count(self, *status: str) -> int:
        return sum(1 for p in self.pairs for g in p.groups
                   for r in g.rows if r.status in status)

    @property
    def n_diff(self) -> int:
        return sum(p.n_diff for p in self.pairs) + \
            sum(1 for p in self.pairs if p.a is None or p.b is None)

    @property
    def n_info(self) -> int:
        return sum(p.n_info for p in self.pairs)

    @property
    def bitstat(self) -> BitStat:
        total = BitStat()
        for p in self.pairs:
            for g in p.groups:
                total.same   += g.bitstat.same
                total.info   += g.bitstat.info
                total.diff   += g.bitstat.diff
                total.only_a += g.bitstat.only_a
                total.only_b += g.bitstat.only_b
        return total

    @property
    def identical_bytes(self) -> bool:
        return (b"".join(s.raw for s in self.side_a.sources) ==
                b"".join(s.raw for s in self.side_b.sources))


def report_length(bits: int, report_id: int) -> Tuple[int, int]:
    """回傳 (payload bytes, wire bytes)；wire 在 Report ID != 0 時含 ID 那個 byte。"""
    payload = (bits + 7) // 8
    return payload, payload + (1 if report_id else 0)


def _align_fields(a_fields: Sequence[Field], b_fields: Sequence[Field]) -> List[FieldRow]:
    """用結構指紋做序列對齊，中間插入／刪除欄位時後面仍能正確對上。"""
    ka = [str(f.struct_key()) for f in a_fields]
    kb = [str(f.struct_key()) for f in b_fields]
    rows: List[FieldRow] = []
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                a, b = a_fields[i1 + k], b_fields[j1 + k]
                st, why = compare_field_details(a, b)
                rows.append(FieldRow(a, b, st, why))
        else:
            la, lb = i2 - i1, j2 - j1
            for k in range(max(la, lb)):
                a = a_fields[i1 + k] if k < la else None
                b = b_fields[j1 + k] if k < lb else None
                st, why = compare_field_details(a, b)
                rows.append(FieldRow(a, b, st, why))
    return rows


def _merge_reports(cols: Sequence[Collection], kind: str) -> Dict[int, List[Field]]:
    out: Dict[int, List[Field]] = {}
    for c in cols:
        for rid, fields in c.reports.get(kind, {}).items():
            out.setdefault(rid, []).extend(fields)
    return out


def _diff_items(a_items: Sequence[DescItem], b_items: Sequence[DescItem]) -> List[ItemRow]:
    ka = [it.text for it in a_items]
    kb = [it.text for it in b_items]
    rows: List[ItemRow] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                rows.append(ItemRow("equal", a_items[i1 + k], b_items[j1 + k]))
        elif op == "replace":
            la, lb = i2 - i1, j2 - j1
            for k in range(max(la, lb)):
                rows.append(ItemRow(
                    "replace",
                    a_items[i1 + k] if k < la else None,
                    b_items[j1 + k] if k < lb else None,
                ))
        elif op == "delete":
            for k in range(i1, i2):
                rows.append(ItemRow("delete", a_items[k], None))
        else:   # insert
            for k in range(j1, j2):
                rows.append(ItemRow("insert", None, b_items[k]))
    return rows


def compare_sides(side_a: Side, side_b: Side) -> CompareResult:
    """完整比對：collection 配對 → report 配對 → 欄位對齊 → 逐 bit 統計 + 寫法 diff。"""
    pairs: List[CollectionPair] = []
    for key, acol, bcol in pair_collections(side_a.collections, side_b.collections):
        groups: List[ReportGroup] = []
        for kind in ("input", "output", "feature"):
            areps = _merge_reports([acol] if acol else [], kind)
            breps = _merge_reports([bcol] if bcol else [], kind)
            for rid_a, rid_b in pair_report_ids(areps, breps):
                af = normalize_fields(areps.get(rid_a, []) if rid_a is not None else [])
                bf = normalize_fields(breps.get(rid_b, []) if rid_b is not None else [])
                bits_a = acol.bits.get((kind, rid_a), 0) if acol and rid_a is not None else 0
                bits_b = bcol.bits.get((kind, rid_b), 0) if bcol and rid_b is not None else 0
                groups.append(ReportGroup(
                    kind=kind, rid_a=rid_a, rid_b=rid_b,
                    bits_a=bits_a, bits_b=bits_b,
                    rows=_align_fields(af, bf),
                    bitstat=compare_bits(af, bits_a, bf, bits_b),
                ))
        label = (acol or bcol).label if (acol or bcol) else f"{key[0]:02X}:{key[1]:02X}"
        pairs.append(CollectionPair(
            key=key, label=label, a=acol, b=bcol, groups=groups,
            item_rows=_diff_items(acol.items if acol else [], bcol.items if bcol else []),
        ))

    loose_a = [it for s in side_a.sources for it in s.items if it.col_index < 0]
    loose_b = [it for s in side_b.sources for it in s.items if it.col_index < 0]
    loose = _diff_items(loose_a, loose_b) if (loose_a or loose_b) else []
    return CompareResult(side_a=side_a, side_b=side_b, pairs=pairs, loose_items=loose)


# ---------------------------------------------------------------------------
# Base profile（bases/ 資料夾裡的目標 descriptor）
# ---------------------------------------------------------------------------

# 依裝置類型自動挑 profile 的順序：先看有沒有筆
_PROFILE_BY_DEVICE = (
    ("Pen", "touchscreen_pen"),
    ("Touch Screen", "touchscreen"),
    ("Touch Pad", "touchpad"),
)


def find_profiles(dirs: Sequence[str]) -> Dict[str, List[str]]:
    """掃描 base 資料夾，回傳 profile 名稱 -> 檔案清單。

    ``<name>.txt``  = 單檔 profile；
    ``<name>/*.txt`` = 多檔 profile（同一顆 IC 的多個 top-level collection，
    例如 touch screen + stylus 分兩個檔）。前面的目錄優先。
    """
    def hex_files(folder: str) -> List[str]:
        # 只收真的解得出 hex 的檔案，說明文字／筆記放在同一層也不會被當成 base
        out = []
        for f in sorted(os.listdir(folder)):
            full = os.path.join(folder, f)
            if not f.lower().endswith(".txt") or not os.path.isfile(full):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    if clean_hex_string(fh.read()):
                        out.append(full)
            except OSError:
                pass
        return out

    found: Dict[str, List[str]] = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            full = os.path.join(d, entry)
            if os.path.isdir(full):
                files = hex_files(full)
                if files:
                    found.setdefault(entry, files)
        for path in hex_files(d):
            found.setdefault(os.path.splitext(os.path.basename(path))[0], [path])
    return found


def load_profile(paths: Sequence[str]) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """讀取 profile 的所有檔案，回傳 ((顯示名稱, bytes) 清單, 錯誤訊息清單)。"""
    sources: List[Tuple[str, bytes]] = []
    errors: List[str] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = clean_hex_string(fh.read())
        except OSError as e:
            errors.append(f"{os.path.basename(path)}: {e}")
            continue
        if not raw:
            errors.append(f"{os.path.basename(path)}: 沒有可解析的 hex 內容")
            continue
        sources.append((os.path.basename(path), raw))
    return sources, errors


def detect_device_types(collections: Sequence[Collection]) -> List[str]:
    """依 top-level collection 判斷裝置類型（Touch Screen / Pen / Touch Pad …）。"""
    found: List[str] = []
    for c in collections:
        name = DEVICE_USAGES.get((c.page, c.usage))
        if name and name not in found:
            found.append(name)
    return found


def auto_profile(device_types: Sequence[str], available: Sequence[str]) -> Optional[str]:
    """依偵測到的裝置類型挑一個存在的 profile；挑不到回傳 None。"""
    for dev_type, stem in _PROFILE_BY_DEVICE:
        if dev_type in device_types and stem in available:
            return stem
    return None


# ---------------------------------------------------------------------------
# 文字報告（匯出 / CLI）
# ---------------------------------------------------------------------------

def format_text_report(res: CompareResult, only_diff: bool = False) -> str:
    L: List[str] = []
    add = L.append

    add("=== 0. 來源 ===")
    for side, tag in ((res.side_a, "A"), (res.side_b, "B")):
        add(f"[{tag}] {side.name}")
        for s in side.sources:
            add(f"      - {s.name}  {len(s.raw)} bytes  "
                f"collection: {', '.join(c.label for c in s.collections) or '(無)'}")
        for d in side.diagnostics:
            add(f"      ! {d}")
    add("")

    st = res.bitstat
    add("=== 1. 總結 ===")
    add(f"Binary        : {'完全相同' if res.identical_bytes else '不同'}"
        f"（A {res.side_a.total_bytes} bytes / B {res.side_b.total_bytes} bytes）")
    add(f"逐 bit 比對   : {st.text()}")
    add(f"欄位 DIFF     : {res.count('DIFF')}（usage / size / flags 不同）")
    add(f"欄位 OFFSET   : {res.count('OFFSET')}（結構相同但 bit 位置錯開）")
    add(f"欄位 LEN      : {res.count('LEN')}（只有單邊有的欄位）")
    add(f"欄位 INFO     : {res.count('INFO')}（logical / physical / unit 等面板參數）")
    only_col = [p.label for p in res.pairs if p.a is None or p.b is None]
    if only_col:
        add(f"單邊 collection: {', '.join(only_col)}")
    add(f"寫法差異項目  : {sum(p.n_item_diff for p in res.pairs)} / "
        f"{sum(len(p.item_rows) for p in res.pairs)}")
    add("")

    add("=== 2. 上報資料格式（逐欄位 / 逐 bit）===")
    for p in res.pairs:
        add("")
        add(f"--- [{p.label}] ---")
        if p.a is None:
            add("    只有 B 有此 collection")
        elif p.b is None:
            add("    只有 A 有此 collection")
        for g in p.groups:
            add(f"  {g.label}   {g.wire_text()}")
            add(f"    逐 bit：{g.bitstat.text()}")
            add(f"    {'A bit':<12} {'A 欄位':<58} {'B bit':<12} {'B 欄位':<58} 狀態")
            for r in g.rows:
                if only_diff and r.status == "OK":
                    continue
                add(f"    {bit_span(r.a):<12} {field_str(r.a):<58} "
                    f"{bit_span(r.b):<12} {field_str(r.b):<58} "
                    f"{r.status}{(' (' + r.reason + ')') if r.reason else ''}")
    add("")

    add("=== 3. Descriptor 寫法（item 差異）===")
    sections = [(p.label, p.item_rows) for p in res.pairs]
    if res.loose_items:
        sections.append(("(collection 外)", res.loose_items))
    for label, rows in sections:
        ndiff = sum(1 for r in rows if r.op != "equal")
        add("")
        add(f"--- [{label}] 差異 {ndiff} / 共 {len(rows)} 項 ---")
        for r in rows:
            if only_diff and r.op == "equal":
                continue
            mark = {"equal": "  ", "replace": "~ ", "delete": "- ", "insert": "+ "}[r.op]
            a_txt = f"{r.a.offset:04X} {r.a.hex_bytes:<14} {'  ' * r.a.depth}{r.a.text}" if r.a else ""
            b_txt = f"{r.b.offset:04X} {r.b.hex_bytes:<14} {'  ' * r.b.depth}{r.b.text}" if r.b else ""
            add(f"{mark}{a_txt:<70} | {b_txt}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI：python hid_desc_compare.py A.txt[,A2.txt] B.txt[,B2.txt]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(__doc__)
        print("用法: python hid_desc_compare.py A.txt[,A2.txt] B.txt[,B2.txt] [--only-diff]")
        raise SystemExit(1)

    def load(spec: str) -> List[Tuple[str, bytes]]:
        out = []
        for path in spec.split(","):
            with open(path, encoding="utf-8", errors="replace") as fh:
                data = clean_hex_string(fh.read())
            if data is None:
                raise SystemExit(f"hex 解析失敗: {path}")
            out.append((os.path.basename(path), data))
        return out

    a = make_side("A", load(sys.argv[1]))
    b = make_side("B", load(sys.argv[2]))
    print(format_text_report(compare_sides(a, b), only_diff="--only-diff" in sys.argv))
