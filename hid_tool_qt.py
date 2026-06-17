"""
Qt prototype shell for HID Tool.

This file is intentionally separate from hid_tool.py so the current Tkinter
tool remains usable while the Qt UI is brought up feature by feature.
"""

import sys
from typing import List, Optional

try:
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QBrush, QColor, QPainter, QPen
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QRadioButton,
        QScrollArea,
        QSizePolicy,
        QSlider,
        QSpinBox,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextEdit,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    QT_IMPORT_ERROR = exc
else:
    QT_IMPORT_ERROR = None

from hid_descriptor import HIDField, parse_report_descriptor
from hid_device import (
    enumerate_hid_devices,
    format_device_label,
    read_descriptor_via_hidapi,
)


APP_NAME = "HID Tool"
APP_AUTHOR = "alfferus.lin"
APP_VERSION_LABEL = "v2026.06.16"
APP_VERSION_TIME = "2026-06-16"
CMD_SAME_LABEL = "（同監聽裝置）"


if QT_IMPORT_ERROR is None:

    class StatusChip(QLabel):
        def __init__(self, text: str, tone: str = "neutral"):
            super().__init__(text)
            self.setObjectName(f"chip-{tone}")
            self.setAlignment(Qt.AlignCenter)
            self.setMinimumWidth(82)


    class TouchCanvas(QWidget):
        """Lightweight placeholder for the future Qt touch inspector."""

        def __init__(self):
            super().__init__()
            self.setMinimumWidth(360)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._x_range = (0, 4096)
            self._y_range = (0, 4096)

        def set_range(self, x_range, y_range):
            self._x_range = x_range
            self._y_range = y_range
            self.update()

        def paintEvent(self, _event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.rect(), QColor("#fbfcfd"))

            pad = 24
            area = QRectF(
                pad,
                pad,
                max(1, self.width() - pad * 2),
                max(1, self.height() - pad * 2),
            )

            painter.setPen(QPen(QColor("#cbd5e1"), 1))
            painter.drawRect(area)

            painter.setPen(QPen(QColor("#e2e8f0"), 1))
            for i in range(1, 5):
                x = area.left() + area.width() * i / 5
                y = area.top() + area.height() * i / 5
                painter.drawLine(int(x), int(area.top()), int(x), int(area.bottom()))
                painter.drawLine(int(area.left()), int(y), int(area.right()), int(y))

            painter.setPen(QColor("#64748b"))
            x0, x1 = self._x_range
            y0, y1 = self._y_range
            painter.drawText(int(area.left()), int(area.top()) - 8, f"({x0}, {y0})")
            painter.drawText(int(area.right()) - 96, int(area.bottom()) + 18, f"({x1}, {y1})")

            # Static sample markers make the shell visually representative while
            # the live monitor pipeline is still owned by the Tkinter app.
            points = [
                (0.28, 0.32, "#2563eb", "1"),
                (0.46, 0.56, "#16a34a", "2"),
                (0.62, 0.42, "#f59e0b", "3"),
            ]
            for px, py, color, label in points:
                cx = area.left() + area.width() * px
                cy = area.top() + area.height() * py
                painter.setPen(QPen(QColor(color), 2))
                painter.setBrush(QColor(color))
                painter.drawEllipse(int(cx) - 7, int(cy) - 7, 14, 14)
                painter.setPen(QColor("#0f172a"))
                painter.drawText(int(cx) + 10, int(cy) + 4, label)


    class HIDToolQt(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} - Qt Shell - {APP_VERSION_LABEL}")
            self.resize(1440, 860)
            self.setMinimumSize(1120, 720)

            self._devices: List[dict] = []
            self._selected_dev: Optional[dict] = None
            self._raw_descriptor: Optional[bytes] = None
            self._fields: List[HIDField] = []

            self._build_ui()
            self._apply_style()
            self.refresh_devices()

        def _build_ui(self):
            root = QWidget()
            root_layout = QVBoxLayout(root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)
            self.setCentralWidget(root)

            self._build_top_bar(root_layout)
            self._build_workspace(root_layout)
            self._build_status_bar(root_layout)

        def _build_top_bar(self, root_layout: QVBoxLayout):
            top = QFrame()
            top.setObjectName("topbar")
            top_layout = QVBoxLayout(top)
            top_layout.setContentsMargins(14, 10, 14, 10)
            top_layout.setSpacing(8)
            root_layout.addWidget(top)

            title_row = QHBoxLayout()
            title_row.setSpacing(8)
            top_layout.addLayout(title_row)

            title = QLabel(APP_NAME)
            title.setObjectName("app-title")
            title_row.addWidget(title)

            subtitle = QLabel("Monitor / Send / Stress / Replay")
            subtitle.setObjectName("subtitle")
            title_row.addWidget(subtitle)
            title_row.addStretch(1)

            self.rate_chip = StatusChip("0 scan/s", "ok")
            self.error_chip = StatusChip("ERR 0", "err")
            self.record_chip = StatusChip("REC 0", "neutral")
            title_row.addWidget(self.rate_chip)
            title_row.addWidget(self.error_chip)
            title_row.addWidget(self.record_chip)

            device_row = QHBoxLayout()
            device_row.setSpacing(8)
            top_layout.addLayout(device_row)

            device_row.addWidget(self._field_label("監聽裝置"))
            self.monitor_combo = QComboBox()
            self.monitor_combo.setMinimumWidth(420)
            self.monitor_combo.currentIndexChanged.connect(self._on_monitor_selected)
            device_row.addWidget(self.monitor_combo, 1)

            device_row.addWidget(self._field_label("指令裝置"))
            self.command_combo = QComboBox()
            self.command_combo.setMinimumWidth(300)
            device_row.addWidget(self.command_combo)

            self.refresh_btn = QPushButton("重新整理")
            self.refresh_btn.clicked.connect(self.refresh_devices)
            device_row.addWidget(self.refresh_btn)

            self.listen_btn = QPushButton("開始監聽")
            self.listen_btn.setObjectName("start-button")
            self.listen_btn.clicked.connect(self._not_wired_yet)
            device_row.addWidget(self.listen_btn)

        def _build_workspace(self, root_layout: QVBoxLayout):
            splitter = QSplitter(Qt.Horizontal)
            splitter.setObjectName("workspace")
            root_layout.addWidget(splitter, 1)

            descriptor_panel = self._section_frame("Report Descriptor")
            descriptor_layout = QVBoxLayout(descriptor_panel)
            descriptor_layout.setContentsMargins(8, 8, 8, 8)
            descriptor_layout.setSpacing(8)

            self.descriptor_tree = QTreeWidget()
            self.descriptor_tree.setHeaderLabels(["欄位 / 名稱", "位元大小", "Logical 範圍"])
            self.descriptor_tree.setColumnWidth(0, 210)
            descriptor_layout.addWidget(self.descriptor_tree, 1)

            self.raw_btn = QPushButton("原始 Descriptor Bytes")
            self.raw_btn.clicked.connect(self.show_raw_descriptor)
            descriptor_layout.addWidget(self.raw_btn)

            splitter.addWidget(descriptor_panel)

            self.tabs = QTabWidget()
            self.tabs.addTab(self._build_monitor_tab(), "監聽")
            self.tabs.addTab(self._build_send_tab(), "發送")
            self.tabs.addTab(self._build_stress_tab(), "壓測")
            self.tabs.addTab(self._build_replay_tab(), "回放")
            splitter.addWidget(self.tabs)
            splitter.setSizes([330, 1110])

        def _build_monitor_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)

            control = QGroupBox("監聽顯示與篩選")
            control_layout = QVBoxLayout(control)
            control_layout.setSpacing(8)

            display = QHBoxLayout()
            display.setSpacing(8)
            control_layout.addLayout(display)

            display.addWidget(QLabel("Report ID:"))
            self.rid_combo = QComboBox()
            self.rid_combo.addItem("全部")
            self.rid_combo.setMaximumWidth(90)
            display.addWidget(self.rid_combo)

            display.addWidget(QLabel("View:"))
            self.view_combo = QComboBox()
            self.view_combo.addItems(["Hybrid", "Parallel"])
            self.view_combo.setMaximumWidth(120)
            display.addWidget(self.view_combo)

            self.raw_check = QCheckBox("顯示 RAW 欄位")
            display.addWidget(self.raw_check)

            self.canvas_btn = QPushButton("隱藏畫布 ◀")
            self.canvas_btn.clicked.connect(self._toggle_canvas_panel)
            display.addWidget(self.canvas_btn)
            display.addStretch(1)

            self.export_btn = QPushButton("匯出 Excel")
            self.export_btn.clicked.connect(self._not_wired_yet)
            display.addWidget(self.export_btn)
            self.clear_btn = QPushButton("清除")
            self.clear_btn.clicked.connect(self._not_wired_yet)
            display.addWidget(self.clear_btn)

            advanced = QHBoxLayout()
            advanced.setSpacing(8)
            control_layout.addLayout(advanced)
            advanced.addWidget(QLabel("Frame gap(ms):"))
            self.gap_spin = QSpinBox()
            self.gap_spin.setRange(1, 50)
            self.gap_spin.setValue(4)
            advanced.addWidget(self.gap_spin)
            advanced.addWidget(QLabel("最大 scan Δ:"))
            self.scan_delta_spin = QSpinBox()
            self.scan_delta_spin.setRange(0, 9999)
            self.scan_delta_spin.setValue(200)
            advanced.addWidget(self.scan_delta_spin)
            advanced.addWidget(QLabel("保留筆數:"))
            self.keep_rows_spin = QSpinBox()
            self.keep_rows_spin.setRange(50, 5000)
            self.keep_rows_spin.setSingleStep(50)
            self.keep_rows_spin.setValue(200)
            advanced.addWidget(self.keep_rows_spin)
            advanced.addStretch(1)

            layout.addWidget(control)

            ff01 = QGroupBox("Usage Page FF01 欄位顯示")
            ff01_layout = QHBoxLayout(ff01)
            ff01_layout.addWidget(QLabel("（尚未載入 FF01 欄位）"))
            ff01_layout.addStretch(1)
            ff01_layout.addWidget(QLabel("格式:"))
            self.ff01_format = QComboBox()
            self.ff01_format.addItems(["Hex", "Dec", "Bin"])
            ff01_layout.addWidget(self.ff01_format)
            layout.addWidget(ff01)

            replay = QGroupBox("錄製回放")
            replay_layout = QHBoxLayout(replay)
            self.replay_btn = QPushButton("▶ 回放")
            self.replay_btn.clicked.connect(self._not_wired_yet)
            replay_layout.addWidget(self.replay_btn)
            replay_layout.addWidget(QLabel("速度:"))
            self.replay_speed = QComboBox()
            self.replay_speed.addItems(["0.25x", "0.5x", "1x", "2x", "4x", "8x"])
            self.replay_speed.setCurrentText("1x")
            replay_layout.addWidget(self.replay_speed)
            self.replay_slider = QSlider(Qt.Horizontal)
            replay_layout.addWidget(self.replay_slider, 1)
            replay_layout.addWidget(QLabel("0 / 0"))
            replay_layout.addWidget(QLabel("錄製 0"))
            layout.addWidget(replay)

            data_split = QSplitter(Qt.Horizontal)
            self.monitor_table = QTableWidget(0, 10)
            self.monitor_table.setHorizontalHeaderLabels(
                ["Time", "RID", "Frame", "Contact", "X", "Y", "Tip", "Pressure", "Scan Δ", "RAW"]
            )
            self.monitor_table.verticalHeader().setVisible(False)
            self.monitor_table.setAlternatingRowColors(True)
            data_split.addWidget(self._wrap_with_title("Monitor Data", self.monitor_table))

            self.touch_canvas = TouchCanvas()
            self.canvas_panel = self._wrap_with_title("Touch Canvas", self.touch_canvas)
            data_split.addWidget(self.canvas_panel)
            data_split.setSizes([760, 380])
            layout.addWidget(data_split, 1)

            return page

        def _build_send_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)

            params = QGroupBox("發送參數")
            grid = QGridLayout(params)
            grid.addWidget(QLabel("Report 類型:"), 0, 0)
            grid.addWidget(QRadioButton("Output"), 0, 1)
            grid.addWidget(QRadioButton("Feature"), 0, 2)
            grid.addWidget(QRadioButton("Input"), 0, 3)
            grid.addWidget(QLabel("Report ID (hex):"), 1, 0)
            grid.addWidget(QLineEdit("01"), 1, 1)
            grid.addWidget(QLabel("Data (hex):"), 2, 0)
            grid.addWidget(QLineEdit(), 2, 1, 1, 3)
            layout.addWidget(params)

            actions = QHBoxLayout()
            send_btn = QPushButton("發送 (Set Report)")
            send_btn.clicked.connect(self._not_wired_yet)
            actions.addWidget(send_btn)
            get_btn = QPushButton("Get Report")
            get_btn.clicked.connect(self._not_wired_yet)
            actions.addWidget(get_btn)
            actions.addStretch(1)
            layout.addLayout(actions)

            log = QTextEdit()
            log.setReadOnly(True)
            log.setPlaceholderText("Qt shell log")
            layout.addWidget(self._wrap_with_title("操作記錄", log), 1)
            return page

        def _build_stress_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)
            layout.addWidget(self._simple_group("壓測後發送指令"))
            layout.addWidget(self._simple_group("壓測設定"))
            layout.addWidget(self._simple_group("定時讀取指令"))

            stats = QGroupBox("統計")
            stats_layout = QHBoxLayout(stats)
            for label, value in (("總次數:", "0"), ("失敗:", "0"), ("通過:", "0"), ("已運行:", "0.0s"), ("狀態:", "就緒")):
                stats_layout.addWidget(QLabel(label))
                v = QLabel(value)
                v.setObjectName("stat-value")
                stats_layout.addWidget(v)
            stats_layout.addStretch(1)
            layout.addWidget(stats)

            log = QTextEdit()
            log.setReadOnly(True)
            layout.addWidget(self._wrap_with_title("壓測記錄", log), 1)
            return page

        def _build_replay_tab(self) -> QWidget:
            tabs = QTabWidget()
            tabs.addTab(self._build_differ_tab(), "Differ")
            tabs.addTab(self._build_digi_tab(), "DigiInfo")
            return tabs

        def _build_differ_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)
            layout.addWidget(self._file_row("Differ 資料來源", "TXT 檔案:"))
            layout.addWidget(self._simple_group("熱圖設定"))
            canvas = TouchCanvas()
            layout.addWidget(self._wrap_with_title("Differ Heatmap Preview", canvas), 1)
            return page

        def _build_digi_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)
            layout.addWidget(self._file_row("DigiInfo 資料來源", "XML 檔案:"))
            layout.addWidget(self._simple_group("顯示設定"))
            canvas = TouchCanvas()
            layout.addWidget(self._wrap_with_title("DigiInfo Canvas", canvas), 1)
            return page

        def refresh_devices(self):
            self._devices = enumerate_hid_devices()
            self.monitor_combo.blockSignals(True)
            self.monitor_combo.clear()
            for dev in self._devices:
                self.monitor_combo.addItem(format_device_label(dev))
            self.monitor_combo.blockSignals(False)

            self.command_combo.clear()
            self.command_combo.addItem(CMD_SAME_LABEL)
            for dev in self._devices:
                self.command_combo.addItem(format_device_label(dev))

            self.status_label.setText(f"找到 {len(self._devices)} 個 HID 裝置")
            if self._devices:
                self.monitor_combo.setCurrentIndex(0)
                self._on_monitor_selected(0)
            else:
                self._selected_dev = None
                self.descriptor_tree.clear()

        def _on_monitor_selected(self, index: int):
            if index < 0 or index >= len(self._devices):
                return
            self._selected_dev = self._devices[index]
            self.status_label.setText("讀取 Descriptor...")
            path = self._selected_dev.get("path", b"")
            raw = read_descriptor_via_hidapi(path)
            self._raw_descriptor = raw
            self._fields = parse_report_descriptor(raw) if raw else []
            self._populate_descriptor_tree()
            self._update_report_id_combo()
            self.status_label.setText(
                f"Descriptor 欄位 {len(self._fields)} 個"
                if raw else "無法讀取 Descriptor（可能需要管理員權限）"
            )

        def _populate_descriptor_tree(self):
            self.descriptor_tree.clear()
            if not self._fields:
                QTreeWidgetItem(self.descriptor_tree, ["（無 Descriptor 資料）", "", ""])
                return

            by_rid = {}
            for field in self._fields:
                by_rid.setdefault(field.report_id, {}).setdefault(field.report_type, []).append(field)

            for rid in sorted(by_rid):
                rid_node = QTreeWidgetItem(self.descriptor_tree, [f"Report ID = 0x{rid:02X}", "", ""])
                self.descriptor_tree.addTopLevelItem(rid_node)
                rid_node.setExpanded(True)
                for report_type in sorted(by_rid[rid]):
                    type_node = QTreeWidgetItem(rid_node, [report_type, "", ""])
                    type_node.setExpanded(True)
                    for field in by_rid[rid][report_type]:
                        logical = f"{field.logical_min} ~ {field.logical_max}"
                        item = QTreeWidgetItem(type_node, [field.label, str(field.bit_size), logical])
                        if field.is_vendor:
                            item.setForeground(0, QBrush(QColor("#b45309")))
                        if field.is_const:
                            item.setForeground(0, QBrush(QColor("#94a3b8")))

            self.descriptor_tree.expandToDepth(1)

        def _update_report_id_combo(self):
            self.rid_combo.clear()
            self.rid_combo.addItem("全部")
            rids = sorted({field.report_id for field in self._fields})
            for rid in rids:
                self.rid_combo.addItem(f"0x{rid:02X}")

        def show_raw_descriptor(self):
            if not self._raw_descriptor:
                QMessageBox.information(self, "Report Descriptor Bytes", "目前沒有 Descriptor bytes。")
                return
            text = " ".join(f"{b:02X}" for b in self._raw_descriptor)
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Report Descriptor Bytes")
            dlg.setText(f"共 {len(self._raw_descriptor)} bytes")
            dlg.setDetailedText(text)
            dlg.exec()

        def _toggle_canvas_panel(self):
            visible = self.canvas_panel.isVisible()
            self.canvas_panel.setVisible(not visible)
            self.canvas_btn.setText("顯示畫布 ▶" if visible else "隱藏畫布 ◀")

        def _not_wired_yet(self):
            QMessageBox.information(
                self,
                "Qt Shell",
                "這個功能還在 Tkinter 版中運作；Qt 版會分階段接回。",
            )

        def _build_status_bar(self, root_layout: QVBoxLayout):
            status = QFrame()
            status.setObjectName("statusbar")
            layout = QHBoxLayout(status)
            layout.setContentsMargins(10, 4, 10, 4)
            self.status_label = QLabel("就緒")
            layout.addWidget(self.status_label, 1)
            layout.addWidget(QLabel(f"{APP_VERSION_LABEL} | by {APP_AUTHOR}"))
            root_layout.addWidget(status)

        def _field_label(self, text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("field-label")
            return label

        def _section_frame(self, title: str) -> QGroupBox:
            frame = QGroupBox(title)
            frame.setObjectName("section")
            return frame

        def _wrap_with_title(self, title: str, widget: QWidget) -> QGroupBox:
            box = self._section_frame(title)
            layout = QVBoxLayout(box)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.addWidget(widget)
            return box

        def _simple_group(self, title: str) -> QGroupBox:
            box = self._section_frame(title)
            layout = QHBoxLayout(box)
            layout.addWidget(QLabel("Qt shell placeholder"))
            layout.addStretch(1)
            return box

        def _file_row(self, title: str, label_text: str) -> QGroupBox:
            box = self._section_frame(title)
            layout = QHBoxLayout(box)
            layout.addWidget(QLabel(label_text))
            layout.addWidget(QLineEdit(), 1)
            browse = QPushButton("瀏覽…")
            browse.clicked.connect(self._not_wired_yet)
            layout.addWidget(browse)
            load = QPushButton("載入")
            load.clicked.connect(self._not_wired_yet)
            layout.addWidget(load)
            return box

        def _apply_style(self):
            self.setStyleSheet(
                """
                QMainWindow, QWidget {
                    background: #eef2f7;
                    color: #111827;
                    font-family: "Microsoft JhengHei UI";
                    font-size: 9pt;
                }
                #topbar, #statusbar {
                    background: #ffffff;
                    border-bottom: 1px solid #cbd5e1;
                }
                #statusbar {
                    border-top: 1px solid #cbd5e1;
                    border-bottom: none;
                }
                #app-title {
                    font-size: 14pt;
                    font-weight: 700;
                }
                #subtitle, #field-label {
                    color: #64748b;
                    font-weight: 600;
                }
                QLabel#chip-neutral, QLabel#chip-ok, QLabel#chip-err {
                    border-radius: 4px;
                    padding: 3px 8px;
                    font-family: Consolas;
                    font-weight: 700;
                }
                QLabel#chip-neutral {
                    color: #1e3a8a;
                    background: #eef2ff;
                    border: 1px solid #c7d2fe;
                }
                QLabel#chip-ok {
                    color: #166534;
                    background: #ecfdf3;
                    border: 1px solid #bbf7d0;
                }
                QLabel#chip-err {
                    color: #b91c1c;
                    background: #fff1f2;
                    border: 1px solid #fecdd3;
                }
                QGroupBox {
                    background: #ffffff;
                    border: 1px solid #cbd5e1;
                    border-radius: 6px;
                    margin-top: 12px;
                    padding-top: 8px;
                    font-weight: 700;
                    color: #64748b;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                    background: #ffffff;
                }
                QComboBox, QLineEdit, QSpinBox, QTextEdit, QTableWidget, QTreeWidget {
                    background: #ffffff;
                    border: 1px solid #cbd5e1;
                    border-radius: 4px;
                    padding: 3px;
                }
                QPushButton {
                    background: #f8fafc;
                    border: 1px solid #cbd5e1;
                    border-radius: 4px;
                    padding: 5px 10px;
                }
                QPushButton:hover {
                    background: #f1f5f9;
                }
                QPushButton#start-button {
                    background: #16a34a;
                    color: white;
                    border: 1px solid #15803d;
                    font-weight: 700;
                    padding-left: 14px;
                    padding-right: 14px;
                }
                QTabWidget::pane {
                    border: 0;
                    background: #ffffff;
                }
                QTabBar::tab {
                    background: #e2e8f0;
                    color: #64748b;
                    padding: 8px 20px;
                    margin-right: 2px;
                    border-top-left-radius: 4px;
                    border-top-right-radius: 4px;
                }
                QTabBar::tab:selected {
                    background: #ffffff;
                    color: #2563eb;
                    font-weight: 700;
                }
                QHeaderView::section {
                    background: #f1f5f9;
                    color: #334155;
                    border: 0;
                    border-right: 1px solid #cbd5e1;
                    padding: 5px;
                    font-weight: 700;
                }
                #stat-value {
                    font-family: Consolas;
                    font-weight: 700;
                }
                """
            )


def main() -> int:
    if QT_IMPORT_ERROR is not None:
        print("PySide6 is required for hid_tool_qt.py.")
        print("Install it with: pip install PySide6")
        print(f"Import error: {QT_IMPORT_ERROR}")
        return 1

    app = QApplication(sys.argv)
    win = HIDToolQt()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
