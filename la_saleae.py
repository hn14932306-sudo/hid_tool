"""
Saleae Logic 2 I2C analyzer CSV import -> I2C-HID exchange reconstruction.

Input: CSV exported from Logic 2's I2C analyzer via
"Export Data Table" (columns: name,type,start_time,duration,ack,address,read,data).

This module only understands the I2C bus framing (start/address/data/stop) and the
HID-over-I2C register conventions (HID Descriptor / Input / Output / Command / Data
registers, length-prefixed reads). It does not depend on Tk or the rest of hid_tool.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
from typing import List, Optional


@dataclass
class I2CPhase:
    read: bool
    address: int
    data: bytes
    addr_ack: bool = True          # 位址 byte 本身有沒有被 ACK；False = 這個位址上沒有裝置回應
    acks: List[bool] = field(default_factory=list)


@dataclass
class I2CExchange:
    """One I2C bus transfer: start ... (address+data)+ ... stop.
    A repeated start shows up as more than one phase."""
    time: str
    phases: List[I2CPhase]


@dataclass
class I2CHidEvent:
    time: str
    address: int
    kind: str                      # "input_report" | "register_read" | "register_write" | "register_select" | "other"
    register: Optional[int]
    raw: bytes                     # bytes of the read/write data portion (register prefix stripped for writes)
    length_field: Optional[int]    # little-endian 2-byte length prefix, if present (read kinds only)
    payload: bytes                 # raw with the 2-byte length prefix stripped, best-effort


def _parse_bool(s: Optional[str]) -> bool:
    return (s or "").strip().lower() == "true"


def _parse_hex(s: Optional[str]) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None


def _le16(b: bytes, offset: int = 0) -> Optional[int]:
    if len(b) < offset + 2:
        return None
    return b[offset] | (b[offset + 1] << 8)


def _split_length_prefixed(raw: bytes):
    """HID-over-I2C reads start with a little-endian wLength (self-inclusive) prefix.
    Some captures keep reading well past the real frame (bus idles at 0), so trust
    wLength over the captured byte count when it's plausible."""
    length_field = _le16(raw)
    if length_field is None:
        return None, raw
    if 2 <= length_field <= len(raw):
        return length_field, raw[2:length_field]
    return length_field, raw[2:]


def parse_i2c_csv(path: str) -> List[I2CExchange]:
    """Group Saleae's flat start/address/data/stop rows into per-transfer exchanges."""
    exchanges: List[I2CExchange] = []
    cur_phases: List[dict] = []
    cur_phase: Optional[dict] = None
    cur_time: Optional[str] = None

    def flush():
        nonlocal cur_phases, cur_phase, cur_time
        if cur_phases:
            exchanges.append(I2CExchange(
                time=cur_time or "",
                phases=[I2CPhase(read=p["read"], address=p["address"], data=bytes(p["data"]),
                                addr_ack=p["addr_ack"], acks=p["acks"])
                        for p in cur_phases],
            ))
        cur_phases = []
        cur_phase = None
        cur_time = None

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = (row.get("type") or "").strip()
            if t == "start":
                if cur_time is None:
                    cur_time = row.get("start_time", "")
            elif t == "address":
                addr = _parse_hex(row.get("address")) or 0
                cur_phase = {"read": _parse_bool(row.get("read")), "address": addr,
                             "addr_ack": _parse_bool(row.get("ack")),
                             "data": bytearray(), "acks": []}
                cur_phases.append(cur_phase)
                if cur_time is None:
                    cur_time = row.get("start_time", "")
            elif t == "data":
                if cur_phase is not None:
                    b = _parse_hex(row.get("data"))
                    if b is not None:
                        cur_phase["data"].append(b & 0xFF)
                    cur_phase["acks"].append(_parse_bool(row.get("ack")))
            elif t == "stop":
                flush()
    flush()
    return exchanges


