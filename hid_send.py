"""
HID Send Tool - Windows
Send Report ID + Data to HID devices
"""

import hid
import sys


def list_devices():
    devices = hid.enumerate()
    if not devices:
        print("未找到任何 HID 裝置")
        return []
    print("\n=== 可用 HID 裝置 ===")
    for i, dev in enumerate(devices):
        print(f"[{i}] VID={dev['vendor_id']:04X}  PID={dev['product_id']:04X}  "
              f"Usage={dev['usage_page']:04X}/{dev['usage']:04X}  "
              f"| {dev['manufacturer_string']} - {dev['product_string']}")
    return devices


def parse_hex_bytes(s: str) -> list[int]:
    """解析 hex 字串，例如 'AA BB CC' 或 'AABBCC'"""
    s = s.replace(" ", "").replace(",", "").replace("0x", "").replace("0X", "")
    if len(s) % 2 != 0:
        s = "0" + s
    return [int(s[i:i+2], 16) for i in range(0, len(s), 2)]


def send_report(device_info: dict, report_id: int, data: list[int], use_feature: bool):
    path = device_info["path"]
    try:
        dev = hid.device()
        dev.open_path(path)
        dev.set_nonblocking(1)

        payload = [report_id] + data
        print(f"\n發送 {'Feature' if use_feature else 'Output'} Report")
        print(f"  Report ID : 0x{report_id:02X} ({report_id})")
        print(f"  Data      : {' '.join(f'{b:02X}' for b in data)}")
        print(f"  完整 payload: {' '.join(f'{b:02X}' for b in payload)}")

        if use_feature:
            sent = dev.send_feature_report(payload)
        else:
            sent = dev.write(payload)

        if sent < 0:
            print(f"  [錯誤] 發送失敗 (回傳 {sent})")
        else:
            print(f"  [成功] 已發送 {sent} bytes")

        dev.close()
    except Exception as e:
        print(f"  [錯誤] {e}")


def main():
    print("=== HID Send Tool ===")

    while True:
        devices = list_devices()
        if not devices:
            input("\n按 Enter 結束...")
            sys.exit(1)

        # 選擇裝置
        while True:
            try:
                idx = int(input(f"\n選擇裝置編號 (0-{len(devices)-1}): "))
                if 0 <= idx < len(devices):
                    break
                print("編號超出範圍，請重新輸入")
            except ValueError:
                print("請輸入數字")

        selected = devices[idx]
        print(f"\n已選擇: {selected['manufacturer_string']} - {selected['product_string']}")

        # Report 類型
        rtype = input("Report 類型 [O=Output(預設) / F=Feature]: ").strip().upper()
        use_feature = rtype == "F"

        # Report ID
        while True:
            try:
                rid_str = input("Report ID (十六進位, 例如 01): ").strip()
                report_id = int(rid_str, 16)
                if 0 <= report_id <= 255:
                    break
                print("Report ID 必須在 0x00~0xFF 之間")
            except ValueError:
                print("請輸入有效的十六進位數值")

        # Data
        data_str = input("Data (十六進位, 例如 AA BB CC): ").strip()
        if data_str:
            try:
                data = parse_hex_bytes(data_str)
            except ValueError:
                print("Data 格式錯誤，請使用十六進位")
                continue
        else:
            data = []

        send_report(selected, report_id, data, use_feature)

        again = input("\n繼續發送? [Y/n]: ").strip().lower()
        if again == "n":
            break

    print("\n結束")


if __name__ == "__main__":
    main()
