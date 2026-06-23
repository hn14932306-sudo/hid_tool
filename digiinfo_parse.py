"""
DigiInfo XML 解析後端 — 解析 Focaltech DigiInfo 觸控日誌 XML，抽出觸控點並
整理成 long（每點一列）與 wide（以 row_id 為列、依 contactid 樞紐）兩種表，
並支援匯出 CSV。

純 Python 實作，不依賴 pandas，維持小體積。

巢狀規則（沿用原工具）：
  - <events> 區塊內所有 <packet> 才收。
  - 在 <frame> 內的所有 packet 共用同一個 row_id（同一幀）。
  - 不在 <frame> 內的頂層 packet 各自一個 row_id。
  - 只有 x、y 皆有值才保留 down/confidence；任一缺值則留空。
"""

import csv
import os
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple

_TRUE_SET = ("1", "true", "yes", "down", "pressed")
_FALSE_SET = ("0", "false", "no", "up", "released")


def _lower_attrib(d) -> dict:
    return {(k.lower() if isinstance(k, str) else k): v for k, v in (d or {}).items()}


def _time_like(at: dict) -> Tuple[Optional[str], Optional[str]]:
    st = at.get("scantime") or at.get("time")
    lt = at.get("logtime") or at.get("timestamp") or at.get("systemtime")
    return st, lt


def _to_int(v) -> Optional[int]:
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _map_bool(v) -> Optional[bool]:
    s = str(v).lower().strip() if v is not None and str(v).strip() else ""
    if s in _TRUE_SET:
        return True
    if s in _FALSE_SET:
        return False
    return None


