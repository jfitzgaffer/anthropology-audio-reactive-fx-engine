from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QFrame, QLabel, QSpinBox, QCheckBox, QLineEdit, QGroupBox)
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QPen, QColor, QFont

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtCore import Qt


class DMXGridOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.active_zones = []
        self.dmx_containers = []
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # (eraseRect removed)

        if not self.active_zones or not self.dmx_containers:
            painter.end()
            return

        for start_idx, end_idx, label in self.active_zones:
            if start_idx < len(self.dmx_containers) and end_idx < len(self.dmx_containers):
                w_start = self.dmx_containers[start_idx]
                w_end = self.dmx_containers[end_idx]

                rect = w_start.geometry().united(w_end.geometry())
                rect.adjust(-2, -2, 2, 2)

                painter.setPen(QPen(QColor(255, 255, 0, 200), 2))
                painter.setBrush(QColor(255, 255, 0, 40))
                painter.drawRect(rect)

                if label:
                    painter.setPen(QColor(255, 255, 0, 255))
                    font = painter.font()
                    font.setPointSize(9)
                    font.setBold(True)
                    painter.setFont(font)
                    painter.drawText(rect.adjusted(2, -16, 0, 0), Qt.AlignLeft | Qt.AlignTop, label)

        painter.end()


class FixturePatchWidget(QFrame):
    def __init__(self, f_idx, params, update_cb, update_txt_cb, spin_patch_cb):
        super().__init__()
        self.f_idx = f_idx
        self.params = params
        self.setStyleSheet("QFrame { border: 1px solid #444; border-radius: 4px; margin-top: 2px; background: #222; }")
        layout = QVBoxLayout(self)

        # Header
        header = QHBoxLayout()
        self.chk_active = QCheckBox("")
        self.chk_active.setChecked(bool(params.get(f"f{f_idx}_active", 1)))
        self.chk_active.toggled.connect(lambda v: update_cb(f"f{f_idx}_active", v))

        self.txt_name = QLineEdit(params.get(f"f{f_idx}_name", f"Fixture {f_idx}"))
        self.txt_name.setStyleSheet("font-weight: bold; border: none; background: transparent; color: #fff;")
        self.txt_name.textChanged.connect(lambda v: update_txt_cb(f"f{f_idx}_name", v))

        header.addWidget(self.chk_active)
        header.addWidget(self.txt_name)
        header.addStretch()
        layout.addLayout(header)

        cols = QHBoxLayout()

        # Addressing
        g_addr = QGroupBox("Addressing")
        l_addr = QHBoxLayout(g_addr)
        self.spin_uni = self._make_spin(0, 9999, 50, f"f{f_idx}_uni", spin_patch_cb)
        self.spin_foot = self._make_spin(1, 10, 40, f"f{f_idx}_foot", spin_patch_cb)
        self.spin_addr = self._make_spin(1, 512, 50, f"f{f_idx}_addr", spin_patch_cb)
        self.spin_pix = self._make_spin(1, 512, 50, f"f{f_idx}_pix", spin_patch_cb)
        l_addr.addWidget(QLabel("U:"))
        l_addr.addWidget(self.spin_uni)
        l_addr.addWidget(QLabel("Ch:"))
        l_addr.addWidget(self.spin_foot)
        l_addr.addWidget(QLabel("A:"))
        l_addr.addWidget(self.spin_addr)
        l_addr.addWidget(QLabel("Px:"))
        l_addr.addWidget(self.spin_pix)
        cols.addWidget(g_addr)

        # Routing
        g_route = QGroupBox("Routing")
        l_route = QHBoxLayout(g_route)
        self.chk_extend = self._make_chk("Ext", f"f{f_idx}_extend", update_cb)
        self.chk_flip = self._make_chk("Flip", f"f{f_idx}_flip", update_cb)
        self.chk_align = self._make_chk("ALIGN", f"f{f_idx}_align", update_cb, "#ffaa00")
        l_route.addWidget(self.chk_extend)
        l_route.addWidget(self.chk_flip)
        l_route.addWidget(self.chk_align)
        cols.addWidget(g_route)

        # Effects
        g_fx = QGroupBox("Effect Toggles")
        l_fx = QHBoxLayout(g_fx)
        self.chk_ana = self._make_chk("Static", f"f{f_idx}_glitch_ana", update_cb)
        self.chk_digi = self._make_chk("Digi", f"f{f_idx}_glitch_digi", update_cb)
        self.chk_dist = self._make_chk("Distortion", f"f{f_idx}_od_en", update_cb)
        l_fx.addWidget(self.chk_ana)
        l_fx.addWidget(self.chk_digi)
        l_fx.addWidget(self.chk_dist)
        cols.addWidget(g_fx)

        layout.addLayout(cols)

    def _make_spin(self, mn, mx, w, name, cb):
        s = QSpinBox()
        s.setRange(mn, mx)
        s.setFixedWidth(w)
        s.setValue(int(self.params.get(name, 0)))
        s.valueChanged.connect(lambda v: cb(name, v))
        return s

    def _make_chk(self, lbl, name, cb, color=None):
        c = QCheckBox(lbl)
        c.setChecked(bool(self.params.get(name, 0)))
        if color: c.setStyleSheet(f"color: {color}; font-weight: bold;")
        c.toggled.connect(lambda v: cb(name, v))
        return c