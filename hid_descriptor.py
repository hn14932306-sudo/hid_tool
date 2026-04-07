"""
HID Report Descriptor parser and field value extractor.
"""

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# bType constants
# ---------------------------------------------------------------------------

BTYPE_MAIN   = 0
BTYPE_GLOBAL = 1
BTYPE_LOCAL  = 2

# bTag — Main items
TAG_INPUT            = 8
TAG_OUTPUT           = 9
TAG_FEATURE          = 11
TAG_BEGIN_COLLECTION = 10
TAG_END_COLLECTION   = 12

# bTag — Global items
TAG_USAGE_PAGE   = 0
TAG_LOG_MIN      = 1
TAG_LOG_MAX      = 2
TAG_PHY_MIN      = 3
TAG_PHY_MAX      = 4
TAG_UNIT_EXP     = 5
TAG_UNIT         = 6
TAG_REPORT_SIZE  = 7
TAG_REPORT_ID    = 8
TAG_REPORT_COUNT = 9
TAG_PUSH         = 10
TAG_POP          = 11

# bTag — Local items
TAG_USAGE     = 0
TAG_USAGE_MIN = 1
TAG_USAGE_MAX = 2

# Report type strings
REPORT_TYPE_INPUT   = "Input"
REPORT_TYPE_OUTPUT  = "Output"
REPORT_TYPE_FEATURE = "Feature"


# ---------------------------------------------------------------------------
# HIDField dataclass
# ---------------------------------------------------------------------------

@dataclass
class HIDField:
    report_id:    int
    report_type:  str
    bit_offset:   int   # relative to data after report-ID byte
    bit_size:     int   # total bits = report_size * report_count
    report_count: int
    usage_page:   int
    usages:       List[int]
    logical_min:  int
    logical_max:  int
    flags:        int
    is_const:     bool

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


# ---------------------------------------------------------------------------
# Internal decode helpers
# ---------------------------------------------------------------------------

def _signed(value: int, bits: int) -> int:
    if bits <= 0:
        return 0
    sign_bit = 1 << (bits - 1)
    if value & sign_bit:
        value -= (1 << bits)
    return value


def _decode_item_value(data: bytes, size: int) -> int:
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
    if size == 0:
        return 0
    if size == 1:
        return struct.unpack_from("<b", data)[0]
    if size == 2:
        return struct.unpack_from("<h", data)[0]
    if size == 4:
        return struct.unpack_from("<i", data)[0]
    return 0


# ---------------------------------------------------------------------------
# Descriptor parser
# ---------------------------------------------------------------------------

def parse_report_descriptor(raw: bytes) -> List[HIDField]:
    """Parse a HID Report Descriptor and return a list of HIDField objects."""
    fields: List[HIDField] = []

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

    global_stack = []

    usages:    List[int]      = []
    usage_min: Optional[int]  = None
    usage_max: Optional[int]  = None

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

        if prefix == 0xFE:          # long item
            if i < n:
                long_size = raw[i]
                i += 1
                i += 1 + long_size
            continue

        b_size = prefix & 0x03
        b_type = (prefix >> 2) & 0x03
        b_tag  = (prefix >> 4) & 0x0F

        actual_size = b_size if b_size < 3 else 4
        item_data   = raw[i: i + actual_size]
        i += actual_size

        if b_type == BTYPE_GLOBAL:
            val_u = _decode_item_value(item_data, actual_size)
            val_s = _decode_signed_item_value(item_data, actual_size)

            if   b_tag == TAG_USAGE_PAGE:   usage_page   = val_u
            elif b_tag == TAG_LOG_MIN:      logical_min  = val_s
            elif b_tag == TAG_LOG_MAX:      logical_max  = val_s
            elif b_tag == TAG_PHY_MIN:      physical_min = val_s
            elif b_tag == TAG_PHY_MAX:      physical_max = val_s
            elif b_tag == TAG_UNIT_EXP:     unit_exp     = val_s
            elif b_tag == TAG_UNIT:         unit         = val_u
            elif b_tag == TAG_REPORT_SIZE:  report_size  = val_u
            elif b_tag == TAG_REPORT_ID:    report_id    = val_u
            elif b_tag == TAG_REPORT_COUNT: report_count = val_u
            elif b_tag == TAG_PUSH:
                global_stack.append((
                    usage_page, logical_min, logical_max,
                    physical_min, physical_max, unit_exp, unit,
                    report_size, report_id, report_count,
                ))
            elif b_tag == TAG_POP:
                if global_stack:
                    (usage_page, logical_min, logical_max,
                     physical_min, physical_max, unit_exp, unit,
                     report_size, report_id, report_count) = global_stack.pop()

        elif b_type == BTYPE_LOCAL:
            val_u = _decode_item_value(item_data, actual_size)
            if   b_tag == TAG_USAGE:     usages.append(val_u & 0xFFFF if actual_size == 4 else val_u)
            elif b_tag == TAG_USAGE_MIN: usage_min = val_u
            elif b_tag == TAG_USAGE_MAX: usage_max = val_u

        elif b_type == BTYPE_MAIN:
            if b_tag in (TAG_INPUT, TAG_OUTPUT, TAG_FEATURE):
                flags    = _decode_item_value(item_data, actual_size)
                is_const = bool(flags & 0x01)

                if   b_tag == TAG_INPUT:   rtype = REPORT_TYPE_INPUT
                elif b_tag == TAG_OUTPUT:  rtype = REPORT_TYPE_OUTPUT
                else:                      rtype = REPORT_TYPE_FEATURE

                effective_usages: List[int] = list(usages)
                if usage_min is not None and usage_max is not None:
                    effective_usages += list(range(usage_min, usage_max + 1))

                if len(effective_usages) < report_count:
                    pad = effective_usages[-1] if effective_usages else 0
                    effective_usages += [pad] * (report_count - len(effective_usages))

                key        = (report_id, rtype)
                bit_offset = bit_offsets.get(key, 0)
                total_bits = report_size * report_count

                fields.append(HIDField(
                    report_id    = report_id,
                    report_type  = rtype,
                    bit_offset   = bit_offset,
                    bit_size     = total_bits,
                    report_count = report_count,
                    usage_page   = usage_page,
                    usages       = effective_usages[:report_count] if effective_usages else [],
                    logical_min  = logical_min,
                    logical_max  = logical_max,
                    flags        = flags,
                    is_const     = is_const,
                ))
                bit_offsets[key] = bit_offset + total_bits

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
    starting at `bit_offset`. Sign-extends when logical_min < 0.
    """
    results = []
    if per_bit_size <= 0 or count <= 0:
        return results

    do_sign = logical_min < 0

    for idx in range(count):
        start_bit  = bit_offset + idx * per_bit_size
        start_byte = start_bit >> 3
        end_byte   = (start_bit + per_bit_size - 1) >> 3

        if end_byte >= len(data):
            break

        raw_val = 0
        for b in range(end_byte, start_byte - 1, -1):
            raw_val = (raw_val << 8) | data[b]

        raw_val >>= start_bit - (start_byte * 8)
        raw_val  &= (1 << per_bit_size) - 1

        if do_sign:
            raw_val = _signed(raw_val, per_bit_size)

        results.append(raw_val)

    return results


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


def get_usage_name(usage_page: int, usage: int) -> str:
    name = _USAGE_NAME.get((usage_page, usage))
    if name:
        return name
    if usage_page >= 0xFF00:
        return f"V{usage:02X}"
    return f"UP{usage_page:02X}_U{usage:02X}"
