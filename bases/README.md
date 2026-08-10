# Descriptor 比對的 base target

開啟「Descriptor 比對」視窗時，會自動拿目前選到的裝置
（含同一顆 IC 的其他 digitizer collection）跟這裡的 base 比對。

## 目錄規則

- `<profile>.txt` — 單一 collection 的 profile
- `<profile>/*.txt` — 多個 collection 合成一個 profile（例如 touch screen + stylus 分兩個檔）

解不出 hex 的 `.txt`（說明、筆記）會自動略過，不會被當成 base。
檔案內容支援 `05 0D …`、`0x05, 0x0D, …`，以及 `//` 與 `/* */` 註解。

## 自動挑選規則

此功能只對 **VID 2808** 且包含 Touch Screen (0D:04) 的裝置開放。
分類完全由程式判斷，使用者不需要也不能手動切換 base。

依裝置的 top-level collection 判斷，先看有沒有筆：

| 偵測到 | 使用的 profile |
|---|---|
| Pen (0D:02) | `touchscreen_pen` |
| Touch Screen (0D:04) | `touchscreen` |

挑不到對應 profile 時視窗會提示缺少哪一個 base。
對照表在 `hid_desc_compare.py` 的 `_PROFILE_BY_DEVICE`。

## 目前的 base

| profile | 來源 |
|---|---|
| `touchscreen` | LS02 touch screen report descriptor（FT3610 ASUS LS02，不帶筆） |
| `touchscreen_pen` | PZ14 touch screen + PZ14 stylus report descriptor |

## 更新 base

exe 旁邊如果另外放一個 `bases/` 資料夾，會**優先於**打包進 exe 的那份，
換 base 不用重 build。