def classify_exchange(ex: I2CExchange) -> I2CHidEvent:
    """Map one I2C exchange onto the I2C-HID register conventions:
    - address byte NACK'd                     -> "no_ack" (沒有裝置在這個位址回應，跟真正的
                                                  0-byte 交易不一樣，通常是開機前的位址探測)
    - single read phase                       -> bare "input_report" read (implicit Input Register)
    - single write phase, 2 bytes only        -> "register_select" (register pointer written, no data)
    - single write phase, >2 bytes            -> "register_write" (register + payload, e.g. Output/Command)
    - write(2B register) + repeated-start read -> "register_read" (length-prefixed response)
    - anything else                           -> "other"
    """
    phases = ex.phases
    if not phases:
        return I2CHidEvent(ex.time, 0, "other", None, b"", None, b"")
    address = phases[0].address

    if any(not p.addr_ack for p in phases):
        raw = b"".join(p.data for p in phases)
        return I2CHidEvent(ex.time, address, "no_ack", None, raw, None, raw)

    if len(phases) == 1:
        p = phases[0]
        if p.read:
            length_field, payload = _split_length_prefixed(p.data)
            return I2CHidEvent(ex.time, address, "input_report", None, p.data, length_field, payload)
        if len(p.data) > 2:
            register = _le16(p.data)
            data_part = bytes(p.data[2:])
            return I2CHidEvent(ex.time, address, "register_write", register, data_part, None, data_part)
        if len(p.data) == 2:
            register = _le16(p.data)
            return I2CHidEvent(ex.time, address, "register_select", register, b"", None, b"")
        return I2CHidEvent(ex.time, address, "other", None, bytes(p.data), None, bytes(p.data))

    if len(phases) == 2 and not phases[0].read and phases[1].read:
        register = _le16(phases[0].data)
        raw = phases[1].data
        length_field, payload = _split_length_prefixed(raw)
        return I2CHidEvent(ex.time, address, "register_read", register, raw, length_field, payload)

    raw = b"".join(p.data for p in phases)
    return I2CHidEvent(ex.time, address, "other", None, raw, None, raw)


# Byte offsets inside the 30-byte HID-over-I2C descriptor response (little-endian u16 each,
# see Microsoft's "HID over I2C" protocol spec).
_HID_DESC_FIELDS = (
    ("wHIDDescLength", 0), ("bcdVersion", 2), ("wReportDescLength", 4),
    ("wReportDescRegister", 6), ("wInputRegister", 8), ("wMaxInputLength", 10),
    ("wOutputRegister", 12), ("wMaxOutputLength", 14), ("wCommandRegister", 16),
    ("wDataRegister", 18), ("wVendorID", 20), ("wProductID", 22), ("wVersionID", 24),
)


def parse_hid_descriptor_regmap(raw: bytes) -> Optional[dict]:
    if len(raw) < 20:
        return None
    return {name: _le16(raw, off) for name, off in _HID_DESC_FIELDS if _le16(raw, off) is not None}


def find_hid_descriptor_regmap(events: List[I2CHidEvent]) -> Optional[dict]:
    """Scan register_read events for one whose response looks like the HID descriptor:
    a self-consistent wHIDDescLength in the plausible range, with enough captured bytes.
    We don't assume any fixed register number -- it's device/ACPI-specific."""
    for ev in events:
        if ev.kind != "register_read" or len(ev.raw) < 20:
            continue
        n = _le16(ev.raw)
        if n is None or not (20 <= n <= 32) or len(ev.raw) < n:
            continue
        regmap = parse_hid_descriptor_regmap(ev.raw)
        if regmap:
            regmap["_source_register"] = ev.register
            return regmap
    return None


def find_report_descriptor_bytes(events: List[I2CHidEvent], regmap: dict) -> Optional[bytes]:
    """If the capture also includes a read of wReportDescRegister, pull out the raw
    HID report descriptor bytes (wReportDescLength of them, no length prefix)."""
    reg = regmap.get("wReportDescRegister")
    n = regmap.get("wReportDescLength")
    if reg is None or not n:
        return None
    for ev in events:
        if ev.kind == "register_read" and ev.register == reg and len(ev.raw) >= n:
            return bytes(ev.raw[:n])
    return None


@dataclass
class LAAnalysis:
    events: List[I2CHidEvent]
    regmap: Optional[dict]
    report_descriptor: Optional[bytes]


def analyze_capture(path: str) -> LAAnalysis:
    exchanges = parse_i2c_csv(path)
    events = [classify_exchange(ex) for ex in exchanges]
    regmap = find_hid_descriptor_regmap(events)
    report_descriptor = find_report_descriptor_bytes(events, regmap) if regmap else None
    if regmap is not None:
        events = _fix_report_desc_length(events, regmap)
    return LAAnalysis(events=events, regmap=regmap, report_descriptor=report_descriptor)


