# -*- coding: utf-8 -*-
"""驅動 hid_tool 的 Tk 視窗，逐分頁擷取視窗畫面到 slides/shots/。
- 跳過登入、強制 Engineer 版以取得所有分頁
- 只擷取視窗本身（topmost），不抓整個桌面
- DigiInfo 會載入一段合成 log 以呈現實際雙畫布
資料多為空（無實際觸控），僅作 UI 示意；可事後替換。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "slides", "shots")
os.makedirs(SHOTS, exist_ok=True)

# 合成一份 DigiInfo log（含表頭 + 觸控 + 筆），讓回放畫布有東西
SAMPLE = os.path.join(HERE, "_sample_digiinfo.xml")
def _write_sample():
    import math
    pen = []
    for i in range(140):
        t = i / 139
        x = int(4000 + 14000 * t)
        y = int(7000 + 3000 * math.sin(t * 9))
        down = "true" if (0.15 < t < 0.85) else "false"
        pr = 500 if down == "true" else 0
        pen.append(f'    <packet logtime="{1000+i*8}" digitizer="2" name="INK" x="{x}" y="{y}" '
                   f'down="{down}" pressure="{pr}" inrange="true" tiltx="0" tilty="0" />')
    touch = []
    for c in range(3):
        for i in range(40):
            t = i / 39
            x = int(400 + 2000 * t)
            y = int(400 + 1000 * math.sin(t * 4 + c))
            touch.append(f'    <packet logtime="{20000+c*1000+i*8}" digitizer="1" name="INK" '
                         f'x="{x}" y="{y}" down="true" confidence="true" contactid="{c+1}" '
                         f'width="30" height="20" scantime="{i*83}" />')
    body = "\n".join(pen + touch)
    xml = f'''<inputmanager version="1.0" source="DigiInfo">
  <digitizers>
    <digitizer id="1" kind="MULTI_TOUCH" maxcsrs="10">
      <properties>
        <property name="contactid" logmin="0" logmax="63" />
        <property name="x" logmin="0" logmax="2880" />
        <property name="y" logmin="0" logmax="1800" />
        <property name="confidence" logmin="0" logmax="1" />
      </properties>
    </digitizer>
    <digitizer id="2" kind="PEN" maxcsrs="1">
      <properties>
        <property name="x" logmin="0" logmax="23040" />
        <property name="y" logmin="0" logmax="14400" />
        <property name="pressure" logmin="0" logmax="4096" />
      </properties>
    </digitizer>
  </digitizers>
  <events>
{body}
  </events>
</inputmanager>'''
    with open(SAMPLE, "w", encoding="utf-8") as f:
        f.write(xml)


def fill_canvas(app):
    """灌假資料到監聽畫布 _adigi_devs（多 ID 軌跡 + 一個 confidence=0 放大）。"""
    import collections
    import math
    app._all_digi_mode = True
    devs = {}
    layout = [("Touch  vid_2575&pid_0401", (0, 4095), (0, 4095), 4),
              ("Pen    vid_2575&pid_0402", (0, 23040), (0, 14400), 1)]
    for di, (name, xr, yr, ncont) in enumerate(layout):
        contacts, trails = {}, {}
        for c in range(ncont):
            key = c + 1
            pts = []
            for i in range(44):
                t = i / 43
                x = xr[1] * (0.18 + 0.64 * t)
                y = yr[1] * (0.5 + 0.32 * math.sin(t * 5 + c * 1.4))
                pts.append((x, y))
            trails[key] = collections.deque(pts, maxlen=500)
            lx, ly = pts[-1]
            conf = 0 if (di == 0 and c == 3) else 1
            contacts[key] = {"x": lx, "y": ly, "down": True, "conf": conf}
        devs[name] = {"order": di,
                      "color": app._SLOT_COLORS[di % len(app._SLOT_COLORS)],
                      "xr": xr, "yr": yr, "contacts": contacts, "trails": trails}
    app._adigi_devs = devs


def fill_table(app, n=14):
    """依目前欄位 heading 插入假列（含 RAW 欄位時填 hex）。"""
    import random
    rng = random.Random(7)
    for iid in app._table.get_children():
        app._table.delete(iid)
    cols = list(app._table["columns"])
    heads = [app._table.heading(c, "text").strip() for c in cols]
    for r in range(n):
        vals = []
        for h in heads:
            if "裝置" in h:
                vals.append("Touch" if r % 3 else "Pen")
            elif "ScanTime" in h or h.lower() == "scan":
                vals.append(r * 83)
            elif h == "Cnt":
                vals.append(2)
            elif h == "Slot":
                vals.append(r % 4)
            elif h in ("CID", "ContactID"):
                vals.append(r % 4 + 1)
            elif h == "X":
                vals.append(420 + rng.randint(0, 1900))
            elif h == "Y":
                vals.append(310 + rng.randint(0, 1200))
            elif "Press" in h:
                vals.append(rng.choice([0, 0, 128, 512]))
            elif h in ("W", "Width"):
                vals.append(rng.randint(28, 42))
            elif h in ("H", "Height"):
                vals.append(rng.randint(18, 28))
            elif h in ("XTilt", "YTilt"):
                vals.append(rng.randint(-12, 12))
            elif "Azim" in h:
                vals.append(rng.randint(0, 3600))
            elif "Status" in h:
                vals.append(rng.choice(["Tip", "InRange", "Tip", "Tip Eraser"]))
            elif h.startswith("Conf"):
                vals.append(rng.choice([1, 1, 1, 0]))
            elif "RAW" in h.upper():
                vals.append(" ".join(f"{rng.randint(0, 255):02X}" for _ in range(12)))
            else:
                vals.append("")
        app._table.insert("", "end", values=vals)


def fill_differ(app):
    """灌假矩陣到 Differ heatmap（_hm_frames）。"""
    import random
    import heatmap_frame
    rng = random.Random(5)
    R, C = 34, 46
    m = [[rng.randint(18, 44) for _ in range(C)] for _ in range(R)]
    for r in range(12, 20):           # 一塊高值區（差異/熱點）
        for c in range(20, 30):
            m[r][c] += rng.randint(28, 70)
    app._hm_frames = [m]
    app._hm_used_tx = None
    app._hm_cur_frame = 0
    try:
        app._hm_vmin_var.set("0"); app._hm_vmax_var.set("120")
    except Exception:
        pass
    try:
        app._hm_lut = heatmap_frame.build_lut(app._hm_cmap_var.get())
    except Exception:
        pass
    try:
        app._hm_scale.configure(to=0)
    except Exception:
        pass


def main():
    _write_sample()
    import hid_tool
    hid_tool.HIDToolApp._EDITION = "Engineer"
    hid_tool.HIDToolApp._do_login = lambda self: True   # 跳過登入

    app = hid_tool.HIDToolApp()
    try:
        app.state("zoomed")
    except Exception:
        app.geometry("1600x950")
    app.update_idletasks()

    # 讓啟動排程（after）跑完
    for _ in range(30):
        app.update(); time.sleep(0.05)

    from PIL import ImageGrab

    def pump(sec=0.8):
        end = time.time() + sec
        while time.time() < end:
            app.update(); time.sleep(0.03)

    def grab(name):
        app.deiconify(); app.lift()
        app.attributes("-topmost", True)
        pump(0.6)
        app.update_idletasks(); app.update()
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        p = os.path.join(SHOTS, name + ".png")
        img.save(p)
        # 偵測是否整片同色（黑/白）＝抓不到畫面
        ex = img.convert("RGB").getextrema()
        flat = all(lo == hi for lo, hi in ex)
        print(f"saved {name}.png  {img.size}  {'[WARN 單一色，可能抓不到畫面]' if flat else 'OK'}")
        return not flat

    nb = app._notebook

    def select_tab(keyword):
        for tid in nb.tabs():
            if keyword in nb.tab(tid, "text"):
                nb.select(tid); return True
        return False

    ok_any = False

    # ① Report Descriptor 欄位樹：展開側欄 + 選一個 digitizer 同步讀 descriptor
    select_tab("監聽")
    try:
        if getattr(app, "_desc_collapsed", False):
            app._toggle_desc_panel()
        pump(0.4)
        import hid_tool as H
        dev = next((d for d in app._hidapi_devices if d.get("usage_page") == 0x0D), None)
        if dev is None and getattr(app, "_hidapi_devices", None):
            dev = app._hidapi_devices[0]
        if dev is not None:
            ps = app._get_dev_path_str(dev)
            if ps not in app._descriptors:
                raw = H.read_descriptor_via_hidapi(dev.get("path", b""))
                app._descriptors[ps] = H.parse_report_descriptor(raw) if raw else []
            app._populate_desc_tree(ps)
            pump(0.6)
        grab("01_descriptor")
    except Exception:
        import traceback; traceback.print_exc()
    finally:
        try:
            if not getattr(app, "_desc_collapsed", True):
                app._toggle_desc_panel()
        except Exception:
            pass
        pump(0.3)

    # 灌假資料（畫布 + 表格）
    select_tab("監聽")
    fill_canvas(app)
    fill_table(app)
    pump(0.4)

    def canvas_off():
        try:
            if getattr(app, "_canvas_shown", False):
                app._toggle_monitor_canvas()
        except Exception:
            pass

    def canvas_on():
        try:
            if not getattr(app, "_canvas_shown", False):
                app._toggle_monitor_canvas()
        except Exception:
            pass
        pump(0.6)
        try:
            app._redraw_digi_canvas()
        except Exception:
            pass

    # ③ 表格（不開畫布）
    canvas_off(); pump(0.5); ok_any |= grab("03_monitor_data")
    # ⑤ 表格 + 畫布各佔一半
    canvas_on(); pump(0.4); grab("05_canvas")

    # ⑥ 畫布特寫（從 05 裁右半畫布區）
    try:
        from PIL import Image as _Im
        im = _Im.open(os.path.join(SHOTS, "05_canvas.png"))
        W, H = im.size
        im.crop((int(W * 0.51), int(H * 0.31), W - 6, H - 8)).save(
            os.path.join(SHOTS, "06_canvas_detail.png"))
        print("saved 06_canvas_detail.png (crop)")
    except Exception:
        import traceback; traceback.print_exc()

    # ④ RAW 欄位：開 RAW + 重建欄位 + 填表（不開畫布，看完整表）
    canvas_off(); pump(0.3)
    try:
        app._show_raw.set(True)
        app._rebuild_table_columns()
        fill_table(app, n=12)
        pump(0.5); grab("04_raw_ff01")
        app._show_raw.set(False)
        app._rebuild_table_columns()
        fill_table(app)
    except Exception:
        import traceback; traceback.print_exc()

    # 發送
    if select_tab("發送"):
        pump(0.6); grab("11_send")
    # 壓測
    if select_tab("壓測"):
        pump(0.6); grab("12_stress")

    # 回放 → DigiInfo，載入合成 log
    if select_tab("回放"):
        pump(0.4)
        rnb = getattr(app, "_replay_nb", None)
        if rnb is not None:
            for tid in rnb.tabs():
                if "DigiInfo" in rnb.tab(tid, "text") or "Digi" in rnb.tab(tid, "text"):
                    rnb.select(tid); break
        pump(0.4)
        try:
            # 直接在主執行緒解析 + 套用（避免 worker thread 呼叫 Tk after 的限制）
            import digiinfo_parse
            res = digiinfo_parse.parse_digiinfo_xml(SAMPLE)
            app._digi_path_var.set(SAMPLE)
            app._digi_apply(res)
            pump(0.6)
            print("digi frames:", len(getattr(app, "_digi_frames", []) or []))
            if hasattr(app, "_digi_show_all"):
                app._digi_show_all()      # 跳到最後一幀顯示完整軌跡
            pump(1.4)
        except Exception as e:
            import traceback; traceback.print_exc()
            print("digiinfo load err:", e)
        grab("10_digiinfo")

        # ⑦ Differ：切到 Differ 子分頁 + 灌假矩陣
        if rnb is not None:
            for tid in rnb.tabs():
                if "Differ" in rnb.tab(tid, "text"):
                    rnb.select(tid); break
            pump(0.4)
            try:
                fill_differ(app)
                pump(0.3)
                app._hm_redraw_frame()
                pump(0.5)
                app._hm_redraw_frame()
                grab("07_differ")
            except Exception:
                import traceback; traceback.print_exc()

    print("done. any-good:", ok_any)
    try:
        app.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    main()
