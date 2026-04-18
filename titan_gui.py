import sys
import json
import time
import os
import threading
import pyqtgraph as pg
import logging
from PySide6.QtWidgets import (QApplication, QVBoxLayout, QGridLayout, QFrame, QFileDialog,
                               QWidget, QLabel, QSpinBox, QHBoxLayout, QPushButton, QTableWidget,
                               QAbstractItemView, QHeaderView, QGroupBox, QSlider, QCheckBox,
                               QLineEdit, QScrollArea, QComboBox, QDoubleSpinBox, QTableWidgetItem,
                               QMessageBox, QTextEdit, QDialog)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QFile, QTimer, Qt, QEvent, QObject, Signal
from PySide6.QtGui import QColor, QFont, QBrush

from titan_widgets import DMXGridOverlay, FixturePatchWidget

logger = logging.getLogger("TitanEngine")


# Change the order so QObject is first
class QtLogHandler(QObject, logging.Handler):
    new_log = Signal(str)

    def __init__(self):
        # Initialize them in the correct order
        QObject.__init__(self)
        logging.Handler.__init__(self)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.new_log.emit(msg)
        except Exception:
            self.handleError(record)


class DebugWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Titan Engine - Live Debug Log")
        self.resize(700, 400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #00ff00;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
                padding: 10px;
                border: none;
            }
        """)
        layout.addWidget(self.txt_log)

    def append_log(self, msg):
        self.txt_log.append(msg)
        scrollbar = self.txt_log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class TitanQtGUI(QObject):
    # Single source of truth for what params an effect-preset .json file
    # contains on save and consumes on load. Patch-level params (network,
    # per-fixture address, color macros, etc.) are explicitly excluded.
    PRESET_WHITELIST = (
        "hip", "lop", "env", "input_trim", "noise_gate", "mute",
        "skew", "width",
        "drive", "knee", "expand", "floor", "ceiling", "gamma", "scale", "eq_tilt", "led_gamma",
        "atk_c", "rel_c", "atk_e", "rel_e", "time_gamma",
        "jitter_on", "jitter_thresh", "jitter_amount", "dmx_smooth_on", "smooth_size",
        "glitch_digi_amt", "glitch_digi_block", "glitch_ana_amt", "glitch_ana_tear", "glitch_ana_noise",
        "od_thresh", "od_desat", "od_glitch", "link_all_dynamics",
    )

    def __init__(self, params, slider_cfg, engine, app_state, callbacks):
        super().__init__()
        self.params = params
        self.slider_cfg = slider_cfg
        self.engine = engine
        self.app_state = app_state
        self.callbacks = callbacks
        self.last_rendered_dmx = [-1] * 512
        # Per-pixel cache for the RGBW Pixels view. Holds the last (sr, sg, sb)
        # tuple rendered into each cell so we can skip setStyleSheet() when the
        # pixel is unchanged. setStyleSheet is enormously expensive in Qt and
        # was being called on every cell every frame. Sized in rebuild_dmx_grid.
        self.last_rendered_pixel = []
        self.dyn_widgets = {}

        loader = QUiLoader()
        ui_file = QFile("titan_layout.ui")
        if not ui_file.open(QFile.ReadOnly): sys.exit(1)
        self.ui = loader.load(ui_file)
        ui_file.close()

        # Hide obsolete Base Mix UI elements automatically if they exist
        for widget_name in ["sld_base_mix", "spin_base_mix", "lbl_base_mix"]:
            if hasattr(self.ui, widget_name):
                getattr(self.ui, widget_name).hide()

        self.debug_window = DebugWindow()
        self.qt_logger = QtLogHandler()
        self.qt_logger.new_log.connect(self.debug_window.append_log)
        self.qt_logger.setFormatter(
            logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%H:%M:%S'))
        logging.getLogger("TitanEngine").addHandler(self.qt_logger)

        if hasattr(self.ui, 'lbl_title_osc_out'): self.ui.lbl_title_osc_out.hide()
        if hasattr(self.ui, 'lbl_stat_osc_out'): self.ui.lbl_stat_osc_out.hide()
        if hasattr(self.ui, 'btn_toggle_osc'): self.ui.btn_toggle_osc.hide()
        if hasattr(self.ui, 'label_1'): self.ui.label_1.hide()
        if hasattr(self.ui, 'txt_osc_ip'): self.ui.txt_osc_ip.hide()
        if hasattr(self.ui, 'label_19'): self.ui.label_19.hide()
        if hasattr(self.ui, 'spin_osc_out_port'): self.ui.spin_osc_out_port.hide()
        if hasattr(self.ui, 'label_18'): self.ui.label_18.hide()
        if hasattr(self.ui, 'txt_osc_path'): self.ui.txt_osc_path.hide()

        if hasattr(self.ui, 'btn_save_config'): self.ui.btn_save_config.hide()
        if hasattr(self.ui, 'btn_load_config'): self.ui.btn_load_config.hide()
        if hasattr(self.ui, 'btn_save_default'): self.ui.btn_save_default.hide()

        if hasattr(self.ui, 'label_20'): self.ui.label_20.setText("Audio Data Port (PD):")

        self._setup_menubar()
        self._setup_scope()
        self._setup_dmx_grid()
        self._setup_fixture_preview()
        self._setup_protocol_ui()
        self._setup_artpoll_ui()
        self._setup_dynamic_patch()
        self._setup_color_mixer()
        self._setup_bg_color_mixer()
        self._setup_master_fade_ui()
        self._setup_overdrive_ui()
        self._setup_performance_monitors()
        self._setup_remote_info()
        self._setup_skew_ui()
        self._setup_mute_ui()
        self._setup_audio_device_selector()
        self._setup_panic_button()
        self._link_widgets()

        # Intercept the main window's Close event so we can prompt the gaffer
        # to confirm when Art-Net is still transmitting — filter installed in
        # eventFilter() below.
        self.ui.installEventFilter(self)

        if hasattr(self.ui, 'lbl_stat_audio'):
            self.ui.lbl_stat_audio.installEventFilter(self)
            self.ui.lbl_stat_audio.setCursor(Qt.PointingHandCursor)
        if hasattr(self.ui, 'lbl_stat_artnet'):
            self.ui.lbl_stat_artnet.installEventFilter(self)
            self.ui.lbl_stat_artnet.setCursor(Qt.PointingHandCursor)
        if hasattr(self.ui, 'lbl_stat_osc_in'):
            self.ui.lbl_stat_osc_in.installEventFilter(self)
            self.ui.lbl_stat_osc_in.setCursor(Qt.PointingHandCursor)

        if hasattr(self.ui, 'sld_master_inhibitive'):
            self.ui.sld_master_inhibitive.setStyleSheet("""
                QSlider::groove:vertical { background: #2b2b2b; width: 24px; border-radius: 4px; }
                QSlider::handle:vertical { background: #00ff66; height: 35px; width: 80px; margin: 0 -28px; border-radius: 6px; border: 2px solid #111; }
            """)

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_logic)
        self.timer.start(33)
        self.ui.show()

        # Silent autosave once a minute. Writes the live params to
        # titan_autosave.json so the gaffer can recover state after a crash
        # or forced quit without overwriting their saved default patch.
        self._autosave_timer = QTimer()
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(60000)

        QTimer.singleShot(600, self._show_startup_popup)

    def _setup_mute_ui(self):
        self.ui.chk_mute = QCheckBox("Mute Audio")
        self.ui.chk_mute.setStyleSheet("color: #ff5555; font-weight: bold;")

        if hasattr(self.ui, 'InputState') and self.ui.InputState.layout():
            # insertWidget(0, ...) puts it at the absolute top of the layout
            self.ui.InputState.layout().insertWidget(0, self.ui.chk_mute)

    _PANIC_BUTTON_IDLE_STYLE = (
        "QPushButton { background-color: #cc0000; color: white; font-weight: bold; "
        "font-size: 14px; padding: 12px; border: 2px solid #660000; border-radius: 4px; } "
        "QPushButton:hover { background-color: #ff0000; } "
        "QPushButton:pressed { background-color: #880000; }"
    )
    _PANIC_BUTTON_ACTIVE_STYLE = (
        "QPushButton { background-color: #000; color: #ff5555; font-weight: bold; "
        "font-size: 14px; padding: 12px; border: 3px solid #ff0000; border-radius: 4px; }"
    )

    def _setup_panic_button(self):
        if not hasattr(self.ui, 'box_status'):
            logger.warning("box_status not found; Panic Blackout button not attached.")
            return
        layout = self.ui.box_status.layout()
        if layout is None:
            logger.warning("box_status has no layout; Panic Blackout button not attached.")
            return
        self.btn_panic = QPushButton("PANIC BLACKOUT")
        self.btn_panic.setStyleSheet(self._PANIC_BUTTON_IDLE_STYLE)
        self.btn_panic.setMinimumHeight(50)
        self.btn_panic.setCursor(Qt.PointingHandCursor)
        self.btn_panic.clicked.connect(self.toggle_panic_blackout)
        layout.addWidget(self.btn_panic)

    def toggle_panic_blackout(self):
        active = not bool(self.app_state.get("panic_blackout", False))
        self.app_state["panic_blackout"] = active
        if hasattr(self, 'btn_panic'):
            if active:
                self.btn_panic.setText("BLACKOUT ACTIVE — CLICK TO RESTORE")
                self.btn_panic.setStyleSheet(self._PANIC_BUTTON_ACTIVE_STYLE)
            else:
                self.btn_panic.setText("PANIC BLACKOUT")
                self.btn_panic.setStyleSheet(self._PANIC_BUTTON_IDLE_STYLE)
        logger.warning(f"PANIC BLACKOUT {'ENGAGED' if active else 'RELEASED'}")

    def _autosave(self):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "titan_autosave.json")
            with open(path, "w") as f:
                json.dump(self.params, f, indent=4)
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"Autosave to titan_autosave.json failed: {e}")

    @staticmethod
    def _coerce_dev_id(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _parse_pd_device_list(self, raw, section="input"):
        """Parse `pd -listdev` text into [(device_id, name), ...] for one
        section. `section` is "input" or "output". IDs are 1-indexed to
        match PD's own numbering. Returns [] if the blob is empty or no
        entries are found in the requested section."""
        import re
        if not raw:
            return []
        target = "audio output" if section == "output" else "audio input"
        devices = []
        in_section = False
        for line in raw.splitlines():
            stripped = line.strip().lower()
            if target in stripped and "device" in stripped:
                in_section = True
                continue
            # Any OTHER section header ends ours. Listing every possible
            # header avoids breaking when pd adds a new section in future.
            if in_section and stripped.endswith("devices:"):
                if target not in stripped:
                    break
            if not in_section:
                continue
            m = re.match(r"\s*(\d+)\.\s+(.+?)\s*$", line)
            if m:
                try:
                    devices.append((int(m.group(1)), m.group(2).strip()))
                except ValueError:
                    continue
        return devices

    def _populate_device_combo(self, combo, devices, saved_id):
        combo.clear()
        combo.addItem("(Pure Data default)", userData=None)
        for dev_id, name in devices:
            combo.addItem(f"{dev_id}: {name}", userData=dev_id)
        if saved_id is not None:
            idx = combo.findData(saved_id)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _setup_audio_device_selector(self):
        watchdog = self.callbacks.get("watchdog") if self.callbacks else None
        if watchdog is None:
            logger.warning("No watchdog in callbacks; Audio Input Selector disabled.")
            return
        self._watchdog = watchdog

        try:
            raw = watchdog.get_pd_audio_devices()
        except Exception as e:
            logger.error(f"Failed to scan PD audio devices: {e}")
            raw = ""
        in_devices = self._parse_pd_device_list(raw, "input")
        out_devices = self._parse_pd_device_list(raw, "output")
        if not in_devices:
            logger.warning("No Pure Data audio input devices were detected.")

        box = QGroupBox("Audio Input Device")
        box.setStyleSheet("QGroupBox { font-weight: bold; }")
        v = QVBoxLayout(box)

        # Inline status indicator — mirrors the Status-box audio indicator so
        # the gaffer can tell at a glance whether audio is flowing while they
        # pick a device. Updated in refresh_logic alongside lbl_stat_audio.
        row_stat = QHBoxLayout()
        row_stat.addWidget(QLabel("Audio Input:"))
        self.lbl_stat_audio_inline = QLabel("🔴 WAIT")
        self.lbl_stat_audio_inline.setStyleSheet("color: #ff0000; font-weight: bold;")
        row_stat.addWidget(self.lbl_stat_audio_inline)
        row_stat.addStretch(1)
        v.addLayout(row_stat)

        row_in = QHBoxLayout()
        row_in.addWidget(QLabel("Input:"))
        self.cmb_audio_dev = QComboBox()
        self._populate_device_combo(
            self.cmb_audio_dev, in_devices,
            self._coerce_dev_id(self.params.get("pd_audio_dev")),
        )
        row_in.addWidget(self.cmb_audio_dev, 1)

        btn_rescan = QPushButton("🔄 Rescan")
        btn_rescan.clicked.connect(self._rescan_audio_devices)
        row_in.addWidget(btn_rescan)

        self.cmb_audio_dev.currentIndexChanged.connect(self._on_audio_device_changed)
        v.addLayout(row_in)

        # Output device row. Critical for BlackHole-as-input setups: without
        # an explicit output pick, PD may route its tone/monitor into the
        # virtual loopback and nothing reaches real speakers.
        row_out = QHBoxLayout()
        row_out.addWidget(QLabel("Output:"))
        self.cmb_audio_dev_out = QComboBox()
        self._populate_device_combo(
            self.cmb_audio_dev_out, out_devices,
            self._coerce_dev_id(self.params.get("pd_audio_dev_out")),
        )
        row_out.addWidget(self.cmb_audio_dev_out, 1)
        self.cmb_audio_dev_out.currentIndexChanged.connect(self._on_audio_output_device_changed)
        v.addLayout(row_out)

        target_layout = None
        if hasattr(self.ui, 'tab_audio_mapping') and self.ui.tab_audio_mapping.layout():
            target_layout = self.ui.tab_audio_mapping.layout()
        if target_layout is not None:
            target_layout.insertWidget(0, box)
        else:
            logger.warning("tab_audio_mapping has no layout; device selector not attached.")

    def _on_audio_device_changed(self, _idx):
        self.params["pd_audio_dev"] = self.cmb_audio_dev.currentData()
        self._restart_pd_with_current_devices()

    def _on_audio_output_device_changed(self, _idx):
        self.params["pd_audio_dev_out"] = self.cmb_audio_dev_out.currentData()
        self._restart_pd_with_current_devices()

    def _restart_pd_with_current_devices(self):
        if getattr(self, "_watchdog", None) is None:
            return
        in_id = self.cmb_audio_dev.currentData() if hasattr(self, 'cmb_audio_dev') else None
        out_id = self.cmb_audio_dev_out.currentData() if hasattr(self, 'cmb_audio_dev_out') else None
        try:
            self._watchdog.start_engine(device_id=in_id, output_device_id=out_id)
            logger.info(f"Pure Data restarted (in={in_id}, out={out_id}).")
        except Exception as e:
            logger.error(f"Failed to restart Pure Data (in={in_id}, out={out_id}): {e}")
            return
        # The fresh PD process comes up with patch-hardcoded defaults for
        # hip/lop/env/test_*/mute. Re-push the user's current params once the
        # new PD has had time to open its OSC listener, otherwise audio stays
        # silent until the user wiggles a control. Deferred via QTimer so we
        # don't block the Qt event loop.
        push_init = self.callbacks.get("push_pd_init") if self.callbacks else None
        if push_init is not None:
            QTimer.singleShot(1500, push_init)

    def _rescan_audio_devices(self):
        if getattr(self, "_watchdog", None) is None:
            return
        try:
            raw = self._watchdog.get_pd_audio_devices()
        except Exception as e:
            logger.error(f"Rescan failed: {e}")
            return
        in_devs = self._parse_pd_device_list(raw, "input")
        out_devs = self._parse_pd_device_list(raw, "output")
        for combo, devs in ((self.cmb_audio_dev, in_devs),
                            (self.cmb_audio_dev_out, out_devs)):
            current_id = combo.currentData()
            combo.blockSignals(True)
            self._populate_device_combo(combo, devs, current_id)
            combo.blockSignals(False)
        logger.info(f"Audio devices rescanned ({len(in_devs)} input, {len(out_devs)} output).")

    def _show_audio_device_popup(self):
        """Quick device-picker popup bound to the middle-section status dot.
        A fresh combo is populated from the main input combo's current items;
        selecting in the popup drives the main combo, which triggers the usual
        restart-PD flow — no duplicate routing logic."""
        if not hasattr(self, 'cmb_audio_dev'):
            return
        dlg = QDialog(self.ui)
        dlg.setWindowTitle("Audio Input Device")
        dlg.setMinimumWidth(400)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Select an audio input device:"))
        popup_combo = QComboBox()
        for i in range(self.cmb_audio_dev.count()):
            popup_combo.addItem(self.cmb_audio_dev.itemText(i),
                                userData=self.cmb_audio_dev.itemData(i))
        popup_combo.setCurrentIndex(self.cmb_audio_dev.currentIndex())
        popup_combo.currentIndexChanged.connect(self.cmb_audio_dev.setCurrentIndex)
        lay.addWidget(popup_combo)
        btn = QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec()

    def _show_startup_popup(self):
        # If they already have a default patch saved, skip the tutorial!
        default_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "titan_default.json")
        if os.path.exists(default_file):
            return

        msg = QMessageBox(self.ui)
        msg.setWindowTitle("Welcome to Titan Engine")
        msg.setText("<b>Initial Setup Instructions:</b><br><br>"
                    "1. Go to <b>File -> 📂 Load Patch</b> to load your fixture configuration.<br>"
                    "2. Go to the <b>Control</b> menu (or the Output tab) and toggle Art-Net / sACN to <b>ON</b>.<br><br>"
                    "<i>Note: If you run into issues, click any Red or Yellow status indicator for troubleshooting steps!</i>")
        msg.setIcon(QMessageBox.Information)
        msg.exec()

    def eventFilter(self, obj, event):
        # Confirm-before-quit: if the user closes the main window while
        # Art-Net is still transmitting, warn them first. aboutToQuit is
        # already wired to cleanup_pd in main, so accepting the close is
        # enough — we don't need to call cleanup ourselves.
        if obj is self.ui and event.type() == QEvent.Close:
            if self.app_state.get("artnet_active", False):
                reply = QMessageBox.question(
                    self.ui,
                    "Confirm Quit",
                    "Output is active. Are you sure you want to quit?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return True

        if event.type() == QEvent.MouseButtonPress:
            if obj == getattr(self.ui, 'lbl_stat_audio', None):
                self._show_audio_device_popup()
                return True
            elif obj == getattr(self.ui, 'lbl_stat_artnet', None):
                self._show_artnet_troubleshoot()
                return True
            elif obj == getattr(self.ui, 'lbl_stat_osc_in', None):
                self._show_ctrl_troubleshoot()
                return True
            elif obj == getattr(self, 'lbl_stat_test', None):
                self._show_test_troubleshoot()
                return True

        # This MUST sit outside the MouseButtonPress block!
        return super().eventFilter(obj, event)

    def _show_artnet_troubleshoot(self):
        is_active = self.app_state.get("artnet_active", False)
        packets = self.app_state.get("art_packets", 0)
        err = self.app_state.get("send_error")

        if self.app_state.get("port_conflict_artnet"):
            QMessageBox.critical(self.ui, "Port Conflict Detected",
                                 "Another software (like QLC+, sACNView, or Protokol) has taken exclusive control of the Art-Net port (6454).\n\n"
                                 "To fix this, close the conflicting software and restart Titan Engine.")
        elif not is_active:
            QMessageBox.warning(self.ui, "Network Output Disabled",
                                "Network output is currently toggled OFF.\n\n"
                                "Click the 'Toggle Art-Net / sACN' button on the UI (or in the Control menu) to begin broadcasting data.")
        elif packets == 0 or err:
            msg = "Network output is enabled, but no packets are leaving the engine.\n\n"
            msg += "Troubleshooting Steps:\n"
            msg += "1. Check your destination IP Address. If it is unroutable or not on your local subnet, your Operating System will silently block the traffic.\n"
            msg += "2. Check your DMX mapping or output patch configuration to ensure data is routed.\n"
            if err:
                msg += f"\nLast System Error: {err}"
            QMessageBox.warning(self.ui, "Network Output Blocked (Yellow)", msg)
        else:
            QMessageBox.information(self.ui, "Network Output",
                                    "Network output is active and transmitting packets normally.")

    def _show_ctrl_troubleshoot(self):
        txt = self.app_state.get("osc_in_text", "")
        if self.app_state.get("port_conflict_artnet"):
            QMessageBox.critical(self.ui, "Port Conflict Detected",
                                 "The DMX Control listener is broken because another software (Protokol/QLC+) is hoarding port 6454.\n\n"
                                 "Close the conflicting software and restart.")
        elif "🟢" not in txt:
            QMessageBox.warning(self.ui, "Remote Control Troubleshooting",
                                "No incoming DMX control data detected.\n\n"
                                "1. Ensure your lighting console (QLC+) is actively sending data.\n"
                                "2. Verify the Art-Net Universe coming out of QLC+ matches the 'Control Universe' set in Titan Engine.\n"
                                "3. If sending from QLC+ on the exact same computer, ensure QLC+ is outputting to the '127.0.0.1' (Loopback) adapter.")
        else:
            QMessageBox.information(self.ui, "Remote Control", "Remote control DMX data is being received normally.")

    def _show_test_troubleshoot(self):
        is_test_on = int(self.params.get("test_on", 0)) == 1
        has_remote = "🟢" in self.app_state.get("osc_in_text", "")

        if is_test_on and has_remote:
            QMessageBox.warning(self.ui, "Test Tone Override Active",
                                "⚠️ The internal audio Test Tone is currently ON!\n\n"
                                "This completely overrides any live audio coming from the microphone. "
                                "Since you are receiving remote DMX control (which usually means you are ready for the show), "
                                "you should probably turn the Test Tone OFF in the Audio tab so your lights react to the real music.")
        elif is_test_on:
            QMessageBox.information(self.ui, "Test Tone",
                                    "The Test Tone is actively generating an artificial signal for calibration.")
        else:
            QMessageBox.information(self.ui, "Test Tone",
                                    "The Test Tone is off. The engine is listening to live audio.")

    def _setup_menubar(self):
        menubar = getattr(self.ui, "menubar", None)
        if not menubar: return

        file_menu = menubar.addMenu("File")
        act_load_patch = file_menu.addAction("📂 Load Patch")
        act_load_patch.triggered.connect(self.load_patch)
        act_save_patch = file_menu.addAction("💾 Save Patch")
        act_save_patch.triggered.connect(self.save_patch)
        file_menu.addSeparator()
        act_load_cfg = file_menu.addAction("📂 Load Config")
        act_load_cfg.triggered.connect(self.load_config)
        act_save_cfg = file_menu.addAction("💾 Save Config")
        act_save_cfg.triggered.connect(self.save_config)
        file_menu.addSeparator()
        act_default = file_menu.addAction("⭐ Make Default")
        act_default.triggered.connect(self.save_default)

        view_menu = menubar.addMenu("View")
        act_toggle_log = view_menu.addAction("🐞 Show/Hide Debug Log")
        act_toggle_log.triggered.connect(self._toggle_debug_window)

        ctrl_menu = menubar.addMenu("Control")
        act_toggle_net = ctrl_menu.addAction("📡 Toggle Art-Net / sACN Output")
        act_toggle_net.triggered.connect(self.callbacks.get("toggle_artnet"))

        # --- NEW: Remote Control Toggle ---
        ctrl_menu.addSeparator()
        self.act_remote_lock = ctrl_menu.addAction("🎚️ Enable DMX Remote Control")
        self.act_remote_lock.setCheckable(True)
        self.act_remote_lock.setChecked(bool(self.params.get("remote_on", 1)))
        self.act_remote_lock.triggered.connect(self._on_remote_toggled)

        help_menu = menubar.addMenu("Help")
        act_guide = help_menu.addAction("📖 Audio Mapping & Setup Guide")
        act_guide.triggered.connect(self._show_audio_guide)

    def _toggle_debug_window(self):
        if self.debug_window.isVisible():
            self.debug_window.hide()
        else:
            self.debug_window.show()

    def _show_audio_guide(self):
        dialog = QDialog(self.ui)
        dialog.setWindowTitle("Audio Mapping & Dynamics Guide")
        dialog.resize(750, 700)
        dialog.setStyleSheet("background-color: #2b2b2b; color: #ffffff;")

        layout = QVBoxLayout(dialog)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet(
            "background-color: #1e1e1e; border: 1px solid #555; padding: 15px; font-size: 13px; line-height: 1.5;")

        guide_html = """
        <h2 style='color: #00ff66;'>The Golden Order of Operations</h2>
        <p>Follow these 4 steps to get a great audio-reactive effect:</p>
        <ol>
            <li><b>Set the Volume (Input Trim):</b> Play your music. Adjust the Input Trim until the squiggly line in the scope is bouncing healthily, but not completely flattened against the top of the graph.</li>
            <li><b>Pick the Instrument (High Pass & Low Pass):</b> 
                <ul>
                    <li>Want the lights to punch to the bass drum? Turn the <b>Low Pass</b> down to around 200Hz.</li>
                    <li>Want them to flash to the snare drum or vocals? Turn the <b>High Pass</b> up to around 1000Hz.</li>
                </ul>
            </li>
            <li><b>Set the Limits (Floor & Ceiling):</b> Turn the <b>Floor</b> up slightly until background noise (like crowd chatter or AC hum) stops triggering the lights. If the lights aren't getting bright enough during loud parts, lower the <b>Ceiling</b>.</li>
            <li><b>Shape the Flash (Expand & Drive):</b> Adjust <b>Expand</b> to make the difference between quiet and loud hits more dramatic. Use <b>Drive</b> to push the overall brightness of the effect up.</li>
        </ol>
        <hr style='border: 1px solid #444;'>

        <h2 style='color: #00ffff;'>1. Color & Output</h2>
        <ul>
            <li><b>Master Effect Color:</b> The color the lights turn <i>when the audio hits</i>.</li>
            <li><b>Static Background Color:</b> The "resting" color of the lights during dead silence. If you want the lights to sit at a dim blue, but flash bright white to the beat, set the Background to dim blue and the Effect Color to white.</li>
            <li><b>Output Drive:</b> A master volume pedal for the audio effect. Cranking this makes the audio reaction much brighter overall.</li>
        </ul>

        <h2 style='color: #ffaa00;'>2. Spacial Physics (The "Wave" Effect)</h2>
        <p><i>The engine draws audio like a splash in a pond—starting at the center and rippling outward.</i></p>
        <ul>
            <li><b>Center vs. Edge Timings (Attack & Release):</b> You can give the center pixels different speed rules than the outer edges. 
                <ul><li><i>Pro-Tip:</i> Set the <b>Center</b> to have a fast Attack/Release (instant flash), and the <b>Edge</b> to have a slow Release. It will look like a firework: a bright, fast explosion in the middle that leaves a slow, glowing trail on the edges.</li></ul>
            </li>
            <li><b>Time Gamma:</b> Changes how smoothly the timings blend from the center to the edge. High values make the center punchy but keep the edges sluggish.</li>
            <li><b>Center Skew:</b> Moves the physical "middle" of the splash. Instead of starting perfectly in the center of your light strip, you can push the starting point to the far left or right.</li>
            <li><b>Effect Width:</b> Controls how far the audio wave travels. A low width keeps the flash tightly clumped in the center; a high width lets it shoot all the way to the ends of the strip.</li>
        </ul>

        <h2 style='color: #ff55ff;'>3. Signal Shaping & Smoothing</h2>
        <ul>
            <li><b>Frequency Tilt (EQ Tilt):</b> A seesaw for Bass vs. Treble. Turn it left to make Bass hits visually wider on the light strip. Turn it right to make Treble hits visually wider.</li>
            <li><b>Soft Knee:</b> Smooths the harsh line between "Off" and "On". Instead of the lights snapping on abruptly the millisecond the audio crosses the threshold, a soft knee fades them in gently.</li>
            <li><b>Adaptive Jitter Reduction:</b> Imagine a light switch hovering right between on and off. Jitter reduction holds it steady so the LEDs don't violently "spazz" or flicker when a singer sustains a long note right on the edge of your volume threshold.</li>
            <li><b>DMX Temporal Smoothing:</b> The final, beautiful polish. Digital LEDs can look harsh and "steppy" when fading. This setting slightly blurs the frames together right before sending them to the lights, making digital pixels feel buttery and heavy like old-school halogen bulbs.</li>
        </ul>
        """
        txt.setHtml(guide_html)
        layout.addWidget(txt)

        btn_close = QPushButton("Got it!")
        btn_close.setStyleSheet(
            "background-color: #00ff66; color: #000; font-weight: bold; padding: 10px; border-radius: 4px; font-size: 14px;")
        btn_close.clicked.connect(dialog.accept)
        layout.addWidget(btn_close)

        dialog.exec()

    def _setup_scope(self):
        self.plot = pg.PlotWidget()
        self.plot.setBackground('#0a0a0a')
        self.plot.showGrid(x=False, y=True, alpha=0.3)
        self.plot.setYRange(0, 1.1)
        self.plot.hideAxis('bottom')
        # addLegend before plot() so each curve's name="…" auto-registers.
        self.plot.addLegend(offset=(10, 10), labelTextColor='#dddddd',
                            brush=pg.mkBrush(0, 0, 0, 160),
                            pen=pg.mkPen('#333'))

        if self.ui.scope_canvas.layout():
            self.ui.scope_canvas.layout().addWidget(self.plot)
        else:
            layout = QVBoxLayout(self.ui.scope_canvas)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.plot)

        self.curve_audio = self.plot.plot(pen=pg.mkPen('#ffaa00', width=2), name="Audio In")
        self.curve_center = self.plot.plot(pen=pg.mkPen('#00ffff', width=3), name="Center")
        self.curve_edge = self.plot.plot(pen=pg.mkPen('#ff00ff', width=3), name="Edge")

    def _setup_skew_ui(self):
        row_skew = QHBoxLayout()
        lbl_s = QLabel("Center Skew")
        lbl_s.setMinimumWidth(90)
        lbl_s.setMaximumWidth(90)
        sld_s = QSlider(Qt.Horizontal)
        setattr(self.ui, "sld_skew", sld_s)
        spin_s = QDoubleSpinBox()
        spin_s.setMinimumWidth(65)
        spin_s.setMaximumWidth(65)
        spin_s.setDecimals(2)
        setattr(self.ui, "spin_skew", spin_s)
        row_skew.addWidget(lbl_s)
        row_skew.addWidget(sld_s)
        row_skew.addWidget(spin_s)

        row_width = QHBoxLayout()
        lbl_w = QLabel("Effect Width")
        lbl_w.setMinimumWidth(90)
        lbl_w.setMaximumWidth(90)
        sld_w = QSlider(Qt.Horizontal)
        setattr(self.ui, "sld_width", sld_w)
        spin_w = QDoubleSpinBox()
        spin_w.setMinimumWidth(65)
        spin_w.setMaximumWidth(65)
        spin_w.setDecimals(2)
        setattr(self.ui, "spin_width", spin_w)
        row_width.addWidget(lbl_w)
        row_width.addWidget(sld_w)
        row_width.addWidget(spin_w)

        if hasattr(self.ui, 'verticalLayout_5'):
            self.ui.verticalLayout_5.addLayout(row_skew)
            self.ui.verticalLayout_5.addLayout(row_width)

        if hasattr(self.ui, 'SpacialPhysics'):
            self.ui.SpacialPhysics.setMaximumHeight(16777215)
            self.ui.SpacialPhysics.setMinimumHeight(180)

    def _setup_master_fade_ui(self):
        box = QGroupBox("Master Fade Timing")
        box.setStyleSheet("QGroupBox { font-weight: bold; }")
        layout = QVBoxLayout(box)

        for name, param in [("Fade Up (ms)", "dimmer_atk"), ("Fade Down (ms)", "dimmer_rel")]:
            row = QHBoxLayout()
            lbl = QLabel(name)
            lbl.setMinimumWidth(100)
            lbl.setStyleSheet("font-weight: normal;")
            row.addWidget(lbl)

            sld = QSlider(Qt.Horizontal)
            setattr(self.ui, f"sld_{param}", sld)
            row.addWidget(sld)

            spin = QSpinBox()
            spin.setRange(1, 5000)
            setattr(self.ui, f"spin_{param}", spin)
            row.addWidget(spin)
            layout.addLayout(row)

        if self.ui.box_master_fader.parentWidget().layout():
            self.ui.box_master_fader.parentWidget().layout().insertWidget(2, box)

    def _setup_overdrive_ui(self):
        for old_widget in ["chk_master_glitch_bypass", "chk_master_glitch_force", "box_glitch_master"]:
            if hasattr(self.ui, old_widget):
                getattr(self.ui, old_widget).setVisible(False)

        box = QGroupBox("Audio Distortion (White-Hot Overdrive)")
        box.setStyleSheet("QGroupBox { font-weight: bold; border: 1px solid #777; margin-top: 10px;}")
        layout = QVBoxLayout(box)

        row_f = QHBoxLayout()
        self.chk_master_od_force_off = QCheckBox("Global Force Off")
        self.chk_master_od_force_off.setChecked(bool(self.params.get("master_od_force_off", 0)))

        self.chk_master_od_force_on = QCheckBox("Global Force On")
        self.chk_master_od_force_on.setChecked(bool(self.params.get("master_od_force_on", 0)))

        self.chk_master_od_force_off.toggled.connect(lambda v: self._update_chk_force_exclusive(
            "master_od_force_off", "master_od_force_on", self.chk_master_od_force_on, v))
        self.chk_master_od_force_on.toggled.connect(lambda v: self._update_chk_force_exclusive(
            "master_od_force_on", "master_od_force_off", self.chk_master_od_force_off, v))

        row_f.addWidget(self.chk_master_od_force_off)
        row_f.addWidget(self.chk_master_od_force_on)
        layout.addLayout(row_f)

        for name, param in [("Threshold", "od_thresh"), ("Desaturation", "od_desat"), ("Glitch Boost", "od_glitch")]:
            row = QHBoxLayout()
            row.addWidget(QLabel(name))
            sld = QSlider(Qt.Horizontal)
            sld.setRange(0, 100)
            setattr(self.ui, f"sld_{param}", sld)
            spin = QDoubleSpinBox()
            spin.setRange(0, 1)
            spin.setSingleStep(0.01)
            setattr(self.ui, f"spin_{param}", spin)
            row.addWidget(sld)
            row.addWidget(spin)
            layout.addLayout(row)

        if self.ui.tab_fx_glitch.layout():
            self.ui.tab_fx_glitch.layout().insertWidget(0, box)

        f_box = QGroupBox("Global Effect Overrides")
        f_box.setStyleSheet("QGroupBox { font-weight: bold; border: 1px solid #777; margin-top: 10px;}")
        f_lay = QGridLayout(f_box)

        self.chk_master_ana_force_off = QCheckBox("Force Static Noise Off")
        self.chk_master_ana_force_off.setChecked(bool(self.params.get("master_ana_force_off", 0)))

        self.chk_master_ana_force_on = QCheckBox("Force Static Noise On")
        self.chk_master_ana_force_on.setChecked(bool(self.params.get("master_ana_force_on", 0)))

        self.chk_master_ana_force_off.toggled.connect(lambda v: self._update_chk_force_exclusive(
            "master_ana_force_off", "master_ana_force_on", self.chk_master_ana_force_on, v))
        self.chk_master_ana_force_on.toggled.connect(lambda v: self._update_chk_force_exclusive(
            "master_ana_force_on", "master_ana_force_off", self.chk_master_ana_force_off, v))

        self.chk_master_digi_force_off = QCheckBox("Force Digital Glitch Off")
        self.chk_master_digi_force_off.setChecked(bool(self.params.get("master_digi_force_off", 0)))

        self.chk_master_digi_force_on = QCheckBox("Force Digital Glitch On")
        self.chk_master_digi_force_on.setChecked(bool(self.params.get("master_digi_force_on", 0)))

        self.chk_master_digi_force_off.toggled.connect(lambda v: self._update_chk_force_exclusive(
            "master_digi_force_off", "master_digi_force_on", self.chk_master_digi_force_on, v))
        self.chk_master_digi_force_on.toggled.connect(lambda v: self._update_chk_force_exclusive(
            "master_digi_force_on", "master_digi_force_off", self.chk_master_digi_force_off, v))

        f_lay.addWidget(self.chk_master_ana_force_off, 0, 0)
        f_lay.addWidget(self.chk_master_ana_force_on, 0, 1)
        f_lay.addWidget(self.chk_master_digi_force_off, 1, 0)
        f_lay.addWidget(self.chk_master_digi_force_on, 1, 1)

        if self.ui.tab_fx_glitch.layout():
            self.ui.tab_fx_glitch.layout().insertWidget(0, f_box)

    def _setup_dynamic_patch(self):
        if self.ui.tab_patch.layout():
            layout = self.ui.tab_patch.layout()
            while layout.count():
                item = layout.takeAt(0)
                if item.widget(): item.widget().deleteLater()
        else:
            layout = QVBoxLayout(self.ui.tab_patch)

        ctrl_row = QHBoxLayout()
        btn_add = QPushButton("➕ Add Fixture")
        btn_rem = QPushButton("➖ Remove Fixture")
        btn_add.clicked.connect(self.add_fixture)
        btn_rem.clicked.connect(self.remove_fixture)
        ctrl_row.addWidget(btn_add)
        ctrl_row.addWidget(btn_rem)
        ctrl_row.addStretch()

        layout.addLayout(ctrl_row)

        self.fix_scroll = QScrollArea()
        self.fix_scroll.setWidgetResizable(True)
        self.fix_container = QWidget()
        self.fix_layout = QVBoxLayout(self.fix_container)
        self.fix_layout.setAlignment(Qt.AlignTop)
        self.fix_scroll.setWidget(self.fix_container)
        layout.addWidget(self.fix_scroll)

        self.fixture_widgets = []
        num_fixes = int(self.params.get("num_fixtures", 1))
        self.params["num_fixtures"] = max(1, num_fixes)
        for i in range(1, self.params["num_fixtures"] + 1):
            self._build_fixture_ui(i)

    def _build_fixture_ui(self, f_idx):
        if f"f{f_idx}_active" not in self.params:
            self.params[f"f{f_idx}_active"] = 1
            self.params[f"f{f_idx}_extend"] = 0
            self.params[f"f{f_idx}_flip"] = 0
            self.params[f"f{f_idx}_align"] = 0
            self.params[f"f{f_idx}_glitch_digi"] = 0
            self.params[f"f{f_idx}_glitch_ana"] = 0
            self.params[f"f{f_idx}_od_en"] = 0
            self.params[f"f{f_idx}_name"] = f"Fixture {f_idx}"

            if f_idx > 1:
                prev_uni = int(self.params.get(f"f{f_idx - 1}_uni", 0))
                prev_foot = int(self.params.get(f"f{f_idx - 1}_foot", 4))
                prev_pix = int(self.params.get(f"f{f_idx - 1}_pix", 16))
                prev_addr = int(self.params.get(f"f{f_idx - 1}_addr", 1))

                self.params[f"f{f_idx}_uni"] = prev_uni
                self.params[f"f{f_idx}_foot"] = prev_foot
                self.params[f"f{f_idx}_pix"] = prev_pix

                ch_needed = prev_pix * prev_foot
                next_addr = prev_addr + ch_needed
                next_uni = prev_uni

                while next_addr > 512:
                    next_uni += 1
                    next_addr -= 512

                if next_addr + (prev_pix * prev_foot) - 1 > 512:
                    next_uni += 1
                    next_addr = 1

                self.params[f"f{f_idx}_uni"] = next_uni
                self.params[f"f{f_idx}_addr"] = next_addr
            else:
                self.params[f"f{f_idx}_uni"] = 0
                self.params[f"f{f_idx}_foot"] = 4
                self.params[f"f{f_idx}_addr"] = 1
                self.params[f"f{f_idx}_pix"] = 16

        w = FixturePatchWidget(f_idx, self.params, self._update_chk_simple, self._update_txt_simple,
                               self._update_spin_patch)
        self.fix_layout.addWidget(w)
        self.fixture_widgets.append(w)

    def _update_spin_patch(self, name, val):
        self.params[name] = val
        self._validate_preset_ch()
        if getattr(self, 'cmb_view_mode', None) and self.cmb_view_mode.currentText() == "RGBW Pixels":
            self.rebuild_dmx_grid()
        else:
            self._update_dmx_overlay()

    def _update_chk_simple(self, name, val):
        self.params[name] = 1 if val else 0
        if "send_osc" in self.callbacks:
            self.callbacks["send_osc"](name, float(self.params[name]))
        self._validate_preset_ch()
        if getattr(self, 'cmb_view_mode', None) and self.cmb_view_mode.currentText() == "RGBW Pixels":
            self.rebuild_dmx_grid()
        else:
            self._update_dmx_overlay()

    def _update_chk_force_exclusive(self, name, partner_name, partner_chk, val):
        if val and partner_chk.isChecked():
            partner_chk.blockSignals(True)
            partner_chk.setChecked(False)
            partner_chk.blockSignals(False)
            self.params[partner_name] = 0
            if "send_osc" in self.callbacks:
                self.callbacks["send_osc"](partner_name, 0.0)
        self._update_chk_simple(name, val)

    def _update_txt_simple(self, name, val):
        self.params[name] = val
        self._update_fixture_list()
        if getattr(self, 'cmb_view_mode', None) and self.cmb_view_mode.currentText() == "RGBW Pixels":
            self.rebuild_dmx_grid()
        else:
            self._update_dmx_overlay()

    def _update_txt(self, name, val):
        if getattr(self, 'live_update', True):
            self.params[name] = val

    def add_fixture(self):
        n = int(self.params.get("num_fixtures", 1)) + 1
        self.params["num_fixtures"] = n
        self._build_fixture_ui(n)
        self._update_fixture_list()
        self.rebuild_dmx_grid()

    def remove_fixture(self):
        n = int(self.params.get("num_fixtures", 1))
        if n > 1:
            self.fixture_widgets.pop().deleteLater()
            for key in ["active", "extend", "flip", "align", "name", "uni", "foot", "addr", "pix", "glitch_digi",
                        "glitch_ana", "od_en"]:
                self.params.pop(f"f{n}_{key}", None)
            self.params["num_fixtures"] = n - 1
            self._update_fixture_list()
            self.rebuild_dmx_grid()

    def save_patch(self):
        self.apply_changes()
        try:
            path, _ = QFileDialog.getSaveFileName(self.ui, "Save Patch", "", "JSON Files (*.json)")
            if path:
                if not path.lower().endswith('.json'):
                    path += '.json'

                patch_data = {"num_fixtures": self.params.get("num_fixtures", 1), "file_type": "titan_patch"}
                num_fixes = int(patch_data["num_fixtures"])
                for i in range(1, num_fixes + 1):
                    for key in ["active", "extend", "flip", "align", "name", "uni", "foot", "addr", "pix",
                                "glitch_digi", "glitch_ana", "od_en"]:
                        param_name = f"f{i}_{key}"
                        if param_name in self.params: patch_data[param_name] = self.params[param_name]

                network_keys = [
                    "protocol", "net_mode", "sacn_priority", "adv_net", "art_net", "art_sub",
                    "sacn_src", "sacn_preview", "artnet_offset", "art_ip", "art_port", "ctrl_univ", "preset_ch"
                ]
                for nk in network_keys:
                    if nk in self.params: patch_data[nk] = self.params[nk]

                with open(path, 'w') as f:
                    json.dump(patch_data, f, indent=4)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Failed to save patch file: {e}")

    def load_patch(self, filepath=None):
        if not filepath:
            filepath, _ = QFileDialog.getOpenFileName(self.ui, "Load Patch", "", "JSON Files (*.json)")

        if filepath:
            try:
                with open(filepath, 'r') as f:
                    patch_data = json.load(f)

                if patch_data.get("file_type") == "titan_preset":
                    print("⚠️ Blocked: Cannot load a Preset file into the Patch engine!")
                    return

                old_num = int(self.params.get("num_fixtures", 1))
                for i in range(1, old_num + 1):
                    for key in ["active", "extend", "flip", "align", "name", "uni", "foot", "addr", "pix",
                                "glitch_digi", "glitch_ana", "od_en"]:
                        self.params.pop(f"f{i}_{key}", None)

                while self.fixture_widgets:
                    self.fixture_widgets.pop().deleteLater()

                self.params.update(patch_data)

                if "protocol" in patch_data and hasattr(self, 'cmb_protocol'):
                    self.cmb_protocol.setCurrentText(patch_data["protocol"])
                if "net_mode" in patch_data and hasattr(self, 'cmb_net_mode'):
                    self.cmb_net_mode.setCurrentText(patch_data["net_mode"])

                network_keys = [
                    "sacn_priority", "adv_net", "art_net", "art_sub", "sacn_src",
                    "sacn_preview", "artnet_offset", "art_ip", "art_port", "ctrl_univ", "preset_ch"
                ]
                for nk in network_keys:
                    if nk in patch_data:
                        val = patch_data[nk]
                        spin = getattr(self.ui, f"spin_{nk}", getattr(self, f"spin_{nk}", None))
                        chk = getattr(self.ui, f"chk_{nk}", getattr(self, f"chk_{nk}", None))
                        txt = getattr(self.ui, f"txt_{nk}", getattr(self, f"txt_{nk}", None))

                        if spin:
                            spin.blockSignals(True)
                            try:
                                spin.setValue(float(val))
                            except TypeError:
                                spin.setValue(int(val))
                            spin.blockSignals(False)
                        elif chk:
                            chk.blockSignals(True)
                            chk.setChecked(bool(val))
                            chk.blockSignals(False)
                            if nk == "adv_net" and hasattr(self, 'frm_adv_net'):
                                self.frm_adv_net.setVisible(bool(val))
                        elif txt:
                            txt.blockSignals(True)
                            txt.setText(str(val))
                            txt.blockSignals(False)

                num_fixes = int(self.params.get("num_fixtures", 1))
                for i in range(1, num_fixes + 1):
                    self._build_fixture_ui(i)

                self._update_fixture_list()
                self.rebuild_dmx_grid()
                self._validate_preset_ch()
                self.apply_changes()
                logger.info(f"Loaded patch configuration from: {filepath}")
            except Exception as e:
                logger.error(f"Failed to load patch file: {e}")

    def _setup_dmx_grid(self):
        parent_layout = self.ui.dmx_scroll.parentWidget().layout()
        ctrl = QWidget()
        cl = QHBoxLayout(ctrl)

        self.spin_mon_univ = QSpinBox()
        self.spin_mon_univ.setRange(0, 9999)
        self.spin_mon_univ.setValue(int(self.params.get("monitor_univ", 0)))

        self.cmb_view_mode = QComboBox()
        self.cmb_view_mode.addItems(["Raw DMX Channels", "RGBW Pixels"])
        self.cmb_view_mode.setCurrentText("RGBW Pixels")

        self.spin_dmx_cols = QSpinBox()
        self.spin_dmx_cols.setRange(8, 64)
        self.spin_dmx_cols.setValue(16)

        self.spin_dmx_size = QSpinBox()
        self.spin_dmx_size.setRange(20, 100)
        self.spin_dmx_size.setValue(36)

        cl.addWidget(QLabel("Univ:"))
        cl.addWidget(self.spin_mon_univ)
        cl.addWidget(QLabel("Mode:"))
        cl.addWidget(self.cmb_view_mode)
        cl.addWidget(QLabel("Cols:"))
        cl.addWidget(self.spin_dmx_cols)
        cl.addWidget(QLabel("Size:"))
        cl.addWidget(self.spin_dmx_size)
        parent_layout.insertWidget(0, ctrl)

        self.dmx_grid_widget = DMXGridOverlay()
        self.ui.dmx_scroll.setWidget(self.dmx_grid_widget)
        self.ui.dmx_scroll.setWidgetResizable(True)

        self.spin_mon_univ.valueChanged.connect(self._update_monitor_univ)
        self.cmb_view_mode.currentTextChanged.connect(self.rebuild_dmx_grid)
        self.spin_dmx_cols.valueChanged.connect(self.rebuild_dmx_grid)
        self.spin_dmx_size.valueChanged.connect(self.rebuild_dmx_grid)

        self.dmx_boxes = []
        self.pixel_map = []
        self.rebuild_dmx_grid()

    def rebuild_dmx_grid(self, *args):
        grid = self.dmx_grid_widget.layout() or QGridLayout(self.dmx_grid_widget)
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.dmx_boxes.clear()
        if hasattr(self.dmx_grid_widget, "dmx_containers"):
            self.dmx_grid_widget.dmx_containers.clear()
        self.pixel_map = []
        # Pixel cache is invalidated on every grid rebuild; sized once the
        # pixel_map is fully populated below.
        self.last_rendered_pixel = []
        cols, size = self.spin_dmx_cols.value(), self.spin_dmx_size.value()

        if self.cmb_view_mode.currentText() == "RGBW Pixels":
            mon, n = self.spin_mon_univ.value(), int(self.params.get("num_fixtures", 1))
            for f in range(1, n + 1):
                if not self.params.get(f"f{f}_active"): continue
                if self.params.get(f"f{f}_uni") == mon:
                    addr = self.params[f"f{f}_addr"] - 1
                    ft = self.params[f"f{f}_foot"]
                    px = self.params[f"f{f}_pix"]
                    name = self.params.get(f"f{f}_name", f"Fix {f}")
                    for p in range(px):
                        self.pixel_map.append((addr + p * ft, ft, name, p + 1))

            for i, (addr, ft, name, p_num) in enumerate(self.pixel_map):
                container = QWidget()
                layout = QVBoxLayout(container)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(2)
                layout.setAlignment(Qt.AlignCenter)

                lbl_addr = QLabel(f"P{p_num}")
                lbl_addr.setAlignment(Qt.AlignCenter)
                lbl_addr.setStyleSheet("color: #888888; font-size: 10px; font-weight: bold;")

                lbl_val = QLabel("")
                lbl_val.setFixedSize(size, size)
                lbl_val.setAlignment(Qt.AlignCenter)
                lbl_val.setStyleSheet("background-color: #111111; border: 1px solid #333;")

                layout.addWidget(lbl_addr)
                layout.addWidget(lbl_val)
                grid.addWidget(container, i // cols, i % cols)
                self.dmx_boxes.append(lbl_val)
                if hasattr(self.dmx_grid_widget, "dmx_containers"):
                    self.dmx_grid_widget.dmx_containers.append(container)
            self.last_rendered_pixel = [None] * len(self.pixel_map)
        else:
            for i in range(512):
                container = QWidget()
                layout = QVBoxLayout(container)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(2)
                layout.setAlignment(Qt.AlignCenter)

                lbl_addr = QLabel(str(i + 1))
                lbl_addr.setAlignment(Qt.AlignCenter)
                lbl_addr.setStyleSheet("color: #888888; font-size: 10px; font-weight: bold;")

                lbl_val = QLabel("0")
                lbl_val.setFixedSize(size, size)
                lbl_val.setAlignment(Qt.AlignCenter)
                lbl_val.setStyleSheet("background-color: #111111; color: #555555; font-size: 11px; font-weight: bold;")

                layout.addWidget(lbl_addr)
                layout.addWidget(lbl_val)

                grid.addWidget(container, i // cols, i % cols)
                self.dmx_boxes.append(lbl_val)
                if hasattr(self.dmx_grid_widget, "dmx_containers"):
                    self.dmx_grid_widget.dmx_containers.append(container)

        grid.setRowStretch(grid.rowCount(), 1)
        self.dmx_grid_widget.updateGeometry()
        if hasattr(self.dmx_grid_widget, 'parentWidget') and self.dmx_grid_widget.parentWidget():
            self.dmx_grid_widget.parentWidget().updateGeometry()

        self.rebuild_fixture_preview()
        self._update_dmx_overlay()

    def _setup_fixture_preview(self):
        """Horizontal strip of one color-box per active fixture above the DMX grid."""
        container = QWidget()
        container.setFixedHeight(68)
        h = QHBoxLayout(container)
        h.setContentsMargins(4, 4, 4, 4)
        h.setSpacing(4)
        h.setAlignment(Qt.AlignLeft)
        self._fix_prev_container = container
        self._fix_prev_layout = h
        self._fix_prev_boxes = {}  # {f_num: QLabel color swatch}
        parent_layout = self.ui.dmx_scroll.parentWidget().layout()
        parent_layout.insertWidget(0, container)
        self.rebuild_fixture_preview()

    def rebuild_fixture_preview(self):
        if not hasattr(self, '_fix_prev_layout'):
            return
        while self._fix_prev_layout.count():
            item = self._fix_prev_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._fix_prev_boxes.clear()
        n = int(self.params.get("num_fixtures", 1))
        for f in range(1, n + 1):
            if not self.params.get(f"f{f}_active"):
                continue
            name = self.params.get(f"f{f}_name", f"Fix {f}")
            uni = self.params.get(f"f{f}_uni", 0)
            box = QWidget()
            box.setFixedWidth(72)
            vl = QVBoxLayout(box)
            vl.setContentsMargins(2, 2, 2, 2)
            vl.setSpacing(2)
            lbl_name = QLabel(name[:9])
            lbl_name.setAlignment(Qt.AlignCenter)
            lbl_name.setStyleSheet("color: #777; font-size: 9px;")
            lbl_name.setFixedHeight(12)
            lbl_uni = QLabel(f"U{uni}")
            lbl_uni.setAlignment(Qt.AlignCenter)
            lbl_uni.setStyleSheet("color: #555; font-size: 9px;")
            lbl_uni.setFixedHeight(10)
            lbl_color = QLabel()
            lbl_color.setStyleSheet("background: #111; border: 1px solid #2a2a2a; border-radius: 3px;")
            vl.addWidget(lbl_name)
            vl.addWidget(lbl_uni)
            vl.addWidget(lbl_color, 1)
            self._fix_prev_layout.addWidget(box)
            self._fix_prev_boxes[f] = lbl_color
        self._fix_prev_layout.addStretch()

    def _refresh_fixture_preview(self, snap_buffers):
        if not hasattr(self, '_fix_prev_boxes'):
            return
        for f, lbl in self._fix_prev_boxes.items():
            uni = self.params.get(f"f{f}_uni", 0)
            addr = self.params.get(f"f{f}_addr", 1) - 1
            foot = self.params.get(f"f{f}_foot", 4)
            buf = snap_buffers.get(uni)
            if buf is None or addr < 0 or addr + max(foot, 1) > 512:
                lbl.setStyleSheet("background: #111; border: 1px solid #2a2a2a; border-radius: 3px;")
                continue
            r = buf[addr] if foot >= 1 else 0
            g = buf[addr + 1] if foot >= 2 else 0
            b = buf[addr + 2] if foot >= 3 else 0
            w = buf[addr + 3] if foot >= 4 else 0
            sr, sg, sb = min(255, r + w), min(255, g + w), min(255, b + w)
            if max(sr, sg, sb) < 4:
                lbl.setStyleSheet("background: #111; border: 1px solid #2a2a2a; border-radius: 3px;")
            else:
                lbl.setStyleSheet(f"background: rgb({sr},{sg},{sb}); border-radius: 3px;")

    def _setup_artpoll_ui(self):
        """Network discovery section appended to the Network Output Settings group."""
        box = QGroupBox("Network Discovery (ArtPoll)")
        box.setStyleSheet("QGroupBox { font-weight: bold; }")
        vl = QVBoxLayout(box)
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        self._btn_artpoll = QPushButton("Scan Network")
        self._btn_artpoll.clicked.connect(self._start_artpoll_scan)
        self._lbl_artpoll_status = QLabel("Press Scan to discover Art-Net nodes on this subnet.")
        self._lbl_artpoll_status.setStyleSheet("color: #888; font-size: 11px;")
        bl.addWidget(self._btn_artpoll)
        bl.addWidget(self._lbl_artpoll_status, 1)
        self._tbl_nodes = QTableWidget(0, 3)
        self._tbl_nodes.setHorizontalHeaderLabels(["IP", "Short Name", "Long Name"])
        self._tbl_nodes.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._tbl_nodes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl_nodes.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl_nodes.setFixedHeight(120)
        self._tbl_nodes.setStyleSheet("font-size: 11px;")
        vl.addWidget(btn_row)
        vl.addWidget(self._tbl_nodes)
        if self.ui.groupBox_6.layout():
            self.ui.groupBox_6.layout().addWidget(box)

    def _start_artpoll_scan(self):
        scan_fn = self.callbacks.get("artpoll_scan")
        if not scan_fn:
            return
        self._btn_artpoll.setEnabled(False)
        self._lbl_artpoll_status.setText("Scanning… (3 s)")
        self.app_state["discovered_nodes"] = None  # sentinel: scan in progress

        def _run():
            results = scan_fn(timeout=3.0)
            self.app_state["discovered_nodes"] = results

        threading.Thread(target=_run, daemon=True).start()

    def _update_artpoll_table(self):
        nodes = self.app_state.get("discovered_nodes")
        if nodes is None:
            return  # scan still running
        self._btn_artpoll.setEnabled(True)
        self._tbl_nodes.setRowCount(0)
        if not nodes:
            self._lbl_artpoll_status.setText("No Art-Net nodes found.")
            return
        self._lbl_artpoll_status.setText(f"{len(nodes)} node(s) found.")
        for node in nodes:
            r = self._tbl_nodes.rowCount()
            self._tbl_nodes.insertRow(r)
            self._tbl_nodes.setItem(r, 0, QTableWidgetItem(node.get("ip", "")))
            self._tbl_nodes.setItem(r, 1, QTableWidgetItem(node.get("short_name", "")))
            self._tbl_nodes.setItem(r, 2, QTableWidgetItem(node.get("long_name", "")))
        self.app_state["discovered_nodes"] = []  # clear so we don't re-render next frame

    def _update_dmx_overlay(self):
        if hasattr(self.dmx_grid_widget, "active_zones"):
            self.dmx_grid_widget.active_zones.clear()
            self.dmx_grid_widget.setContentsMargins(0, 15, 0, 0)

        if getattr(self, 'cmb_view_mode', None) and self.cmb_view_mode.currentText() == "RGBW Pixels":
            self.dmx_grid_widget.update()
            return

        mon = self.spin_mon_univ.value()
        n = int(self.params.get("num_fixtures", 1))

        if hasattr(self.dmx_grid_widget, "active_zones"):
            cols = self.spin_dmx_cols.value()
            for f in range(1, n + 1):
                is_active = int(self.params.get(f"f{f}_active", 0)) == 1
                is_on_mon = int(self.params.get(f"f{f}_uni", 0)) == mon

                if is_active and is_on_mon:
                    addr = int(self.params.get(f"f{f}_addr", 1)) - 1
                    ft = int(self.params.get(f"f{f}_foot", 4))
                    px = int(self.params.get(f"f{f}_pix", 16))
                    name = self.params.get(f"f{f}_name", f"Fix {f}")

                    length = ft * px
                    start = addr

                    while length > 0:
                        row_end = ((start // cols) + 1) * cols
                        chunk_end = min(start + length, row_end)
                        label = name if start == addr else ""

                        self.dmx_grid_widget.active_zones.append((start, chunk_end - 1, label))

                        length -= (chunk_end - start)
                        start = chunk_end

            self.dmx_grid_widget.update()
        self.last_rendered_dmx = [-1] * 512
        # Force the next pixel-mode pass to repaint every cell.
        if self.last_rendered_pixel:
            self.last_rendered_pixel = [None] * len(self.last_rendered_pixel)

    def _update_monitor_univ(self, v):
        self.params["monitor_univ"] = v
        self.rebuild_dmx_grid()

    def _setup_protocol_ui(self):
        box = QGroupBox("Network Output Settings")
        box.setStyleSheet("QGroupBox { font-weight: bold; }")
        layout = QGridLayout(box)

        self.cmb_protocol = QComboBox()
        self.cmb_protocol.addItems(["Art-Net", "sACN"])
        self.cmb_protocol.setCurrentText(self.params.get("protocol", "Art-Net"))

        self.cmb_net_mode = QComboBox()
        self.cmb_net_mode.currentTextChanged.connect(lambda text: self.params.update({"net_mode": text}))

        self.spin_sacn_priority = QSpinBox()
        self.spin_sacn_priority.setRange(1, 200)
        self.spin_sacn_priority.setValue(int(self.params.get("sacn_priority", 100)))

        self.chk_adv_net = QCheckBox("Advanced Setup")
        self.chk_adv_net.setChecked(bool(self.params.get("adv_net", 0)))

        self.chk_artnet_offset = QCheckBox("QLC+ Universe Offset (-1)")
        self.chk_artnet_offset.setChecked(bool(self.params.get("artnet_offset", 1)))
        self.chk_artnet_offset.toggled.connect(lambda v: self.params.update({"artnet_offset": 1 if v else 0}))

        self.frm_adv_net = QWidget()
        adv_l = QGridLayout(self.frm_adv_net)
        self.spin_art_net = QSpinBox()
        self.spin_art_net.setRange(0, 127)
        self.spin_art_sub = QSpinBox()
        self.spin_art_sub.setRange(0, 15)
        self.txt_sacn_src = QLineEdit(str(self.params.get("sacn_src", "Titan Engine")))
        self.chk_sacn_preview = QCheckBox("Preview Mode")

        adv_l.addWidget(QLabel("Net:"), 0, 0)
        adv_l.addWidget(self.spin_art_net, 0, 1)
        adv_l.addWidget(QLabel("Sub:"), 0, 2)
        adv_l.addWidget(self.spin_art_sub, 0, 3)
        adv_l.addWidget(QLabel("Src:"), 1, 0)
        adv_l.addWidget(self.txt_sacn_src, 1, 1, 1, 3)

        layout.addWidget(QLabel("Proto:"), 0, 0)
        layout.addWidget(self.cmb_protocol, 0, 1)
        layout.addWidget(QLabel("Mode:"), 0, 2)
        layout.addWidget(self.cmb_net_mode, 0, 3)
        layout.addWidget(self.chk_artnet_offset, 1, 0, 1, 2)
        layout.addWidget(QLabel("sACN Pri:"), 1, 2)
        layout.addWidget(self.spin_sacn_priority, 1, 3)
        layout.addWidget(self.chk_adv_net, 2, 0, 1, 4)
        layout.addWidget(self.frm_adv_net, 3, 0, 1, 4)

        if self.ui.groupBox_6.layout():
            self.ui.groupBox_6.layout().addWidget(box)

        self.chk_adv_net.toggled.connect(
            lambda v: (self.frm_adv_net.setVisible(v), self.params.update({"adv_net": int(v)})))
        self.cmb_protocol.currentTextChanged.connect(self._on_protocol_change)
        self._on_protocol_change(self.cmb_protocol.currentText())

        self.frm_adv_net.setVisible(self.chk_adv_net.isChecked())
        self.spin_sacn_priority.valueChanged.connect(lambda v: self.params.update({"sacn_priority": v}))
        self.spin_art_net.valueChanged.connect(lambda v: self.params.update({"art_net": v}))
        self.spin_art_sub.valueChanged.connect(lambda v: self.params.update({"art_sub": v}))
        self.txt_sacn_src.textChanged.connect(lambda v: self.params.update({"sacn_src": v}))
        self.chk_sacn_preview.toggled.connect(lambda v: self.params.update({"sacn_preview": 1 if v else 0}))

    def _on_adv_net_toggle(self, is_checked):
        self._update_chk_simple("adv_net", is_checked)
        self.frm_adv_net.setVisible(is_checked)

    def _on_protocol_change(self, text):
        self.params["protocol"] = text
        self.cmb_net_mode.clear()
        if text == "Art-Net":
            self.cmb_net_mode.addItems(["Unicast", "Broadcast"])
            self.spin_sacn_priority.setEnabled(False)
        else:
            self.cmb_net_mode.addItems(["Unicast", "Multicast"])
            self.spin_sacn_priority.setEnabled(True)

    def _setup_color_mixer(self):
        box = QGroupBox("Master Effect Color")
        box.setStyleSheet("QGroupBox { font-weight: bold; }")
        layout = QVBoxLayout(box)

        colors = [
            ("Red", "color_r", "#ff5555"),
            ("Green", "color_g", "#55ff55"),
            ("Blue", "color_b", "#55aaff"),
            ("White", "color_w", "#ffffff")
        ]

        for name, param, hex_code in colors:
            row = QHBoxLayout()
            lbl = QLabel(name)
            lbl.setMinimumWidth(50)
            lbl.setStyleSheet(f"color: {hex_code}; font-weight: bold;")
            row.addWidget(lbl)

            sld = QSlider(Qt.Horizontal)
            setattr(self.ui, f"sld_{param}", sld)
            row.addWidget(sld)

            spin = QSpinBox()
            spin.setRange(0, 255)
            setattr(self.ui, f"spin_{param}", spin)
            row.addWidget(spin)
            layout.addLayout(row)

        if self.ui.box_master_fader.parentWidget().layout():
            self.ui.box_master_fader.parentWidget().layout().insertWidget(1, box)

    def _setup_bg_color_mixer(self):
        box = QGroupBox("Static Background Color")
        box.setStyleSheet("QGroupBox { font-weight: bold; border: 1px solid #444; }")
        layout = QVBoxLayout(box)

        row_dim = QHBoxLayout()
        lbl_dim = QLabel("Dimmer")
        lbl_dim.setMinimumWidth(50)
        row_dim.addWidget(lbl_dim)

        # --- FIXED: Properly attach the widgets to the 'ui' object ---
        sld = QSlider(Qt.Horizontal)
        setattr(self.ui, "sld_bg_dimmer", sld)

        spin = QDoubleSpinBox()
        spin.setRange(0, 1)
        spin.setSingleStep(0.01)
        setattr(self.ui, "spin_bg_dimmer", spin)

        row_dim.addWidget(sld)
        row_dim.addWidget(spin)
        layout.addLayout(row_dim)

        colors = [
            ("Red", "bg_r", "#ff5555"),
            ("Green", "bg_g", "#55ff55"),
            ("Blue", "bg_b", "#55aaff"),
            ("White", "bg_w", "#ffffff")
        ]

        for name, param, hex_code in colors:
            row = QHBoxLayout()
            lbl = QLabel(name)
            lbl.setMinimumWidth(50)
            lbl.setStyleSheet(f"color: {hex_code}; font-weight: bold;")
            row.addWidget(lbl)

            sld = QSlider(Qt.Horizontal)
            setattr(self.ui, f"sld_{param}", sld)
            row.addWidget(sld)

            spin = QSpinBox()
            spin.setRange(0, 255)
            setattr(self.ui, f"spin_{param}", spin)
            row.addWidget(spin)
            layout.addLayout(row)

        if self.ui.box_master_fader.parentWidget().layout():
            self.ui.box_master_fader.parentWidget().layout().insertWidget(2, box)

    def _setup_performance_monitors(self):
        l = self.ui.box_status.layout()

        # Test Tone row — added first so it sits directly below the
        # Remote Control row (which is the last .ui-defined status row).
        row_test = QHBoxLayout()
        self.lbl_stat_test = QLabel("⚫ OFF")
        self.lbl_stat_test.setStyleSheet("color: #555555; font-weight: bold;")
        row_test.addWidget(QLabel("Test Tone:"))
        row_test.addWidget(self.lbl_stat_test)
        l.addLayout(row_test)

        self.lbl_stat_test.setCursor(Qt.PointingHandCursor)
        self.lbl_stat_test.installEventFilter(self)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #555; margin-top: 5px; margin-bottom: 5px;")
        l.addWidget(line)

        row_fps = QHBoxLayout()
        self.lbl_stat_fps = QLabel("0.0")
        self.lbl_stat_fps.setStyleSheet("color: #00ffff; font-weight: bold;")
        row_fps.addWidget(QLabel("Engine FPS:"))
        row_fps.addWidget(self.lbl_stat_fps)
        l.addLayout(row_fps)

        row_dsp = QHBoxLayout()
        self.lbl_stat_dsp = QLabel("0.00 ms")
        self.lbl_stat_dsp.setStyleSheet("color: #00ff00; font-weight: bold;")
        row_dsp.addWidget(QLabel("DSP Compute:"))
        row_dsp.addWidget(self.lbl_stat_dsp)
        l.addLayout(row_dsp)

        row_buf = QHBoxLayout()
        self.lbl_stat_buf = QLabel("0.0 ms")
        self.lbl_stat_buf.setStyleSheet("color: #aaaaaa;")
        row_buf.addWidget(QLabel("Audio Buffer:"))
        row_buf.addWidget(self.lbl_stat_buf)
        l.addLayout(row_buf)

        row_web = QHBoxLayout()
        self.lbl_stat_web = QLabel("starting…")
        self.lbl_stat_web.setStyleSheet("color: #5588ff; font-size: 11px;")
        self.lbl_stat_web.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row_web.addWidget(QLabel("Web Remote:"))
        row_web.addWidget(self.lbl_stat_web)
        l.addLayout(row_web)

    def _setup_remote_info(self):
        box = QGroupBox("Control Universe DMX Map (Fixed)")
        box.setStyleSheet("QGroupBox { font-weight: bold; border: 1px solid #555; }")
        layout = QVBoxLayout(box)

        lbl = QLabel(
            "<b>-- Head 0 (Global Master) [Ch 1-18] --</b><br>"
            "<b>Ch 1:</b> Master Dimmer (0-255)<br>"
            "<b>Ch 2:</b> Master Red (0-255)<br>"
            "<b>Ch 3:</b> Master Green (0-255)<br>"
            "<b>Ch 4:</b> Master Blue (0-255)<br>"
            "<b>Ch 5:</b> Master White (0-255)<br>"
            "<b>Ch 6:</b> Master Static Noise (0-127: Force OFF | 128-255: Force ON)<br>"
            "<b>Ch 7:</b> Master Digital Glitch (0-127: Force OFF | 128-255: Force ON)<br>"
            "<b>Ch 8:</b> Master Audio Dist. (0-127: Force OFF | 128-255: Force ON)<br>"
            "<b>Ch 9:</b> Center Skew (0=Left, 127=Center, 255=Right)<br>"
            "<b>Ch 10:</b> Effect Width (0-255)<br>"
            "<b>Ch 11:</b> Analog Glitch Strength (0-255)<br>"
            "<b>Ch 12:</b> Digital Glitch Strength (0-255)<br>"
            "<b>Ch 13:</b> Overdrive Glitch Strength (0-255)<br>"
            "<b>Ch 14:</b> Static BG Dimmer (0-255)<br>"
            "<b>Ch 15:</b> Static BG Red (0-255)<br>"
            "<b>Ch 16:</b> Static BG Green (0-255)<br>"
            "<b>Ch 17:</b> Static BG Blue (0-255)<br>"
            "<b>Ch 18:</b> Static BG White (0-255)<br><br>"
            "<b>-- Individual Fixtures [Ch 19+] (13 Channels Each) --</b><br>"
            "<b>Ch 1:</b> Dimmer | <b>Ch 2:</b> R | <b>Ch 3:</b> G | <b>Ch 4:</b> B | <b>Ch 5:</b> W<br>"
            "<b>Ch 6:</b> Enable Static Noise (128-255)<br>"
            "<b>Ch 7:</b> Enable Digital Glitch (128-255)<br>"
            "<b>Ch 8:</b> Enable Audio Dist. (128-255)<br>"
            "<b>Ch 9:</b> BG Dimmer | <b>Ch 10:</b> BG R | <b>Ch 11:</b> BG G | <b>Ch 12:</b> BG B | <b>Ch 13:</b> BG W<br><br>"
            "<b>-- Global Shape & FX Modifiers (End of Universe) --</b><br>"
            "<b>Ch 511:</b> Reserved<br>"
            "<b>Presets (10% Slots):</b> User Definable (Default 512)"
        )
        lbl.setStyleSheet("font-size: 12px; line-height: 1.4;")
        layout.addWidget(lbl)

        if self.ui.tab_2.layout():
            vbox = self.ui.tab_2.layout().itemAt(0).layout()
            if vbox:
                vbox.insertWidget(0, box)

    def _setup_presets(self):
        self.preset_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "titan_presets.json")
        try:
            with open(self.preset_file, 'r') as f:
                self.app_state["preset_map"] = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load preset map '{self.preset_file}': {e}. Starting with empty preset map.")
            self.app_state["preset_map"] = {}

        if hasattr(self.ui, 'tbl_presets'):
            while self.ui.horizontalLayout_28.count():
                item = self.ui.horizontalLayout_28.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            self.spin_preset_ch = QSpinBox()
            self.spin_preset_ch.setRange(1, 512)
            self.spin_preset_ch.setValue(int(self.params.get("preset_ch", 512)))
            self.spin_preset_ch.valueChanged.connect(lambda v: self._update_spin_patch("preset_ch", v))

            self.lbl_preset_warn = QLabel("")
            self.lbl_preset_warn.setStyleSheet("font-weight: bold;")

            lay = self.ui.horizontalLayout_28
            lay.addWidget(QLabel("Preset DMX Channel:"))
            lay.addWidget(self.spin_preset_ch)
            lay.addWidget(self.lbl_preset_warn)
            lay.addStretch()

            btn_add = QPushButton("Assign Slot")
            btn_add.clicked.connect(self._add_preset)
            btn_rem = QPushButton("Clear Slot")
            btn_rem.clicked.connect(self._remove_preset)

            lay.addWidget(btn_add)
            lay.addWidget(btn_rem)

            self.ui.tbl_presets.setColumnCount(2)
            self.ui.tbl_presets.setHorizontalHeaderLabels(["DMX Range / Slot", "Target JSON File"])
            self.ui.tbl_presets.horizontalHeader().setStretchLastSection(True)
            self.ui.tbl_presets.setSelectionBehavior(QAbstractItemView.SelectRows)

            self._validate_preset_ch()
            self._refresh_presets()

    def _refresh_presets(self):
        pmap = self.app_state.get("preset_map", {})
        active_preset = str(self.app_state.get("current_preset", "")).strip()

        self.ui.tbl_presets.setSortingEnabled(False)
        self.ui.tbl_presets.clearContents()
        self.ui.tbl_presets.setRowCount(10)

        for slot in range(1, 11):
            file = pmap.get(str(slot), "[Empty]")
            clean_file = str(file).strip()

            dmx_low = 26 + ((slot - 1) * 25) if slot < 10 else 251
            dmx_high = 50 + ((slot - 1) * 25) if slot < 10 else 255

            item_ch = QTableWidgetItem(f"Slot {slot}  ({dmx_low}-{dmx_high})")
            item_ch.setTextAlignment(Qt.AlignCenter)
            self.ui.tbl_presets.setItem(slot - 1, 0, item_ch)

            if clean_file == active_preset:
                item_file = QTableWidgetItem(f"▶ [ACTIVE] {clean_file}")
                font = QFont()
                font.setBold(True)
                item_file.setFont(font)
                item_file.setForeground(QBrush(QColor("#00ff00")))
            else:
                item_file = QTableWidgetItem(clean_file)
                item_file.setForeground(QBrush(QColor("#aaaaaa")))

            self.ui.tbl_presets.setItem(slot - 1, 1, item_file)

        self.ui.tbl_presets.viewport().update()

    def _add_preset(self):
        path, _ = QFileDialog.getOpenFileName(self.ui, "Select JSON Configuration", "", "JSON Files (*.json)")
        if not path: return
        filename = os.path.basename(path)

        from PySide6.QtWidgets import QInputDialog
        slot, ok = QInputDialog.getInt(self.ui, "Assign Slot", f"Enter Preset Slot (1-10) for '{filename}':", 1, 1, 10)
        if ok:
            self.app_state["preset_map"][str(slot)] = filename
            self._save_presets()

    def _remove_preset(self):
        row = self.ui.tbl_presets.currentRow()
        if row >= 0:
            slot = str(row + 1)
            if slot in self.app_state["preset_map"]:
                del self.app_state["preset_map"][slot]
                self._save_presets()

    def _save_presets(self):
        with open(self.preset_file, 'w') as f:
            json.dump(self.app_state["preset_map"], f, indent=4)
        self._refresh_presets()

    def _validate_preset_ch(self):
        if not hasattr(self, 'spin_preset_ch'): return
        num = int(self.params.get("num_fixtures", 1))
        used = 18 + (num * 13)
        is_bad = self.spin_preset_ch.value() <= used
        self.lbl_preset_warn.setText(
            f"⚠️ CONFLICT! (Channels 1-{used} in use)" if is_bad else f"🟢 Safe (Footprint: 1-{used})")
        self.lbl_preset_warn.setStyleSheet(
            "color: red; font-weight: bold;" if is_bad else "color: #00ff00; font-weight: bold;")
        self.spin_preset_ch.setStyleSheet("color: red; font-weight: bold;" if is_bad else "")

    def _is_system_key(self, key):
        system_exact = [
            "art_ip", "art_port", "osc_ip", "osc_out_port", "osc_in_port",
            "osc_path", "protocol", "net_mode", "sacn_priority", "ctrl_univ", "monitor_univ",
            "monitor_fps", "num_fixtures", "adv_net", "art_net", "art_sub", "sacn_src", "sacn_preview",
            "preset_ch", "artnet_offset"
        ]
        if key in system_exact: return True

        system_suffixes = [
            "_uni", "_foot", "_addr", "_pix", "_active",
            "_extend", "_flip", "_align", "_name"
        ]
        if key.startswith("f"):
            for suffix in system_suffixes:
                if key.endswith(suffix):
                    return True
        return False

    def save_default(self):
        self.apply_changes()
        try:
            filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "titan_default.json")
            with open(filepath, 'w') as f:
                json.dump(self.params, f, indent=4)

            logger.info("Default configuration saved successfully.")
            QMessageBox.information(self.ui, "Default Saved",
                                    "Current patch and settings saved as Default!\n\n"
                                    "They will automatically load and begin transmitting next time you launch Titan Engine.")
        except Exception as e:
            logger.error(f"Failed to save default: {e}")

    def save_config(self):
        self.apply_changes()
        try:
            path, _ = QFileDialog.getSaveFileName(self.ui, "Save Effect Preset", "", "JSON Files (*.json)")
            if path:
                if not path.lower().endswith('.json'):
                    path += '.json'

                preset_data = {k: v for k, v in self.params.items() if k in self.PRESET_WHITELIST}
                preset_data["file_type"] = "titan_preset"

                with open(path, 'w') as f:
                    json.dump(preset_data, f, indent=4)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Failed to save preset file: {e}")

    def load_config(self, filepath=None):
        if not filepath:
            filepath, _ = QFileDialog.getOpenFileName(self.ui, "Load Preset", "", "JSON Files (*.json)")

        if filepath:
            try:
                # --- NEW: Tell the engine to drop audio for 250ms ---
                self.engine.preset_mask_time = time.time() + 0.25

                with open(filepath, 'r') as f:
                    new_params = json.load(f)

                if new_params.get("file_type") == "titan_patch":
                    print("⚠️ Blocked: Cannot load a Patch file into the Preset engine!")
                    return

                filtered_params = {k: v for k, v in new_params.items() if k in self.PRESET_WHITELIST}

                # --- NEW: Merge the preset into the master memory BEFORE updating the GUI ---
                self.params.update(filtered_params)

                if "protocol" in self.params:
                    self.cmb_protocol.setCurrentText(self.params["protocol"])
                if "net_mode" in self.params:
                    self.cmb_net_mode.setCurrentText(self.params["net_mode"])
                if "sacn_priority" in self.params:
                    self.spin_sacn_priority.setValue(int(self.params["sacn_priority"]))

                for name in filtered_params.keys():
                    spin = getattr(self.ui, f"spin_{name}", self.dyn_widgets.get(f"spin_{name}"))
                    sld = getattr(self.ui, f"sld_{name}", None)
                    chk = getattr(self.ui, f"chk_{name}", self.dyn_widgets.get(f"chk_{name}"))
                    txt = getattr(self.ui, f"txt_{name}", self.dyn_widgets.get(f"txt_{name}"))

                    if spin:
                        spin.blockSignals(True)
                        val = float(self.params[name])
                        try:
                            spin.setValue(val)
                        except TypeError:
                            spin.setValue(int(val))
                        spin.blockSignals(False)
                    if sld:
                        cfg = self.slider_cfg.get(name, {"min": 0, "max": 100})
                        ratio = (self.params[name] - cfg.get("min", 0)) / max(0.001,
                                                                              (cfg.get("max", 100) - cfg.get("min", 0)))
                        sld.blockSignals(True)
                        sld.setValue(int(ratio * 100))
                        sld.blockSignals(False)
                    if chk:
                        chk.blockSignals(True)
                        chk.setChecked(bool(self.params[name]))
                        chk.blockSignals(False)
                    if txt:
                        txt.blockSignals(True)
                        txt.setText(str(self.params[name]))
                        txt.blockSignals(False)

                self.apply_changes()
                self._update_dmx_overlay()
                self.app_state["current_preset"] = os.path.basename(filepath)
                if hasattr(self, '_refresh_presets'):
                    self._refresh_presets()
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                logger.error(f"Failed to load preset '{filepath}': {e}")

    def _link_widgets(self):
        # --- Catch Qt Designer naming bug for the Control Universe ---
        if not hasattr(self.ui, 'spin_ctrl_univ') and hasattr(self.ui, 'spinBox_ctrl_univ'):
            self.ui.spin_ctrl_univ = self.ui.spinBox_ctrl_univ

        # --- Catch Qt Designer naming bug for the IP Address ---
        if not hasattr(self.ui, 'txt_art_ip') and hasattr(self.ui, 'lineEdit_art_ip'):
            self.ui.txt_art_ip = self.ui.lineEdit_art_ip

        self._setup_presets()

        if hasattr(self.ui, 'btn_toggle_artnet'):
            self.ui.btn_toggle_artnet.clicked.connect(self.callbacks.get("toggle_artnet"))

        self.live_update = True
        if hasattr(self.ui, 'chk_live_update'):
            self.ui.chk_live_update.toggled.connect(self._toggle_live_update)
        if hasattr(self.ui, 'btn_apply'):
            self.ui.btn_apply.clicked.connect(self.apply_changes)
            self.ui.btn_apply.setEnabled(False)

        for name in self.params.keys():
            try:
                sld = getattr(self.ui, f"sld_{name}", None)
                spin = getattr(self.ui, f"spin_{name}", self.dyn_widgets.get(f"spin_{name}"))
                chk = getattr(self.ui, f"chk_{name}", self.dyn_widgets.get(f"chk_{name}"))
                txt = getattr(self.ui, f"txt_{name}", None)
                cfg = self.slider_cfg.get(name, {"min": 0.0, "max": 65535.0})

                if spin:
                    spin.setKeyboardTracking(False)
                    from PySide6.QtWidgets import QAbstractSpinBox
                    spin.setButtonSymbols(QAbstractSpinBox.NoButtons)

                if spin and not name.startswith("f"):
                    min_val = float(cfg.get("min", 0.0))
                    max_val = float(cfg.get("max", 65535.0))
                    try:
                        spin.setRange(min_val, max_val)
                    except TypeError:
                        spin.setRange(int(min_val), int(max_val))

                if sld:
                    sld.setRange(0, 100)
                    sld.valueChanged.connect(lambda v, n=name, s=spin: self._update_param(n, v, s, False))

                if spin and not name.startswith("f"):
                    spin.editingFinished.connect(
                        lambda n=name, s=spin, sl=sld: self._update_param(n, s.value(), sl, True))

                if chk and not name.startswith("f"):
                    chk.toggled.connect(lambda v, n=name: self._update_chk(n, v))

                # --- NEW: Hook up text boxes so typing actually saves the IP! ---
                if txt and not name.startswith("f"):
                    txt.textChanged.connect(lambda v, n=name: self._update_txt(n, v))

                if spin:
                    spin.blockSignals(True)
                    val = float(self.params[name])
                    try:
                        spin.setValue(val)
                    except TypeError:
                        spin.setValue(int(val))
                    spin.blockSignals(False)

                if sld:
                    sld.blockSignals(True)
                    val = float(self.params[name])
                    ratio = (val - cfg.get("min", 0.0)) / max(0.001, (cfg.get("max", 1.0) - cfg.get("min", 0.0)))
                    sld.setValue(int(ratio * 100))
                    sld.blockSignals(False)
                if chk:
                    chk.blockSignals(True)
                    chk.setChecked(bool(float(self.params[name])))
                    chk.blockSignals(False)

                # --- NEW: Force checkboxes to match their saved default state on boot ---
                if chk:
                    chk.blockSignals(True)
                    chk.setChecked(bool(float(self.params[name])))
                    chk.blockSignals(False)

                # --- NEW: Force text boxes to load their saved strings on boot ---
                if txt:
                    txt.blockSignals(True)
                    txt.setText(str(self.params[name]))
                    txt.blockSignals(False)
            except Exception as e:
                logger.error(f"_link_widgets failed for param '{name}': {e}")

        self._init_multiplexer()

        # --- NEW: Set initial lock state on boot ---
        self._apply_remote_lock(bool(int(self.params.get("remote_on", 1))))

    def _on_remote_toggled(self, checked):
        # 1. Tell the main engine to stop/start listening
        if "toggle_remote" in self.callbacks:
            self.callbacks["toggle_remote"]()

        # 2. Lock or unlock the GUI sliders
        self._apply_remote_lock(checked)

    def _apply_remote_lock(self, is_locked):
        # 1. ONLY lock the parameters that QLC+ actually controls via DMX
        dmx_controlled_sliders = [
            "master_inhibitive", "dimmer",
            "color_r", "color_g", "color_b", "color_w",
            "skew", "width",
            "glitch_ana_amt", "glitch_digi_amt", "od_glitch",
            "bg_dimmer", "bg_r", "bg_g", "bg_b", "bg_w"
        ]

        # Lock/Unlock ONLY those specific sliders and spinboxes
        for name in dmx_controlled_sliders:
            sld = getattr(self.ui, f"sld_{name}", None)
            spin = getattr(self.ui, f"spin_{name}",
                           self.dyn_widgets.get(f"spin_{name}", None) if hasattr(self, 'dyn_widgets') else None)

            if sld: sld.setEnabled(not is_locked)
            if spin: spin.setEnabled(not is_locked)

        # 2. Lock/Unlock the master effect checkboxes (also DMX controlled)
        master_chks = [
            "master_ana_force_off", "master_ana_force_on",
            "master_digi_force_off", "master_digi_force_on",
            "master_od_force_off", "master_od_force_on"
        ]
        for chk_name in master_chks:
            chk = getattr(self, f"chk_{chk_name}", getattr(self.ui, f"chk_{chk_name}", None))
            if chk: chk.setEnabled(not is_locked)

    def _toggle_live_update(self, state):
        self.live_update = state
        if hasattr(self.ui, 'btn_apply'):
            self.ui.btn_apply.setEnabled(not state)
        if state:
            self.apply_changes()

    def _init_multiplexer(self):
        self.multiplexed_params = [
            "drive", "scale", "gamma", "eq_tilt", "knee", "freq_width", "skew",
            "atk_c", "rel_c", "atk_e", "rel_e", "time_gamma", "dimmer",
            "color_r", "color_g", "color_b", "color_w",
            "jitter_on", "jitter_thresh", "jitter_amount", "dmx_smooth_on",
            "smooth_size", "glitch_digi_amt", "glitch_digi_block",
            "glitch_ana_amt", "glitch_ana_tear", "glitch_ana_noise",
            "od_thresh", "od_desat", "od_glitch", "dimmer_atk", "dimmer_rel", "width",
            "bg_dimmer", "bg_r", "bg_g", "bg_b", "bg_w"
        ]

        if hasattr(self.ui, 'list_target_fixtures'):
            self.btn_select_fixtures = QPushButton("🎯 Target: GLOBAL MASTER")
            self.btn_select_fixtures.setStyleSheet(
                "font-weight: bold; padding: 6px; background-color: #333; color: white;")
            parent_layout = self.ui.list_target_fixtures.parentWidget().layout()
            parent_layout.replaceWidget(self.ui.list_target_fixtures, self.btn_select_fixtures)
            self.ui.list_target_fixtures.deleteLater()
            del self.ui.list_target_fixtures

            self.fixture_popup = QFrame(self.ui, Qt.Popup)
            self.fixture_popup.setStyleSheet("background-color: #2b2b2b; border: 1px solid #555;")
            pop_layout = QVBoxLayout(self.fixture_popup)
            pop_layout.setContentsMargins(0, 0, 0, 0)

            lbl_info = QLabel("Target selection routes local UI clicks.\nExternal DMX controls channels directly.")
            lbl_info.setStyleSheet("color: #aaa; font-style: italic; font-size: 10px; padding: 4px;")
            pop_layout.addWidget(lbl_info)

            self.tbl_target_fixtures = QTableWidget()
            self.tbl_target_fixtures.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.tbl_target_fixtures.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.tbl_target_fixtures.verticalHeader().setVisible(False)
            self.tbl_target_fixtures.setStyleSheet("border: none;")
            pop_layout.addWidget(self.tbl_target_fixtures)

            self.btn_select_fixtures.clicked.connect(self._show_fixture_popup)
            self.tbl_target_fixtures.itemSelectionChanged.connect(self._refresh_multiplexer_ui)

        if hasattr(self.ui, 'chk_link_all_dynamics'):
            self.ui.chk_link_all_dynamics.toggled.connect(self._refresh_multiplexer_ui)
        self._update_fixture_list()

    def _show_fixture_popup(self):
        pos = self.btn_select_fixtures.mapToGlobal(self.btn_select_fixtures.rect().bottomLeft())
        self.fixture_popup.move(pos)
        self.fixture_popup.resize(350, 250)
        self.fixture_popup.show()

    def _update_fixture_list(self):
        if not hasattr(self, 'tbl_target_fixtures'): return
        self.tbl_target_fixtures.blockSignals(True)
        self.tbl_target_fixtures.setRowCount(0)

        rows = [("GLOBAL MASTER", "(Fallback)"), ("ALL FIXTURES", "(Burn to all)")]
        num_fixes = int(self.params.get("num_fixtures", 1))
        for i in range(1, num_fixes + 1):
            name = self.params.get(f"f{i}_name", f"Fixture {i}")
            rows.append((f"Fixture {i}", name))

        self.tbl_target_fixtures.setRowCount(len(rows))
        self.tbl_target_fixtures.setColumnCount(2)
        self.tbl_target_fixtures.setHorizontalHeaderLabels(["Target", "Fixture Name"])

        for r, (t, n) in enumerate(rows):
            it = QTableWidgetItem(t)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tbl_target_fixtures.setItem(r, 0, it)

            it_n = QTableWidgetItem(n)
            it_n.setFlags(it_n.flags() & ~Qt.ItemIsEditable)
            self.tbl_target_fixtures.setItem(r, 1, it_n)

        self.tbl_target_fixtures.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_target_fixtures.horizontalHeader().setStretchLastSection(True)
        self.tbl_target_fixtures.blockSignals(False)

    def _refresh_multiplexer_ui(self, *args):
        if not hasattr(self, 'multiplexed_params'): return
        is_linked = self.ui.chk_link_all_dynamics.isChecked() if hasattr(self.ui, 'chk_link_all_dynamics') else True

        if is_linked:
            fix_idx = 0
            if hasattr(self, 'btn_select_fixtures'):
                self.btn_select_fixtures.setText("🎯 Target: LINKED (Master Override)")
        else:
            if hasattr(self, 'tbl_target_fixtures'):
                selected_rows = list(set([item.row() for item in self.tbl_target_fixtures.selectedItems()]))
                if not selected_rows:
                    self.btn_select_fixtures.setText("🎯 Target: NONE")
                    fix_idx = 0
                elif 0 in selected_rows:
                    self.btn_select_fixtures.setText("🎯 Target: GLOBAL MASTER")
                    fix_idx = 0
                elif 1 in selected_rows:
                    self.btn_select_fixtures.setText("🎯 Target: ALL FIXTURES")
                    fix_idx = 0
                else:
                    self.btn_select_fixtures.setText(f"🎯 Target: {len(selected_rows)} Fixture(s) Selected")
                    target_txt = self.tbl_target_fixtures.item(selected_rows[0], 0).text()
                    fix_idx = int(target_txt.split(" ")[1])
            else:
                fix_idx = 0

        for name in self.multiplexed_params:
            val = float(self.params.get(name, 0.0)) if fix_idx == 0 else float(
                self.params.get(f"f{fix_idx}_{name}", self.params.get(name, 0.0)))

            spin = getattr(self.ui, f"spin_{name}", self.dyn_widgets.get(f"spin_{name}"))
            sld = getattr(self.ui, f"sld_{name}", None)
            chk = getattr(self.ui, f"chk_{name}", self.dyn_widgets.get(f"chk_{name}"))

            if spin:
                spin.blockSignals(True)
                try:
                    spin.setValue(val)
                except TypeError:
                    spin.setValue(int(val))
                spin.blockSignals(False)
            if sld:
                cfg = self.slider_cfg.get(name, {"min": 0.0, "max": 1.0})
                ratio = (val - cfg.get("min", 0.0)) / max(0.001, (cfg.get("max", 1.0) - cfg.get("min", 0.0)))
                sld.blockSignals(True)
                sld.setValue(int(ratio * 100))
                sld.blockSignals(False)
            if chk:
                chk.blockSignals(True)
                chk.setChecked(bool(val))
                chk.blockSignals(False)

    def _notify_gui_cache(self, name, val):
        if name == "master_inhibitive" and "update_ctrl_cache" in self.callbacks:
            self.callbacks["update_ctrl_cache"](0, int(val * 255))
        elif name == "color_r" and "update_ctrl_cache" in self.callbacks:
            self.callbacks["update_ctrl_cache"](1, int(val))
        elif name == "color_g" and "update_ctrl_cache" in self.callbacks:
            self.callbacks["update_ctrl_cache"](2, int(val))
        elif name == "color_b" and "update_ctrl_cache" in self.callbacks:
            self.callbacks["update_ctrl_cache"](3, int(val))
        elif name == "color_w" and "update_ctrl_cache" in self.callbacks:
            self.callbacks["update_ctrl_cache"](4, int(val))

    def _update_chk(self, name, val):
        self.app_state['gui_lock_time'] = time.time()
        if getattr(self, 'live_update', True):
            final_val = 1 if val else 0
            self._notify_gui_cache(name, final_val)

            if hasattr(self, 'multiplexed_params') and name in self.multiplexed_params:
                is_linked = self.ui.chk_link_all_dynamics.isChecked() if hasattr(self.ui,
                                                                                 'chk_link_all_dynamics') else True
                if is_linked:
                    self.params[name] = final_val
                    if "send_osc" in self.callbacks: self.callbacks["send_osc"](name, float(final_val))
                else:
                    selected_rows = list(
                        set([item.row() for item in self.tbl_target_fixtures.selectedItems()])) if hasattr(self,
                                                                                                           'tbl_target_fixtures') else []
                    if not selected_rows or 0 in selected_rows:
                        self.params[name] = final_val
                        if "send_osc" in self.callbacks: self.callbacks["send_osc"](name, float(final_val))
                    if 1 in selected_rows:
                        self.params[name] = final_val
                        num_fixes = int(self.params.get("num_fixtures", 1))
                        for i in range(1, num_fixes + 1):
                            self.params[f"f{i}_{name}"] = final_val
                            if "send_osc" in self.callbacks: self.callbacks["send_osc"](f"f{i}_{name}",
                                                                                        float(final_val))
                    for r in selected_rows:
                        if r > 1:
                            target_txt = self.tbl_target_fixtures.item(r, 0).text()
                            f_idx = int(target_txt.split(' ')[1])
                            self.params[f"f{f_idx}_{name}"] = final_val
                            if "send_osc" in self.callbacks: self.callbacks["send_osc"](f"f{f_idx}_{name}",
                                                                                        float(final_val))
            else:
                self.params[name] = final_val
                if "send_osc" in self.callbacks: self.callbacks["send_osc"](name, float(final_val))

    def _update_param(self, name, val, companion_widget, is_spin=False):
        self.app_state['gui_lock_time'] = time.time()
        cfg = self.slider_cfg.get(name, {"min": 0.0, "max": 1.0})
        if is_spin:
            final_val = val
            if companion_widget:
                ratio = (val - cfg.get("min", 0.0)) / max(0.001, (cfg.get("max", 1.0) - cfg.get("min", 0.0)))
                companion_widget.blockSignals(True)
                companion_widget.setValue(int(ratio * 100))
                companion_widget.blockSignals(False)
        else:
            final_val = cfg.get("min", 0.0) + (val / 100.0) * (cfg.get("max", 1.0) - cfg.get("min", 0.0))
            if companion_widget:
                companion_widget.blockSignals(True)
                companion_widget.setValue(final_val)
                companion_widget.blockSignals(False)

        if getattr(self, 'live_update', True):
            self._notify_gui_cache(name, final_val)
            if hasattr(self, 'multiplexed_params') and name in self.multiplexed_params:
                is_linked = self.ui.chk_link_all_dynamics.isChecked() if hasattr(self.ui,
                                                                                 'chk_link_all_dynamics') else True
                if is_linked:
                    self.params[name] = final_val
                    if "send_osc" in self.callbacks: self.callbacks["send_osc"](name, final_val)
                else:
                    selected_rows = list(
                        set([item.row() for item in self.tbl_target_fixtures.selectedItems()])) if hasattr(self,
                                                                                                           'tbl_target_fixtures') else []
                    if not selected_rows or 0 in selected_rows:
                        self.params[name] = final_val
                        if "send_osc" in self.callbacks: self.callbacks["send_osc"](name, final_val)
                    if 1 in selected_rows:
                        self.params[name] = final_val
                        num_fixes = int(self.params.get("num_fixtures", 1))
                        for i in range(1, num_fixes + 1):
                            self.params[f"f{i}_{name}"] = final_val
                            if "send_osc" in self.callbacks: self.callbacks["send_osc"](f"f{i}_{name}", final_val)
                    for r in selected_rows:
                        if r > 1:
                            target_txt = self.tbl_target_fixtures.item(r, 0).text()
                            f_idx = int(target_txt.split(' ')[1])
                            self.params[f"f{f_idx}_{name}"] = final_val
                            if "send_osc" in self.callbacks: self.callbacks["send_osc"](f"f{f_idx}_{name}", final_val)
            else:
                self.params[name] = final_val
                if "send_osc" in self.callbacks: self.callbacks["send_osc"](name, final_val)

    def apply_changes(self):
        try:
            is_linked = self.ui.chk_link_all_dynamics.isChecked() if hasattr(self.ui, 'chk_link_all_dynamics') else True
            selected_rows = []
            if not is_linked and hasattr(self, 'tbl_target_fixtures'):
                selected_rows = list(set([item.row() for item in self.tbl_target_fixtures.selectedItems()]))

            for name in list(self.params.keys()):
                spin = getattr(self.ui, f"spin_{name}", self.dyn_widgets.get(f"spin_{name}"))
                chk = getattr(self.ui, f"chk_{name}", self.dyn_widgets.get(f"chk_{name}"))
                txt = getattr(self.ui, f"txt_{name}", self.dyn_widgets.get(f"txt_{name}"))

                val = None
                if spin:
                    val = spin.value()
                elif chk:
                    val = 1 if chk.isChecked() else 0
                elif txt:
                    val = txt.text()

                if val is not None:
                    self._notify_gui_cache(name, val)
                    if hasattr(self, 'multiplexed_params') and name in self.multiplexed_params:
                        if is_linked:
                            self.params[name] = val
                            if "send_osc" in self.callbacks and isinstance(val, (int, float)):
                                self.callbacks["send_osc"](name, float(val))
                        else:
                            if not selected_rows or 0 in selected_rows:
                                self.params[name] = val
                                if "send_osc" in self.callbacks and isinstance(val, (int, float)):
                                    self.callbacks["send_osc"](name, float(val))
                            if 1 in selected_rows:
                                self.params[name] = val
                                num_fixes = int(self.params.get("num_fixtures", 1))
                                for i in range(1, num_fixes + 1):
                                    self.params[f"f{i}_{name}"] = val
                                    if "send_osc" in self.callbacks and isinstance(val, (int, float)):
                                        self.callbacks["send_osc"](f"f{i}_{name}", float(val))
                            for r in selected_rows:
                                if r > 1:
                                    target_txt = self.tbl_target_fixtures.item(r, 0).text()
                                    fix_idx = int(target_txt.split(' ')[1])
                                    self.params[f"f{fix_idx}_{name}"] = val
                                    if "send_osc" in self.callbacks and isinstance(val, (int, float)):
                                        self.callbacks["send_osc"](f"f{fix_idx}_{name}", float(val))
                    else:
                        self.params[name] = val
                        if "send_osc" in self.callbacks and isinstance(val, (int, float)):
                            self.callbacks["send_osc"](name, float(val))
            self._update_dmx_overlay()
        except Exception as e:
            logger.error(f"apply_changes failed: {e}")

    def refresh_logic(self):
        if hasattr(self, 'lbl_stat_fps'):
            self.lbl_stat_fps.setText(f"{self.app_state.get('current_fps', 0.0):.1f}")

            dsp_time = self.engine.dsp_latency_ms
            self.lbl_stat_dsp.setText(f"{dsp_time:.2f} ms")
            if dsp_time > 25.0:
                self.lbl_stat_dsp.setStyleSheet("color: #ff0000; font-weight: bold;")
            elif dsp_time > 15.0:
                self.lbl_stat_dsp.setStyleSheet("color: #ffff00; font-weight: bold;")
            else:
                self.lbl_stat_dsp.setStyleSheet("color: #00ff00; font-weight: bold;")

            self.lbl_stat_buf.setText(f"{self.engine.audio_latency_ms:.1f} ms")

            if hasattr(self, 'lbl_stat_web'):
                web_ip = self.app_state.get("web_ip")
                web_port = self.app_state.get("web_port")
                if web_ip and web_port:
                    self.lbl_stat_web.setText(f"http://{web_ip}:{web_port}")

        if self.app_state.get("pending_preset"):
            preset_file = self.app_state.get("pending_preset")
            self.app_state["pending_preset"] = None
            filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), preset_file)
            if os.path.exists(filepath):
                self.load_config(filepath)

        if hasattr(self, 'curve_audio'):
            snap_buffers, (snap_audio, snap_center, snap_edge) = self.engine.get_snapshot()
            self.curve_audio.setData(snap_audio)
            self.curve_center.setData(snap_center)
            self.curve_edge.setData(snap_edge)
            self._refresh_fixture_preview(snap_buffers)

            if hasattr(self, 'dmx_boxes'):
                mon_univ = self.spin_mon_univ.value()
                current_univ_data = snap_buffers.get(mon_univ, bytes(512))
                mode = getattr(self, "cmb_view_mode", None)
                is_pixel = mode and mode.currentText() == "RGBW Pixels"

                if is_pixel:
                    cache = self.last_rendered_pixel
                    if len(cache) != len(self.pixel_map):
                        # Sizes can drift if the grid was rebuilt while a frame
                        # was in flight; re-sync to the current pixel_map.
                        cache = [None] * len(self.pixel_map)
                        self.last_rendered_pixel = cache
                    for i, (addr, foot, name, p_num) in enumerate(self.pixel_map):
                        if i < len(self.dmx_boxes):
                            r, g, b, w = 0, 0, 0, 0
                            if addr < 512: r = current_univ_data[addr]
                            if foot > 1 and addr + 1 < 512: g = current_univ_data[addr + 1]
                            if foot > 2 and addr + 2 < 512: b = current_univ_data[addr + 2]
                            if foot > 3 and addr + 3 < 512: w = current_univ_data[addr + 3]

                            sr = min(255, r + w)
                            sg = min(255, g + w)
                            sb = min(255, b + w)

                            key = (sr, sg, sb)
                            if cache[i] == key:
                                continue
                            cache[i] = key

                            bg_color = f"rgb({sr},{sg},{sb})"
                            lum = 0.299 * sr + 0.587 * sg + 0.114 * sb
                            txt_color = "#000" if lum > 128 else "#aaa"
                            self.dmx_boxes[i].setStyleSheet(
                                f"background-color: {bg_color}; color: {txt_color}; border: 1px solid #444; font-size: 11px; font-weight: bold;")
                else:
                    for i in range(512):
                        val = current_univ_data[i]
                        if val != self.last_rendered_dmx[i]:
                            self.last_rendered_dmx[i] = val
                            if val == 0:
                                bg_color = "#111111"
                                txt_color = "#555555"
                            else:
                                if val < 128:
                                    r = int((val / 127.0) * 255)
                                    g = 255
                                else:
                                    r = 255
                                    g = int(255 - ((val - 128) / 127.0) * 255)
                                bg_color = f"rgb({r},{g},0)"
                                txt_color = "#000000"

                            self.dmx_boxes[i].setStyleSheet(
                                f"background-color: {bg_color}; color: {txt_color}; font-size: 11px; font-weight: bold;")
                            self.dmx_boxes[i].setText(str(val))

        if hasattr(self, 'app_state'):
            # --- 1. AUDIO STATUS ---
            if self.app_state.get("port_conflict_osc"):
                audio_text, audio_css = "🔴 PORT IN USE", "color: #ff0000; font-weight: bold;"
            elif time.time() - self.app_state.get("pd_last_time", 0) < 0.1:
                audio_text, audio_css = "🟢 ACTIVE", "color: #00ff00; font-weight: bold;"
            else:
                audio_text, audio_css = "🔴 WAIT", "color: #ff0000; font-weight: normal;"
            self.ui.lbl_stat_audio.setText(audio_text)
            self.ui.lbl_stat_audio.setStyleSheet(audio_css)
            if hasattr(self, 'lbl_stat_audio_inline'):
                self.lbl_stat_audio_inline.setText(audio_text)
                self.lbl_stat_audio_inline.setStyleSheet(audio_css)

            # --- 2. NETWORK OUTPUT STATUS ---
            if self.app_state.get("artnet_active", False):
                protocol_str = self.params.get("protocol", "Art-Net")
                net_mode_str = self.params.get("net_mode", "Unicast")
                if hasattr(self.ui, 'lbl_title_artnet'):
                    self.ui.lbl_title_artnet.setText(f"{protocol_str} ({net_mode_str}):")

                if self.app_state.get("port_conflict_artnet"):
                    self.ui.lbl_stat_artnet.setText("🔴 PORT IN USE")
                    self.ui.lbl_stat_artnet.setStyleSheet("color: #ff0000; font-weight: bold;")
                elif self.app_state.get("art_packets", 0) == 0 or self.app_state.get("send_error"):
                    self.ui.lbl_stat_artnet.setText("🟡 BLOCKED")
                    self.ui.lbl_stat_artnet.setStyleSheet("color: #ffff00; font-weight: bold;")
                else:
                    self.ui.lbl_stat_artnet.setText(f"🟢 TX ({self.app_state.get('art_packets', 0)})")
                    self.ui.lbl_stat_artnet.setStyleSheet("color: #00ff00; font-weight: bold;")
            else:
                self.ui.lbl_stat_artnet.setText("🔴 OFF")
                self.ui.lbl_stat_artnet.setStyleSheet("color: #ff0000; font-weight: normal;")

            # --- 3. REMOTE CONTROL STATUS ---
            if int(self.params.get("remote_on", 1)) == 0:
                osc_in_txt = "⚫ DISABLED"
            else:
                osc_in_txt = self.app_state.get("osc_in_text", "🔴 WAIT")

            if hasattr(self.ui, 'lbl_stat_osc_in'):
                if self.app_state.get("port_conflict_artnet"):
                    self.ui.lbl_stat_osc_in.setText("🔴 PORT IN USE")
                    self.ui.lbl_stat_osc_in.setStyleSheet("color: #ff0000; font-weight: bold;")
                else:
                    self.ui.lbl_stat_osc_in.setText(osc_in_txt)
                    if "🟢" in osc_in_txt or "RX" in osc_in_txt:
                        self.ui.lbl_stat_osc_in.setStyleSheet("color: #00ff00; font-weight: bold;")
                    elif "⚫" in osc_in_txt:
                        self.ui.lbl_stat_osc_in.setStyleSheet("color: #888; font-weight: normal;")
                    else:
                        self.ui.lbl_stat_osc_in.setStyleSheet("color: #ff0000; font-weight: normal;")

            # --- 4. TEST TONE STATUS ---
            if hasattr(self, 'lbl_stat_test'):
                is_test_on = int(self.params.get("test_on", 0)) == 1
                has_remote = "🟢" in self.app_state.get("osc_in_text", "")

                if is_test_on and has_remote:
                    self.lbl_stat_test.setText("🟡 OVERRIDE")
                    self.lbl_stat_test.setStyleSheet("color: #ffff00; font-weight: bold;")
                elif is_test_on:
                    self.lbl_stat_test.setText("🟢 ON")
                    self.lbl_stat_test.setStyleSheet("color: #00ff00; font-weight: bold;")
                else:
                    self.lbl_stat_test.setText("⚫ OFF")
                    self.lbl_stat_test.setStyleSheet("color: #555555; font-weight: normal;")

            # --- 5. SYNC SLIDERS TO INCOMING DMX ---
            # If remote is ON and receiving data, make the sliders physically follow the DMX
            if "🟢" in self.app_state.get("osc_in_text", ""):
                for name, cfg in self.slider_cfg.items():
                    # Handle the Multiplexer targeting logic
                    target_name = name
                    if hasattr(self, 'multiplexed_params') and name in self.multiplexed_params:
                        is_linked = self.ui.chk_link_all_dynamics.isChecked() if hasattr(self.ui,
                                                                                         'chk_link_all_dynamics') else True
                        if not is_linked and hasattr(self, 'tbl_target_fixtures'):
                            sel = [item.row() for item in self.tbl_target_fixtures.selectedItems()]
                            if sel and sel[0] > 1:  # If a specific fixture is selected instead of Global
                                f_idx = int(self.tbl_target_fixtures.item(sel[0], 0).text().split(' ')[1])
                                target_name = f"f{f_idx}_{name}"

                    if target_name in self.params:
                        val = float(self.params[target_name])

                        # Update the Number Box safely
                        spin = getattr(self.ui, f"spin_{name}", self.dyn_widgets.get(f"spin_{name}"))
                        if spin and not spin.hasFocus():  # Don't fight the user if they are typing
                            spin.blockSignals(True)
                            try:
                                spin.setValue(val)
                            except TypeError:
                                spin.setValue(int(val))
                            spin.blockSignals(False)

                        # Update the Visual Slider safely
                        sld = getattr(self.ui, f"sld_{name}", None)
                        if sld and not sld.isSliderDown():  # Don't fight the user if they are clicking it
                            ratio = (val - cfg.get("min", 0.0)) / max(0.001, (
                                        cfg.get("max", 1.0) - cfg.get("min", 0.0)))
                            target_sld = int(ratio * 100)
                            if sld.value() != target_sld:
                                sld.blockSignals(True)
                                sld.setValue(target_sld)
                                sld.blockSignals(False)

        if "discovered_nodes" in self.app_state:
            self._update_artpoll_table()