def _fix_report_desc_length(events: List[I2CHidEvent], regmap: dict) -> List[I2CHidEvent]:
    """wReportDescRegister 的讀回不是 length-prefixed（它前 2 bytes 就是 descriptor
    本體，不是自含長度），classify_exchange 當下還不知道 regmap，會把那 2 bytes 誤讀
    成一個看起來很大的假長度（例如 0x0D05=3333）。這裡等 regmap 抓出來後回頭修正，
    避免使用者在表格裡看到一個沒有意義的長度數字。"""
    reg = regmap.get("wReportDescRegister")
    if reg is None:
        return events
    out = []
    for ev in events:
        if ev.kind == "register_read" and ev.register == reg:
            ev = replace(ev, length_field=None, payload=ev.raw)
        out.append(ev)
    return out


def is_input_report_event(ev: I2CHidEvent, regmap: Optional[dict]) -> bool:
    if ev.kind == "input_report":
        return True
    if ev.kind == "register_read" and regmap is not None:
        return ev.register == regmap.get("wInputRegister")
    return False


# ---------------------------------------------------------------------------
# Command Register decoding (Microsoft "HID over I2C" device-command format)
# ---------------------------------------------------------------------------
#
# Write to the Command Register:
#   byte0   : bits[7:4]=Report Type (0=Reserved,1=Input,2=Output,3=Feature)
#             bits[3:0]=Report ID, or 0xF as a sentinel meaning "ID follows in byte2"
#   byte1   : opcode
#   [byte2] : extended Report ID, present only when byte0's low nibble was 0xF
#   next 2  : wDataRegister (LE) -- present for every opcode except RESET/SET_POWER
#   (SET_REPORT only) next 2: wLength (LE, *including* these 2 length bytes),
#             then [Report ID byte if the 0xF sentinel was used] + report data
#
# The opcode table and the header layout come from the public spec. The SET_REPORT
# tail layout (length/id/data) was reverse-checked against two independent samples
# in a real capture and both reconciled byte-for-byte, so that part is on solid
# ground; GET_IDLE/SET_IDLE/GET_PROTOCOL/SET_PROTOCOL tails were never exercised
# in the data we've seen, so those are left undecoded past wDataRegister rather
# than guessed.
_OPCODES = {
    0x01: "RESET", 0x02: "GET_REPORT", 0x03: "SET_REPORT",
    0x04: "GET_IDLE", 0x05: "SET_IDLE", 0x06: "GET_PROTOCOL",
    0x07: "SET_PROTOCOL", 0x08: "SET_POWER",
}
_REPORT_TYPE_NAMES = {0: "Reserved", 1: "Input", 2: "Output", 3: "Feature"}


@dataclass
class CommandInfo:
    opcode: int
    opcode_name: str
    report_type: Optional[int]
    report_type_name: str
    report_id: Optional[int]
    data_register: Optional[int] = None
    length: Optional[int] = None
    data: bytes = b""
    power_state: Optional[str] = None


def decode_command_write(raw: bytes) -> Optional[CommandInfo]:
    """raw = the bytes written *after* the 2-byte Command Register pointer
    (i.e. an I2CHidEvent.raw for a register_write whose register == wCommandRegister)."""
    if len(raw) < 2:
        return None
    b0, opcode = raw[0], raw[1]
    report_type = (b0 >> 4) & 0x0F
    id_nibble = b0 & 0x0F
    idx = 2
    if id_nibble == 0x0F:
        if len(raw) < 3:
            return None
        report_id = raw[2]
        idx = 3
    else:
        report_id = id_nibble

    info = CommandInfo(opcode=opcode, opcode_name=_OPCODES.get(opcode, f"opcode 0x{opcode:02X}"),
                        report_type=report_type,
                        report_type_name=_REPORT_TYPE_NAMES.get(report_type, str(report_type)),
                        report_id=report_id)

    if opcode == 0x08:  # SET_POWER：低 nibble 其實是電源狀態，不是 report id
        info.power_state = "SLEEP" if id_nibble else "ON"
        info.report_id = None
        return info
    if opcode == 0x01:  # RESET：沒有後續欄位
        return info

    if len(raw) >= idx + 2:
        info.data_register = raw[idx] | (raw[idx + 1] << 8)
        idx += 2

    if opcode == 0x03 and len(raw) >= idx + 2:  # SET_REPORT 內嵌 length(自含) + id + data
        length = raw[idx] | (raw[idx + 1] << 8)
        info.length = length
        body = raw[idx + 2: idx + max(length, 2)]
        if id_nibble == 0x0F and body[:1] == bytes([report_id]):
            info.data = body[1:]
        else:
            info.data = body

    return info