def parse_digiinfo_xml(path: str, progress_cb: Optional[Callable[[int], None]] = None):
    """解析 XML。回傳 dict：frames / wide_rows / wide_cols / long_rows / long_cols / stats。"""
    rows: List[dict] = []
    file_size = os.path.getsize(path) if os.path.exists(path) else 0
    last_pct = -1

    in_events = False
    events_ctx: Optional[dict] = None
    frame_stack: List[dict] = []
    next_row_id = 1
    order_seq = 0

    # <digitizers> 表頭：每個 digitizer 宣告的座標範圍，畫布座標軸用這個才準
    digi_ranges: Dict[int, dict] = {}   # id -> {"x":(lo,hi), "y":(lo,hi)}
    cur_digi: Optional[int] = None
    pen_digis: set = set()              # 進「筆」群（無 contactid）的 digitizer id
    touch_digis: set = set()            # 進「手」群（有 contactid）的 digitizer id

    with open(path, "rb") as fh:
        for ev, elem in ET.iterparse(fh, events=("start", "end")):
            tag = (elem.tag or "").lower()
            local = tag.rsplit("}", 1)[-1]   # 去掉命名空間

            if ev == "start":
                if local == "digitizer":
                    at = _lower_attrib(elem.attrib)
                    try:
                        cur_digi = int(at.get("id"))
                        digi_ranges[cur_digi] = {"x": None, "y": None}
                    except (TypeError, ValueError):
                        cur_digi = None
                elif local == "property" and cur_digi is not None:
                    at = _lower_attrib(elem.attrib)
                    nm = (at.get("name") or "").lower()
                    if nm in ("x", "y"):
                        try:
                            digi_ranges[cur_digi][nm] = (
                                float(at.get("logmin")), float(at.get("logmax")))
                        except (TypeError, ValueError):
                            pass
                if "events" in tag:
                    in_events = True
                    at = _lower_attrib(elem.attrib)
                    st, lt = _time_like(at)
                    events_ctx = {"scantime": st, "logtime": lt}
                elif "frame" in tag and in_events:
                    at = _lower_attrib(elem.attrib)
                    st, lt = _time_like(at)
                    if st is None:
                        st = (events_ctx or {}).get("scantime")
                    if lt is None:
                        lt = (events_ctx or {}).get("logtime")
                    frame_stack.append({"scantime": st, "logtime": lt, "row_id": next_row_id})
                    next_row_id += 1
            else:  # end
                if "packet" in tag and in_events:
                    at = _lower_attrib(elem.attrib)
                    if frame_stack:
                        gid = frame_stack[-1]["row_id"]
                        st = (at.get("scantime") or at.get("time")
                              or frame_stack[-1]["scantime"] or (events_ctx or {}).get("scantime"))
                        lt = (at.get("logtime") or at.get("timestamp") or at.get("systemtime")
                              or frame_stack[-1]["logtime"] or (events_ctx or {}).get("logtime"))
                    else:
                        gid = next_row_id
                        next_row_id += 1
                        st = at.get("scantime") or at.get("time") or (events_ctx or {}).get("scantime")
                        lt = (at.get("logtime") or at.get("timestamp") or at.get("systemtime")
                              or (events_ctx or {}).get("logtime"))

                    x_raw, y_raw = at.get("x"), at.get("y")
                    has_x = x_raw is not None and str(x_raw).strip() != ""
                    has_y = y_raw is not None and str(y_raw).strip() != ""

                    if has_x or has_y:
                        contactid = at.get("contactid") or at.get("id") or at.get("contact")
                        # 沒有 contactid 的裝置（如手寫筆 digitizer kind=PEN）給合成 ID，
                        # 否則 wide/frames 樞紐會把它丟掉，畫布上看不到。
                        # 以 digitizer 區分（90+），避免與觸控 contactid(0~63) 撞號。
                        synth = contactid is None
                        dig_raw = at.get("digitizer")
                        try:
                            dig_id = (int(dig_raw)
                                      if dig_raw is not None and str(dig_raw).strip() != ""
                                      else None)
                        except (TypeError, ValueError):
                            dig_id = None
                        if synth:
                            if dig_id is not None:
                                contactid = 90 + dig_id
                                pen_digis.add(dig_id)
                        elif dig_id is not None:
                            touch_digis.add(dig_id)
                        if has_x and has_y:
                            c_raw = at.get("confidence")
                            if synth:
                                # 筆：tip(down) 或 eraser 或 有壓力 才算「接觸」→ 才連線；
                                # 純懸空(inrange 但無壓力)只顯示位置、不畫線
                                tip = _map_bool(at.get("down"))
                                era = _map_bool(at.get("eraser"))
                                pr = _to_float(at.get("pressure"))
                                contact = bool(tip) or bool(era) or (pr is not None and pr > 0)
                                d_raw = "true" if contact else "false"
                                if c_raw is None:
                                    c_raw = "true"          # 筆無 confidence → 高信心，不標 palm
                            else:
                                d_raw = at.get("down")
                        else:
                            d_raw, c_raw = None, None
                        order_seq += 1
                        rows.append({
                            "row_id": gid,
                            "order_seq": order_seq,
                            "x": x_raw if has_x else None,
                            "y": y_raw if has_y else None,
                            "down": d_raw,
                            "confidence": c_raw,
                            # 筆專屬狀態（觸控多為空）：tip=下筆、invert/eraser/inrange、壓力
                            "tip": _map_bool(at.get("down")),
                            "invert": _map_bool(at.get("inverted")),
                            "eraser": _map_bool(at.get("eraser")),
                            "inrange": _map_bool(at.get("inrange")),
                            "pressure": _to_int(at.get("pressure")),
                            "scantime": st,
                            "contactid": contactid,
                            "logtime": lt,
                        })

                if local == "digitizer":
                    cur_digi = None
                if "frame" in tag and frame_stack:
                    frame_stack.pop()
                elif "events" in tag:
                    in_events = False
                    events_ctx = None

                elem.clear()
                if progress_cb and file_size:
                    try:
                        pct = int(fh.tell() * 100 / file_size)
                        if pct != last_pct:
                            last_pct = pct
                            progress_cb(pct)
                    except Exception:
                        pass

    # 依 <digitizers> 表頭算出每群（手/筆）的座標範圍；多個 digitizer 取聯集(取最大)
    def _group_bounds(digis):
        xs_hi, ys_hi = [], []
        for d in digis:
            rng = digi_ranges.get(d)
            if rng and rng.get("x") and rng.get("y"):
                xs_hi.append(rng["x"][1]); ys_hi.append(rng["y"][1])
        if not xs_hi:
            return None
        return (0.0, max(xs_hi), 0.0, max(ys_hi))

    bounds_touch = _group_bounds(touch_digis)
    bounds_pen = _group_bounds(pen_digis)

    if not rows:
        empty_stats = dict(total_points=0, rows_with_xy=0, unique_row_id=0, unique_scantime=0)
        return {
            "frames": [], "wide_rows": [], "wide_cols": [],
            "long_rows": [], "long_cols": [], "stats": empty_stats,
            "bounds_touch": bounds_touch, "bounds_pen": bounds_pen,
        }

    # ---- long 清整 ----
    rows_with_xy = 0
    scantime_set = set()
    rowid_set = set()
    for r in rows:
        r["row_id"] = _to_int(r["row_id"])
        r["x"] = _to_float(r["x"])
        r["y"] = _to_float(r["y"])
        r["scantime"] = _to_int(r["scantime"])
        r["contactid"] = _to_int(r["contactid"])
        r["logtime"] = _to_int(r["logtime"])
        down = _map_bool(r["down"])
        conf = _map_bool(r["confidence"])
        xy_ok = r["x"] is not None and r["y"] is not None
        if xy_ok:
            rows_with_xy += 1
            down = False if down is None else down
            conf = False if conf is None else conf
        else:
            down = None
            conf = None
        r["down"], r["confidence"] = down, conf
        if r["row_id"] is not None:
            rowid_set.add(r["row_id"])
        if r["scantime"] is not None:
            scantime_set.add(r["scantime"])

    long_cols = ["row_id", "order_seq", "x", "y", "down", "tip", "invert",
                 "eraser", "inrange", "pressure", "confidence",
                 "scantime", "contactid", "logtime"]

    # ---- wide 樞紐 ----
    # 每個 row_id：第一筆的 scantime/logtime；各 contactid 第一筆的 x/y/down/conf
    meta: Dict[int, dict] = {}
    pivot: Dict[int, dict] = {}
    cids_seen = set()
    for r in sorted(rows, key=lambda d: d["order_seq"]):
        rid = r["row_id"]
        if rid is None:
            continue
        if rid not in meta:
            meta[rid] = {"scantime": r["scantime"], "logtime": r["logtime"]}
            pivot[rid] = {}
        cid = r["contactid"]
        if cid is None:
            continue
        cids_seen.add(cid)
        slot = pivot[rid]
        for base in ("x", "y", "down", "confidence",
                     "tip", "invert", "eraser", "inrange", "pressure"):
            col = f"{base}_{cid}"
            if col not in slot:           # first 聚合
                slot[col] = r[base]

    sorted_cids = sorted(cids_seen)
    # 筆專屬欄位只對「該接點確實有值」才加欄，避免觸控產生一堆空欄
    _EXTRA = ("tip", "invert", "eraser", "inrange", "pressure")
    extra_has = set()
    for rid in pivot:
        for cid in sorted_cids:
            for f in _EXTRA:
                if pivot[rid].get(f"{f}_{cid}") is not None:
                    extra_has.add((cid, f))
    wide_cols = ["row_id", "logtime", "scantime"]
    for cid in sorted_cids:
        wide_cols += [f"x_{cid}", f"y_{cid}"]
    for cid in sorted_cids:
        wide_cols += [f"down_{cid}", f"confidence_{cid}"]
        for f in _EXTRA:
            if (cid, f) in extra_has:
                wide_cols += [f"{f}_{cid}"]

    wide_rows: List[dict] = []
    frames: List[dict] = []
    for rid in sorted(meta.keys()):
        slot = pivot[rid]
        row = {"row_id": rid, "logtime": meta[rid]["logtime"], "scantime": meta[rid]["scantime"]}
        contacts: Dict[int, dict] = {}
        for cid in sorted_cids:
            x = slot.get(f"x_{cid}")
            y = slot.get(f"y_{cid}")
            d = slot.get(f"down_{cid}")
            c = slot.get(f"confidence_{cid}")
            xy_ok = x is not None and y is not None
            if not xy_ok:
                d = c = None
            else:
                d = False if d is None else d
                c = False if c is None else c
            row[f"x_{cid}"] = x
            row[f"y_{cid}"] = y
            row[f"down_{cid}"] = d
            row[f"confidence_{cid}"] = c
            for f in _EXTRA:
                if (cid, f) in extra_has:
                    row[f"{f}_{cid}"] = slot.get(f"{f}_{cid}")
            if xy_ok:
                contacts[cid] = {"x": x, "y": y, "down": d, "conf": c}
        wide_rows.append(row)
        frames.append({"row_id": rid, "logtime": meta[rid]["logtime"],
                       "scantime": meta[rid]["scantime"], "contacts": contacts})

    stats = dict(
        total_points=len(rows),
        rows_with_xy=rows_with_xy,
        unique_row_id=len(rowid_set),
        unique_scantime=len(scantime_set),
    )
    return {
        "frames": frames,
        "wide_rows": wide_rows,
        "wide_cols": wide_cols,
        "long_rows": sorted(rows, key=lambda d: d["order_seq"]),
        "long_cols": long_cols,
        "stats": stats,
        "bounds_touch": bounds_touch, "bounds_pen": bounds_pen,
    }


def _fmt_cell(col: str, val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "True" if val else "False"
    if (col.startswith("x_") or col.startswith("y_") or col in ("x", "y")) and isinstance(val, float):
        return str(int(round(val)))
    return str(val)


def write_csv(path: str, rows: List[dict], cols: List[str]):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt_cell(c, r.get(c)) for c in cols])