def decode_length_prefixed_response(raw: bytes) -> Optional[dict]:
    """套同一份 [length(自含)][report id][資料] 慣例解讀讀回資料（Command/Output Register
    的回應、以及一般 Output Report 寫入）。這是從實測 bytes 反推出來的慣例，不是逐欄位
    對照官方文件，report id 猜錯的機率不是 0，只當輔助說明用。"""
    if len(raw) < 2:
        return None
    length = raw[0] | (raw[1] << 8)
    if not (2 <= length <= len(raw)):
        return None
    body = raw[2:length]
    if not body:
        return {"length": length, "report_id": None, "data": b""}
    return {"length": length, "report_id": body[0], "data": body[1:]}


@dataclass
class EventDescription:
    text: str
    report_id: Optional[int] = None
    report_type_name: Optional[str] = None


def describe_event(ev: I2CHidEvent, regmap: Optional[dict]) -> EventDescription:
    """人看得懂的一句話說明；report_id/report_type_name 給呼叫端拿去對 Report
    Descriptor 查 usage 名稱用（la_saleae 本身不碰 HIDField，維持跟描述子解析無關）。"""
    if ev.kind == "no_ack":
        return EventDescription(f"位址探測，裝置未回應（NACK，address=0x{ev.address:02X}）"
                                 "——通常是裝置還沒開機/上電，bus 上還沒有東西回話")
    if regmap is None:
        return EventDescription("")

    cmd_reg = regmap.get("wCommandRegister")
    out_reg = regmap.get("wOutputRegister")
    desc_reg = regmap.get("wReportDescRegister")
    hid_reg = regmap.get("_source_register")

    if ev.kind == "register_read" and desc_reg is not None and ev.register == desc_reg:
        return EventDescription(f"Report Descriptor 內容（{len(ev.raw)} bytes）")
    if ev.kind == "register_read" and hid_reg is not None and ev.register == hid_reg:
        return EventDescription("HID Descriptor 協商讀取")

    if cmd_reg is not None and ev.register == cmd_reg:
        if ev.kind == "register_write":
            info = decode_command_write(ev.raw)
            if info is None:
                return EventDescription("Command Register 寫入（格式無法解析）")
            if info.power_state is not None:
                return EventDescription(f"SET_POWER -> {info.power_state}")
            rtn = info.report_type_name
            if info.opcode == 0x01:
                return EventDescription("RESET")
            text = f"{info.opcode_name} type={rtn}"
            if info.report_id is not None:
                text += f" id=0x{info.report_id:02X}"
            if info.length is not None:
                text += f"，寫入 {len(info.data)} bytes"
            return EventDescription(text, info.report_id, rtn)
        if ev.kind == "register_read":
            resp = decode_length_prefixed_response(ev.raw)
            if resp is None:
                return EventDescription("Command Register 讀回（格式無法解析）")
            text = "讀回"
            if resp["report_id"] is not None:
                text += f" id=0x{resp['report_id']:02X}"
            text += f"，{len(resp['data'])} bytes（推測格式，非逐欄位對照官方文件）"
            return EventDescription(text, resp["report_id"], None)

    if out_reg is not None and ev.register == out_reg and ev.kind == "register_write":
        resp = decode_length_prefixed_response(ev.raw)
        if resp is not None:
            text = "Output Report 寫入"
            if resp["report_id"] is not None:
                text += f" id=0x{resp['report_id']:02X}"
            text += f"，{len(resp['data'])} bytes"
            return EventDescription(text, resp["report_id"], "Output")

    if is_input_report_event(ev, regmap) and ev.payload:
        rid = ev.payload[0]
        return EventDescription(f"Input Report 輪詢 id=0x{rid:02X}", rid, "Input")

    return EventDescription("")
