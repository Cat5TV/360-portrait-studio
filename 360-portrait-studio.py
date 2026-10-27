# ==============================================================================
# Baldnerd's 360 Portrait Studio
# Created by Robbie Ferguson // The Bald Nerd
#
# Repository: https://github.com/Cat5TV/360-portrait-studio
# ==============================================================================

import sys
import cv2
import numpy as np
import os
import urllib.request
import json
import time
import copy

try:
    import piexif
    import piexif.helper
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QSlider, QFileDialog, QComboBox,
                             QSizePolicy, QCheckBox, QMessageBox, QToolButton, QDialog, QScrollArea)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QUrl, QVariantAnimation, QSettings
from PyQt6.QtGui import QImage, QPixmap, QShortcut, QKeySequence, QDesktopServices, QPainter, QColor, QPen

VERSION = "1.0"

class EquirectangularProjector:
    def __init__(self, image_path):
        self.image_path = image_path
        self.img = cv2.imread(image_path, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
        self.height, self.width = self.img.shape[:2]

    def get_projection(self, yaw, pitch, roll, fov, aspect_ratio, dewarp=0, preview_width=None):
        pixels_per_degree = self.width / 360.0
        out_w = preview_width if preview_width else int(fov * pixels_per_degree)
        out_h = int(out_w / aspect_ratio)

        yaw, pitch, roll = np.radians(yaw), np.radians(pitch), np.radians(roll)
        fov_rad = np.radians(fov)

        x = np.linspace(-1, 1, out_w)
        y = np.linspace(-1 / aspect_ratio, 1 / aspect_ratio, out_h)
        x_mesh, y_mesh = np.meshgrid(x, y)

        r_sq = x_mesh**2 + y_mesh**2
        factor = 1.0 + ((dewarp / 100.0) * r_sq * 0.6)
        x_ray = x_mesh * factor
        y_ray = y_mesh * factor

        z = np.ones_like(x_mesh) / np.tan(fov_rad / 2)
        rays = np.stack((x_ray, y_ray, z), axis=-1)

        Rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
        Ry = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
        Rz = np.array([[np.cos(roll), -np.sin(roll), 0], [np.sin(roll), np.cos(roll), 0], [0, 0, 1]])

        R = Ry @ Rx @ Rz
        rays_rotated = np.einsum('ij,klj->kli', R, rays)

        norm = np.linalg.norm(rays_rotated, axis=-1)
        theta = np.arctan2(rays_rotated[..., 0], rays_rotated[..., 2])
        phi = np.arcsin(rays_rotated[..., 1] / norm)

        U = (theta / (2 * np.pi) + 0.5) * self.width
        V = (phi / np.pi + 0.5) * self.height

        map_x = U.astype(np.float32)
        map_y = V.astype(np.float32)

        return cv2.remap(self.img, map_x, map_y, interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_WRAP)

class InteractiveImageLabel(QLabel):
    dragged = pyqtSignal(float, float)
    scrolled = pyqtSignal(int)
    clicked_color = pyqtSignal(int, int)
    interaction_ended = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #111; color: #777; font-size: 16px;")
        self.setText("Load a 360 Image to begin.")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 400)
        self.last_pos = None
        self.current_pixmap = None
        self.old_pixmap = None
        self.raw_preview_img = None

        self.show_grid = False
        self.overlay_text = ""
        self.eyedropper_mode = False

        self.fade_anim = QVariantAnimation(self)
        self.fade_anim.setDuration(250)
        self.fade_anim.valueChanged.connect(self._update_fade)
        self.fade_anim.finished.connect(self._fade_finished)
        self.fade_alpha = 1.0

    def set_master_pixmap(self, pixmap, raw_img, fade=False):
        self.raw_preview_img = raw_img
        if fade and self.current_pixmap:
            self.old_pixmap = self.current_pixmap
            self.current_pixmap = pixmap
            self.fade_anim.setStartValue(0.0)
            self.fade_anim.setEndValue(1.0)
            self.fade_anim.start()
        else:
            self.fade_anim.stop()
            self.old_pixmap = None
            self.current_pixmap = pixmap
            self.fade_alpha = 1.0
            self.update()

    def _update_fade(self, value):
        self.fade_alpha = value
        self.update()

    def _fade_finished(self):
        self.old_pixmap = None
        self.fade_alpha = 1.0
        self.update()

    def store_bypass_cache(self):
        self.bypass_pixmap_cache = self.current_pixmap
        self.bypass_text_cache = self.overlay_text

    def restore_bypass_cache(self):
        if hasattr(self, 'bypass_pixmap_cache') and self.bypass_pixmap_cache:
            self.fade_anim.stop()
            self.old_pixmap = None
            self.current_pixmap = self.bypass_pixmap_cache
            self.overlay_text = getattr(self, 'bypass_text_cache', "")
            self.fade_alpha = 1.0
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#111"))

        if not self.current_pixmap and not self.old_pixmap:
            painter.setPen(QColor("#777"))
            font = painter.font()
            font.setPixelSize(16)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
            return

        def draw_pm(pm, alpha):
            scaled = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.setOpacity(alpha)
            painter.drawPixmap(x, y, scaled)
            return x, y, scaled.width(), scaled.height()

        x, y, w, h = 0, 0, 0, 0
        if self.old_pixmap:
            draw_pm(self.old_pixmap, 1.0)
            x, y, w, h = draw_pm(self.current_pixmap, self.fade_alpha)
        elif self.current_pixmap:
            x, y, w, h = draw_pm(self.current_pixmap, 1.0)

        if self.show_grid and w > 0:
            painter.setOpacity(0.4)
            pen = QPen(Qt.GlobalColor.white, max(1, h // 400))
            painter.setPen(pen)

            painter.drawLine(x + w//3, y, x + w//3, y + h)
            painter.drawLine(x + 2*w//3, y, x + 2*w//3, y + h)
            painter.drawLine(x, y + h//3, x + w, y + h//3)
            painter.drawLine(x, y + 2*h//3, x + w, y + 2*h//3)

            painter.setOpacity(0.6)
            cx, cy = x + w//2, y + h//2
            c_len = min(w, h) // 20
            painter.drawLine(cx - c_len, cy, cx + c_len, cy)
            painter.drawLine(cx, cy - c_len, cx, cy + c_len)

        if self.overlay_text and w > 0:
            painter.setOpacity(1.0)

            if self.overlay_text == "PREVIEW":
                painter.setPen(QColor(0, 215, 255))
            elif self.overlay_text == "BYPASS":
                painter.setPen(QColor(255, 50, 50))
            else:
                painter.setPen(QColor(255, 165, 0))

            font = painter.font()
            font.setPixelSize(24)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(x + 20, y + 40, self.overlay_text)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.eyedropper_mode and self.raw_preview_img is not None:
                h, w = self.raw_preview_img.shape[:2]
                cx, cy = w//2, h//2
                b, g, r = self.raw_preview_img[cy, cx]
                self.clicked_color.emit(int(r), int(b))
                self.eyedropper_mode = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
            else:
                self.last_pos = event.pos()

    def mouseMoveEvent(self, event):
        if self.last_pos and not self.eyedropper_mode:
            dx = float(event.pos().x() - self.last_pos.x())
            dy = float(event.pos().y() - self.last_pos.y())
            self.dragged.emit(dx, dy)
            self.last_pos = event.pos()

    def mouseReleaseEvent(self, event):
        self.last_pos = None
        self.interaction_ended.emit()

    def wheelEvent(self, event):
        self.scrolled.emit(event.angleDelta().y())

class CollapsibleBox(QWidget):
    def __init__(self, title="", parent=None, expanded=False):
        super().__init__(parent)
        self.toggle_button = QToolButton(text=title, checkable=True, checked=expanded)
        self.toggle_button.setStyleSheet("QToolButton { border: none; font-weight: bold; text-align: left; background-color: #e0e0e0; padding: 5px; }")
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.toggle_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(5, 5, 5, 5)
        self.content_area.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content_area)

    def set_expanded(self, expanded):
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content_area.setVisible(expanded)
        self.toggle_button.blockSignals(False)

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Baldnerd's 360 Portrait Studio")
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)

        text = f"""
        <h3>Baldnerd's 360 Portrait Studio v{VERSION}</h3>
        <p>Created by Robbie Ferguson // The Bald Nerd</p>
        <p>Robbie is the founder of Category5 TV Network, host of several technology broadcasts, 
        and the creator of NEMS Linux.</p>
        <p>I build this software and many other creative projects to share with the community for free. 
        If you find it useful, please consider supporting my work!</p>
        <p>Visit <a href="https://baldnerd.com/">https://baldnerd.com/</a> to learn more.</p>
        """
        lbl = QLabel(text)
        lbl.setOpenExternalLinks(True)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(lbl)

        btn_layout = QHBoxLayout()

        btn_update = QPushButton("Check for Updates")
        btn_update.clicked.connect(self.check_for_updates)

        btn_donate = QPushButton("Donate")
        btn_donate.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://donate.category5.tv/")))

        btn_patron = QPushButton("Become a Patron")
        btn_patron.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://patreon.com/Category5")))

        btn_layout.addWidget(btn_update)
        btn_layout.addWidget(btn_donate)
        btn_layout.addWidget(btn_patron)

        layout.addLayout(btn_layout)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def check_for_updates(self):
        try:
            base_url = "https://raw.githubusercontent.com/Cat5TV/360-portrait-studio/refs/heads/main/currentversion.json"
            url = f"{base_url}?nocache={int(time.time())}"

            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            })

            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))

            latest_version = data.get("currentVersion", "")

            if not latest_version:
                QMessageBox.warning(self, "Update Check", "Could not parse the version from the update server.")
                return

            if latest_version != VERSION:
                msg = f"A new version (<b>v{latest_version}</b>) is available!<br>You are currently running v{VERSION}.<br><br>"

                summary = data.get("updateSummary", "")
                if summary:
                    msg += f"<b>Summary:</b> {summary}<br><br>"

                changes = data.get("changes", [])
                if changes:
                    msg += "<b>Changes:</b><ul>"
                    for change in changes:
                        msg += f"<li>{change}</li>"
                    msg += "</ul><br>"

                download_url = data.get("downloadUrl", "https://github.com/Cat5TV/360-portrait-studio")
                msg += f"Please visit the <a href='{download_url}'>repository</a> to download the latest release."

                box = QMessageBox(self)
                box.setWindowTitle("Update Available")
                box.setTextFormat(Qt.TextFormat.RichText)
                box.setText(msg)
                box.exec()
            else:
                QMessageBox.information(self, "Up to Date", f"You are running the latest version (v{VERSION}).")

        except Exception as e:
            QMessageBox.warning(self, "Update Check Failed", f"Could not connect to the update server.\n\nError: {e}")

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Baldnerd's 360 Portrait Studio v{VERSION}")
        self.resize(1300, 900)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.projector = None

        self.float_yaw = 0.0
        self.float_pitch = 0.0
        self.base_proj_img = None

        self.is_resetting = False
        self.is_bypassed = False
        self.settings_changed_while_bypassed = False

        self.history = []
        self.history_index = -1
        self.history_timer = QTimer(self)
        self.history_timer.setSingleShot(True)
        self.history_timer.timeout.connect(self._save_state)

        self.settings = QSettings("Baldnerd", "360PortraitStudio")
        self.last_load_dir = self.settings.value("last_load_dir", "")
        self.last_export_dir = self.settings.value("last_export_dir", "")
        self.current_exif = None

        self.fast_timer = QTimer(self)
        self.fast_timer.setSingleShot(True)
        self.fast_timer.timeout.connect(self._render_fast)

        self.medium_timer = QTimer(self)
        self.medium_timer.setSingleShot(True)
        self.medium_timer.timeout.connect(self._render_medium)

        self.high_timer = QTimer(self)
        self.high_timer.setSingleShot(True)
        self.high_timer.timeout.connect(self._render_high)

        self.ultra_timer = QTimer(self)
        self.ultra_timer.setSingleShot(True)
        self.ultra_timer.timeout.connect(self._render_ultra)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        self.image_label = InteractiveImageLabel()
        self.image_label.dragged.connect(self.handle_image_drag)
        self.image_label.interaction_ended.connect(self._save_state)
        self.image_label.scrolled.connect(self.handle_scroll)
        self.image_label.clicked_color.connect(self.handle_eyedropper)
        layout.addWidget(self.image_label, stretch=4)

        control_panel = QVBoxLayout()
        control_panel.setContentsMargins(10, 10, 10, 10)
        layout.addLayout(control_panel, stretch=1)

        # --- Top Buttons ---
        top_btn_layout = QHBoxLayout()
        self.btn_load = QPushButton("Load Image")
        self.btn_load.clicked.connect(self.load_image)
        self.btn_load.setMinimumHeight(35)
        self.btn_load.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.btn_import = QPushButton("Import Settings")
        self.btn_import.clicked.connect(self.import_settings)
        self.btn_import.setMinimumHeight(35)
        self.btn_import.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.btn_about = QPushButton("About")
        self.btn_about.clicked.connect(self.show_about)
        self.btn_about.setMinimumHeight(35)
        self.btn_about.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        top_btn_layout.addWidget(self.btn_load)
        top_btn_layout.addWidget(self.btn_import)
        top_btn_layout.addWidget(self.btn_about)
        control_panel.addLayout(top_btn_layout)

        row_two_btns = QHBoxLayout()
        self.btn_reset_all = QPushButton("Reset All")
        self.btn_reset_all.clicked.connect(self.reset_all_settings)
        self.btn_reset_all.setMinimumHeight(35)
        self.btn_reset_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.btn_bypass = QPushButton("Bypass")
        self.btn_bypass.setMinimumHeight(35)
        self.btn_bypass.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_bypass.pressed.connect(self._start_bypass)
        self.btn_bypass.released.connect(self._stop_bypass)

        row_two_btns.addWidget(self.btn_reset_all)
        row_two_btns.addWidget(self.btn_bypass)
        control_panel.addLayout(row_two_btns)

        # Aspect, Orientation, and Grid Row
        row_aspect = QHBoxLayout()
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["4:6", "5:7", "8:10", "16:9", "1:1"])
        self.aspect_combo.currentIndexChanged.connect(self.update_view)
        self.aspect_combo.activated.connect(self._save_state)
        self.aspect_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Landscape", "Portrait"])
        self.orient_combo.currentIndexChanged.connect(self.update_view)
        self.orient_combo.activated.connect(self._save_state)
        self.orient_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.chk_grid = QCheckBox("Show Grid")
        grid_saved = self.settings.value("show_grid", False, type=bool)
        self.chk_grid.setChecked(grid_saved)
        self.chk_grid.stateChanged.connect(self._reapply_grid)
        self.chk_grid.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        row_aspect.addWidget(QLabel("Ratio:"))
        row_aspect.addWidget(self.aspect_combo)
        row_aspect.addWidget(self.orient_combo)
        row_aspect.addSpacing(10)
        row_aspect.addWidget(self.chk_grid)
        control_panel.addLayout(row_aspect)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; } QWidget#scroll_content { background: transparent; }")

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("scroll_content")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)

        self.sliders = {}
        self.boxes = []

        # --- Collapsible: Positioning ---
        self.pos_box = CollapsibleBox("Positioning (Pan/Tilt/Zoom)", expanded=False)
        self.sliders['yaw'] = self.create_slider_row(self.pos_box.content_layout, "Pan (Yaw):", -180, 180, 0)
        self.sliders['pitch'] = self.create_slider_row(self.pos_box.content_layout, "Tilt (Pitch):", -90, 90, 0)
        self.sliders['roll'] = self.create_slider_row(self.pos_box.content_layout, "Horizon (Roll):", -180, 180, 0)
        self.sliders['zoom'] = self.create_slider_row(self.pos_box.content_layout, "Zoom:", 0, 110, 30)
        self.boxes.append(self.pos_box)

        # --- Collapsible: Color ---
        self.color_box = CollapsibleBox("Color Adjustments", expanded=False)
        self.create_wb_row(self.color_box.content_layout)
        self.sliders['bright'] = self.create_slider_row(self.color_box.content_layout, "Brightness:", -100, 100, 0)
        self.sliders['contrast'] = self.create_slider_row(self.color_box.content_layout, "Contrast:", -50, 50, 0)
        self.sliders['shadows'] = self.create_slider_row(self.color_box.content_layout, "Boost Shadows:", 0, 100, 0)
        self.sliders['highlights'] = self.create_slider_row(self.color_box.content_layout, "Boost Highlights:", 0, 100, 0)
        self.sliders['sat'] = self.create_slider_row(self.color_box.content_layout, "Saturation:", -50, 100, 0)
        self.sliders['clarity'] = self.create_slider_row(self.color_box.content_layout, "Clarity (Auto Levels):", 0, 100, 0)
        self.boxes.append(self.color_box)

        # --- Collapsible: Effects ---
        self.fx_box = CollapsibleBox("Effects", expanded=False)
        self.sliders['noise'] = self.create_slider_row(self.fx_box.content_layout, "Noise Reduction:", 0, 100, 0)
        self.sliders['beautify'] = self.create_slider_row(self.fx_box.content_layout, "Beautify:", 0, 100, 0)
        self.sliders['sharpen'] = self.create_slider_row(self.fx_box.content_layout, "Sharpen:", 0, 100, 0)
        self.sliders['dof'] = self.create_slider_row(self.fx_box.content_layout, "Edge Blur:", 0, 50, 0)
        self.sliders['vig'] = self.create_slider_row(self.fx_box.content_layout, "Vignette Amount:", 0, 100, 0)
        self.boxes.append(self.fx_box)

        for box in self.boxes:
            box.toggle_button.toggled.connect(lambda checked, b=box: self._on_box_toggled(checked, b))
            self.scroll_layout.addWidget(box)

        self.scroll_layout.addStretch()
        self.scroll_area.setWidget(self.scroll_content)
        control_panel.addWidget(self.scroll_area)

        # --- Always Visible Controls ---
        always_visible_layout = QVBoxLayout()
        self.sliders['dewarp'] = self.create_slider_row(always_visible_layout, "Dewarp:", 0, 100, 0)
        control_panel.addLayout(always_visible_layout)

        # Export Button
        self.btn_save = QPushButton("Export")
        self.btn_save.clicked.connect(self.save_image)
        self.btn_save.setStyleSheet("background-color: #0078D7; color: white; padding: 12px; font-weight: bold; margin-top: 10px;")
        self.btn_save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        control_panel.addWidget(self.btn_save)

        # Keyboard shortcuts
        QShortcut(QKeySequence("Left"), self).activated.connect(lambda: self.keyboard_nudge(-0.02, 0))
        QShortcut(QKeySequence("Right"), self).activated.connect(lambda: self.keyboard_nudge(0.02, 0))
        QShortcut(QKeySequence("Up"), self).activated.connect(lambda: self.keyboard_nudge(0, 0.02))
        QShortcut(QKeySequence("Down"), self).activated.connect(lambda: self.keyboard_nudge(0, -0.02))

        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self).activated.connect(self.redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(self.redo)

        self._save_state()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._start_bypass()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._stop_bypass()
        super().keyReleaseEvent(event)

    def _start_bypass(self):
        if not self.projector: return
        self.is_bypassed = True
        self.settings_changed_while_bypassed = False
        self.btn_bypass.setDown(True)
        self.image_label.store_bypass_cache()
        self.refresh_display(hq_pass=False, overlay_text="BYPASS", fade=False)

    def _stop_bypass(self):
        if not self.projector: return
        self.is_bypassed = False
        self.btn_bypass.setDown(False)

        if getattr(self, 'settings_changed_while_bypassed', False):
            self.update_view()
        else:
            self.image_label.restore_bypass_cache()

    def _on_box_toggled(self, checked, clicked_box):
        if checked:
            clicked_box.content_area.setVisible(False)
            for b in self.boxes:
                if b != clicked_box:
                    b.set_expanded(False)
            clicked_box.content_area.setVisible(True)
        else:
            clicked_box.set_expanded(False)

    def _get_current_state(self):
        return {
            'sliders': {k: v.value() for k, v in self.sliders.items()},
            'aspect': self.aspect_combo.currentIndex(),
            'orient': self.orient_combo.currentIndex(),
            'float_yaw': self.float_yaw,
            'float_pitch': self.float_pitch
        }

    def _save_state(self):
        if self.is_resetting: return
        state = self._get_current_state()

        self.history = self.history[:self.history_index + 1]
        if self.history and self.history[-1] == state:
            return

        self.history.append(state)
        self.history_index += 1

        if len(self.history) > 50:
            self.history.pop(0)
            self.history_index -= 1

    def _restore_state(self, state):
        self.is_resetting = True

        self.aspect_combo.blockSignals(True)
        self.orient_combo.blockSignals(True)
        for s in self.sliders.values():
            s.blockSignals(True)

        self.aspect_combo.setCurrentIndex(state.get('aspect', 0))
        self.orient_combo.setCurrentIndex(state.get('orient', 0))

        self.float_yaw = state.get('float_yaw', 0.0)
        self.float_pitch = state.get('float_pitch', 0.0)

        slider_states = state.get('sliders', {})
        for k, v in slider_states.items():
            if k in self.sliders:
                self.sliders[k].setValue(v)

        self.aspect_combo.blockSignals(False)
        self.orient_combo.blockSignals(False)
        for s in self.sliders.values():
            s.blockSignals(False)

        self.is_resetting = False
        self.update_view()

    def undo(self):
        if self.history_index > 0:
            self.history_index -= 1
            self._restore_state(self.history[self.history_index])

    def redo(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self._restore_state(self.history[self.history_index])

    def reset_all_settings(self):
        self.is_resetting = True

        self.aspect_combo.setCurrentIndex(0)
        self.orient_combo.setCurrentIndex(0)

        self.float_yaw = 0.0
        self.float_pitch = 0.0

        self.sliders['yaw'].setValue(0)
        self.sliders['pitch'].setValue(0)
        self.sliders['roll'].setValue(0)
        self.sliders['zoom'].setValue(30)

        self.sliders['temp'].setValue(0)
        self.sliders['bright'].setValue(0)
        self.sliders['contrast'].setValue(0)
        self.sliders['shadows'].setValue(0)
        self.sliders['highlights'].setValue(0)
        self.sliders['sat'].setValue(0)
        self.sliders['clarity'].setValue(0)

        self.sliders['noise'].setValue(0)
        self.sliders['beautify'].setValue(0)
        self.sliders['sharpen'].setValue(0)
        self.sliders['dof'].setValue(0)
        self.sliders['vig'].setValue(0)
        self.sliders['dewarp'].setValue(0)

        self.is_resetting = False
        self._save_state()
        self.update_view()

    def show_about(self):
        dlg = AboutDialog(self)
        dlg.exec()

    def nudge_view(self, d_yaw, d_pitch):
        if not self.projector: return
        self.float_yaw += d_yaw
        self.float_pitch += d_pitch
        self.float_yaw = (self.float_yaw + 180.0) % 360.0 - 180.0
        self.float_pitch = max(-90.0, min(90.0, self.float_pitch))

        self.sliders['yaw'].blockSignals(True)
        self.sliders['yaw'].setValue(round(self.float_yaw))
        self.sliders['yaw'].blockSignals(False)

        self.sliders['pitch'].blockSignals(True)
        self.sliders['pitch'].setValue(round(self.float_pitch))
        self.sliders['pitch'].blockSignals(False)

        self.update_view()

    def keyboard_nudge(self, d_yaw, d_pitch):
        self.nudge_view(d_yaw, d_pitch)
        self.history_timer.start(500)

    def create_slider_row(self, layout, label_text, min_val, max_val, default_val):
        row_layout = QHBoxLayout()
        label = QLabel(f"{label_text} {default_val}")
        row_layout.addWidget(label)
        btn_reset = QPushButton("↺")
        btn_reset.setFixedWidth(30)
        btn_reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row_layout.addWidget(btn_reset)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setMinimum(min_val)
        slider.setMaximum(max_val)
        slider.setValue(default_val)
        slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        col_layout = QVBoxLayout()
        col_layout.addLayout(row_layout)
        col_layout.addWidget(slider)
        layout.addLayout(col_layout)

        def on_value_changed(val):
            label.setText(f"{label_text} {val}")
            if not getattr(self, 'is_resetting', False):
                self.update_view()

        slider.valueChanged.connect(on_value_changed)
        slider.sliderReleased.connect(self._save_state)

        def reset_action():
            slider.setValue(default_val)
            if 'yaw' in label_text.lower(): self.float_yaw = float(default_val)
            if 'pitch' in label_text.lower(): self.float_pitch = float(default_val)
            self._save_state()

        btn_reset.clicked.connect(reset_action)
        return slider

    def create_wb_row(self, layout):
        row_layout = QHBoxLayout()
        label = QLabel(f"White Balance: 0")
        row_layout.addWidget(label)

        self.btn_eye = QPushButton("🖌")
        self.btn_eye.setFixedWidth(30)
        self.btn_eye.setToolTip("Click here, then click a white or neutral gray area on the image to auto-adjust White Balance.")
        self.btn_eye.clicked.connect(self.toggle_eyedropper)
        self.btn_eye.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row_layout.addWidget(self.btn_eye)

        btn_reset = QPushButton("↺")
        btn_reset.setFixedWidth(30)
        btn_reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row_layout.addWidget(btn_reset)

        self.sliders['temp'] = QSlider(Qt.Orientation.Horizontal)
        self.sliders['temp'].setMinimum(-50)
        self.sliders['temp'].setMaximum(50)
        self.sliders['temp'].setValue(0)
        self.sliders['temp'].setFocusPolicy(Qt.FocusPolicy.NoFocus)

        def on_temp_changed(val):
            label.setText(f"White Balance: {val}")
            if not getattr(self, 'is_resetting', False):
                self.update_view()

        self.sliders['temp'].valueChanged.connect(on_temp_changed)
        self.sliders['temp'].sliderReleased.connect(self._save_state)

        def reset_action():
            self.sliders['temp'].setValue(0)
            self._save_state()

        btn_reset.clicked.connect(reset_action)

        col_layout = QVBoxLayout()
        col_layout.addLayout(row_layout)
        col_layout.addWidget(self.sliders['temp'])
        layout.addLayout(col_layout)

    def load_image(self):
        file_filter = "Image Files (*.jpg *.jpeg *.JPG *.JPEG *.png *.PNG *.hdr *.exr *.tiff *.tif *.webp *.WEBP)"
        file_name, _ = QFileDialog.getOpenFileName(self, "Open 360 Image", self.last_load_dir, file_filter)
        if file_name:
            self.last_load_dir = os.path.dirname(file_name)
            self.settings.setValue("last_load_dir", self.last_load_dir)

            self.image_label.set_master_pixmap(None, None)
            self.image_label.setText("Loading... Please Wait.")
            self.projector = None
            QApplication.processEvents()

            self.projector = EquirectangularProjector(file_name)

            self.current_exif = None
            if HAS_PIEXIF:
                try:
                    self.current_exif = piexif.load(file_name)
                except Exception:
                    pass

            self.history.clear()
            self.history_index = -1
            self.reset_all_settings()

    def import_settings(self):
        file_filter = "Settings or Images (*.json *.jpg *.jpeg *.png *.tiff *.tif *.webp)"
        file_name, _ = QFileDialog.getOpenFileName(self, "Import Settings", self.last_export_dir, file_filter)
        if not file_name: return

        json_path = os.path.splitext(file_name)[0] + ".json"
        if file_name.endswith('.json'):
            json_path = file_name

        if os.path.exists(json_path):
            try:
                with open(json_path, 'r') as f:
                    state = json.load(f)
                self._restore_state(state)
                self._save_state()
                return
            except Exception as e:
                QMessageBox.warning(self, "Import Failed", f"Could not parse the JSON settings file.\n\n{e}")
                return

        if HAS_PIEXIF and file_name.lower().endswith(('.jpg', '.jpeg')):
            try:
                exif_dict = piexif.load(file_name)
                user_comment = exif_dict["Exif"].get(piexif.ExifIFD.UserComment)
                if user_comment:
                    json_str = piexif.helper.UserComment.load(user_comment)
                    state = json.loads(json_str)
                    self._restore_state(state)
                    self._save_state()
                    return
                else:
                    QMessageBox.warning(self, "No Settings Found", "Could not find an embedded preset or a companion .json file.")
            except Exception as e:
                QMessageBox.warning(self, "Import Failed", f"Could not load settings from EXIF.\n\n{e}")
        else:
            QMessageBox.warning(self, "No Settings Found", "Could not find a companion .json file for this image.")

    def get_aspect_ratio(self):
        ratios = [6.0/4.0, 7.0/5.0, 10.0/8.0, 16.0/9.0, 1.0]
        ratio = ratios[self.aspect_combo.currentIndex()]
        if self.orient_combo.currentText() == "Portrait":
            return 1.0 / ratio
        return ratio

    def get_fov(self):
        return 120 - self.sliders['zoom'].value()

    def handle_image_drag(self, dx, dy):
        if not self.projector: return
        fov = self.get_fov()
        sensitivity = (fov / 120.0) * 0.15 
        self.nudge_view(-dx * sensitivity, dy * sensitivity)

    def handle_scroll(self, delta):
        step = 5 if delta > 0 else -5
        new_zoom = self.sliders['zoom'].value() + step
        self.sliders['zoom'].setValue(max(0, min(110, new_zoom)))
        self.history_timer.start(500)

    def toggle_eyedropper(self):
        self.image_label.eyedropper_mode = True
        self.image_label.setCursor(Qt.CursorShape.CrossCursor)

    def handle_eyedropper(self, r, b):
        diff = r - b
        new_temp = self.sliders['temp'].value() - int(diff * 0.5)
        self.sliders['temp'].setValue(max(-50, min(50, new_temp)))
        self._save_state()

    def apply_image_adjustments(self, img, hq_pass=False):
        if img is None: return None
        if self.is_bypassed: return img

        temp = self.sliders['temp'].value()
        bright = self.sliders['bright'].value()
        cont = self.sliders['contrast'].value()
        shadows_val = self.sliders['shadows'].value()
        highlights_val = self.sliders['highlights'].value()
        sat = self.sliders['sat'].value()
        clarity_val = self.sliders['clarity'].value()

        sharpen_val = self.sliders['sharpen'].value()
        dof = self.sliders['dof'].value()
        vig = self.sliders['vig'].value()

        if img.dtype != np.uint8: 
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

        if temp != 0:
            img_float = img.astype(np.float32)
            b, g, r = cv2.split(img_float)
            r = np.clip(r * (1.0 + (temp / 150.0)), 0, 255)
            b = np.clip(b * (1.0 - (temp / 150.0)), 0, 255)
            img = cv2.merge([b, g, r]).astype(np.uint8)

        if bright != 0 or cont != 0:
            alpha = 1.0 + (cont / 100.0)
            beta = bright
            img = cv2.convertScaleAbs(img, alpha=alpha, beta=beta)

        if clarity_val > 0:
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clip_limit = (clarity_val / 100.0) * 3.0 + 1.0
            clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8,8))
            l = clahe.apply(l)
            lab = cv2.merge((l, a, b))
            img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        if sat != 0 or shadows_val > 0 or highlights_val > 0:
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
            if sat != 0:
                hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + (sat / 100.0)), 0, 255)

            v = hsv[:, :, 2]
            if shadows_val > 0:
                shadows = shadows_val * 0.3
                shadow_mask = np.clip(1.0 - (v / 128.0), 0, 1)
                v = np.clip(v + (shadow_mask * shadows * 1.5), 0, 255)

            if highlights_val > 0:
                highlights = highlights_val * 0.3
                highlight_mask = np.clip((v - 128.0) / 127.0, 0, 1)
                v = np.clip(v + (highlight_mask * highlights * 1.5), 0, 255)

            hsv[:, :, 2] = v
            img = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

        if dof > 0:
            blur_kernel = max(3, (dof * 2) + 1)
            blurred = cv2.GaussianBlur(img, (blur_kernel, blur_kernel), dof)
            h, w = img.shape[:2]
            y, x = np.ogrid[:h, :w]
            mask = np.power(np.clip(np.sqrt((x - w/2)**2 + (y - h/2)**2) / np.hypot(w/2, h/2), 0, 1), 1.5)
            mask = np.dstack([mask]*3)
            img = (img * (1 - mask) + blurred * mask).astype(np.uint8)

        if sharpen_val > 0:
            blur = cv2.GaussianBlur(img, (0, 0), 3)
            amount = sharpen_val / 100.0 * 2.0
            img = cv2.addWeighted(img, 1.0 + amount, blur, -amount, 0)

        if vig > 0:
            h, w = img.shape[:2]
            kernel = cv2.getGaussianKernel(h, h/2) * cv2.getGaussianKernel(w, w/2).T
            mask = 255 * kernel / np.linalg.norm(kernel)
            mask = mask / mask.max()
            intensity = vig / 100.0
            img = (img.astype(np.float32) * (1.0 - intensity * (1.0 - np.dstack([mask]*3)))).astype(np.uint8)

        if hq_pass:
            noise_val = self.sliders['noise'].value()
            if noise_val > 0:
                sigma = (noise_val / 100.0) * 50.0
                img = cv2.bilateralFilter(img, 9, sigma, sigma)

            beautify_val = self.sliders['beautify'].value()
            if beautify_val > 0:
                h_val = float(beautify_val) / 100.0 * 11.0
                img = cv2.fastNlMeansDenoisingColored(img, None, h_val, h_val, 7, 21)

        return img

    def refresh_display(self, hq_pass=False, overlay_text="", fade=False):
        if self.base_proj_img is None: return

        if overlay_text:
            QApplication.processEvents()

        processed = self.apply_image_adjustments(self.base_proj_img, hq_pass=hq_pass)
        self.render_to_ui(processed, overlay_text=overlay_text, fade=fade)

    def _reapply_grid(self):
        self.image_label.show_grid = self.chk_grid.isChecked()
        self.settings.setValue("show_grid", self.chk_grid.isChecked())
        self.image_label.update()

    def render_to_ui(self, img, overlay_text="", fade=False):
        if img is None: return

        self.image_label.show_grid = self.chk_grid.isChecked()
        self.image_label.overlay_text = overlay_text

        preview_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = preview_rgb.shape[:2]
        q_img = QImage(preview_rgb.data, w, h, preview_rgb.shape[2] * w, QImage.Format.Format_RGB888)

        self.image_label.set_master_pixmap(QPixmap.fromImage(q_img), img, fade=fade)

    def update_view(self):
        if getattr(self, 'is_resetting', False): return
        if not self.projector: return

        if self.is_bypassed:
            self.settings_changed_while_bypassed = True

        self.medium_timer.stop()
        self.high_timer.stop()
        self.ultra_timer.stop()

        self.fast_timer.start(15)

    def _render_fast(self):
        if not self.projector: return

        yaw, pitch, roll = self.sliders['yaw'].value(), self.sliders['pitch'].value(), self.sliders['roll'].value()
        dewarp = self.sliders['dewarp'].value()

        if round(self.float_yaw) != yaw: self.float_yaw = float(yaw)
        if round(self.float_pitch) != pitch: self.float_pitch = float(pitch)

        self.base_proj_img = self.projector.get_projection(self.float_yaw, self.float_pitch, roll, self.get_fov(), self.get_aspect_ratio(), dewarp=dewarp, preview_width=400)
        self.refresh_display(hq_pass=False, overlay_text="BYPASS" if self.is_bypassed else "", fade=False)

        self.medium_timer.start(150)

    def _render_medium(self):
        if not self.projector: return
        roll = self.sliders['roll'].value()
        dewarp = self.sliders['dewarp'].value()

        self.base_proj_img = self.projector.get_projection(self.float_yaw, self.float_pitch, roll, self.get_fov(), self.get_aspect_ratio(), dewarp=dewarp, preview_width=800)
        self.refresh_display(hq_pass=True, overlay_text="BYPASS" if self.is_bypassed else "RENDERING", fade=True)

        self.high_timer.start(250)

    def _render_high(self):
        if not self.projector: return
        roll = self.sliders['roll'].value()
        dewarp = self.sliders['dewarp'].value()

        self.base_proj_img = self.projector.get_projection(self.float_yaw, self.float_pitch, roll, self.get_fov(), self.get_aspect_ratio(), dewarp=dewarp, preview_width=1200)
        self.refresh_display(hq_pass=True, overlay_text="BYPASS" if self.is_bypassed else "RENDERING", fade=True)

        self.ultra_timer.start(400)

    def _render_ultra(self):
        if not self.projector: return
        roll = self.sliders['roll'].value()
        dewarp = self.sliders['dewarp'].value()

        self.base_proj_img = self.projector.get_projection(self.float_yaw, self.float_pitch, roll, self.get_fov(), self.get_aspect_ratio(), dewarp=dewarp, preview_width=1600)
        self.refresh_display(hq_pass=True, overlay_text="BYPASS" if self.is_bypassed else "PREVIEW", fade=True)

    def save_image(self):
        if not self.projector: return

        base_name = os.path.splitext(os.path.basename(self.projector.image_path))[0]
        aspect_str = self.aspect_combo.currentText().replace(":", "x")
        yaw, pitch, roll = self.sliders['yaw'].value(), self.sliders['pitch'].value(), self.sliders['roll'].value()
        dewarp = self.sliders['dewarp'].value()

        default_save_name = f"{base_name}_Y{yaw}_P{pitch}_R{roll}_{aspect_str}"
        default_path = os.path.join(self.last_export_dir, default_save_name) if self.last_export_dir else default_save_name
        file_filter = "JPEG High Quality (*.jpg *.jpeg);;TIFF Lossless (*.tiff *.tif);;PNG Lossless (*.png);;WebP Compressed (*.webp)"

        options = QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.DontConfirmOverwrite
        save_path, selected_filter = QFileDialog.getSaveFileName(self, "Export", default_path, file_filter, options=options)

        if save_path:
            if "JPEG" in selected_filter and not save_path.lower().endswith(('.jpg', '.jpeg')):
                save_path += ".jpg"
            elif "TIFF" in selected_filter and not save_path.lower().endswith(('.tiff', '.tif')):
                save_path += ".tiff"
            elif "PNG" in selected_filter and not save_path.lower().endswith('.png'):
                save_path += ".png"
            elif "WebP" in selected_filter and not save_path.lower().endswith('.webp'):
                save_path += ".webp"

            if os.path.exists(save_path):
                reply = QMessageBox.question(self, "Overwrite File?",
                                             f"The file {os.path.basename(save_path)} already exists.\nAre you sure you want to overwrite it?",
                                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply == QMessageBox.StandardButton.No:
                    return

            self.last_export_dir = os.path.dirname(save_path)
            self.settings.setValue("last_export_dir", self.last_export_dir)

            self.btn_save.setText("Exporting... Please Wait")
            self.btn_save.setEnabled(False)
            QApplication.processEvents()

            master_img = self.projector.get_projection(yaw, pitch, roll, self.get_fov(), self.get_aspect_ratio(), dewarp=dewarp, preview_width=None)

            was_bypassed = self.is_bypassed
            self.is_bypassed = False
            master_img = self.apply_image_adjustments(master_img, hq_pass=True)
            self.is_bypassed = was_bypassed

            ext = save_path.lower()
            if ext.endswith('.jpg') or ext.endswith('.jpeg'):
                cv2.imwrite(save_path, master_img, [int(cv2.IMWRITE_JPEG_QUALITY), 98])
            elif ext.endswith('.webp'):
                cv2.imwrite(save_path, master_img, [int(cv2.IMWRITE_WEBP_QUALITY), 95])
            else:
                cv2.imwrite(save_path, master_img)

            settings_dict = self._get_current_state()
            settings_json = json.dumps(settings_dict, indent=4)
            json_path = os.path.splitext(save_path)[0] + ".json"
            try:
                with open(json_path, 'w') as f:
                    f.write(settings_json)
            except Exception as e:
                print(f"Failed to write sidecar JSON file: {e}")

            if HAS_PIEXIF and ext in ['.jpg', '.jpeg']:
                try:
                    clean_exif = {"0th":{}, "Exif":{}, "GPS":{}, "1st":{}, "thumbnail":None}

                    if self.current_exif:
                        safe_0th = [piexif.ImageIFD.Make, piexif.ImageIFD.Model, piexif.ImageIFD.DateTime, piexif.ImageIFD.Copyright, piexif.ImageIFD.Artist]
                        for tag in safe_0th:
                            if tag in self.current_exif.get("0th", {}):
                                clean_exif["0th"][tag] = self.current_exif["0th"][tag]

                        safe_exif = [piexif.ExifIFD.ExposureTime, piexif.ExifIFD.FNumber, piexif.ExifIFD.ISOSpeedRatings, piexif.ExifIFD.DateTimeOriginal, 
                                     piexif.ExifIFD.DateTimeDigitized, piexif.ExifIFD.FocalLength, piexif.ExifIFD.LensMake, piexif.ExifIFD.LensModel, 
                                     piexif.ExifIFD.ApertureValue, piexif.ExifIFD.ShutterSpeedValue, piexif.ExifIFD.ExposureBiasValue, 
                                     piexif.ExifIFD.MaxApertureValue, piexif.ExifIFD.MeteringMode, piexif.ExifIFD.Flash]
                        for tag in safe_exif:
                            if tag in self.current_exif.get("Exif", {}):
                                clean_exif["Exif"][tag] = self.current_exif["Exif"][tag]

                        if "GPS" in self.current_exif:
                            clean_exif["GPS"] = copy.deepcopy(self.current_exif["GPS"])

                    clean_exif["0th"][piexif.ImageIFD.Software] = f"Baldnerd's 360 Portrait Studio v{VERSION}".encode('utf-8')
                    clean_exif["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(settings_json, encoding="unicode")

                    exif_bytes = piexif.dump(clean_exif)
                    piexif.insert(exif_bytes, save_path)

                except Exception as e:
                    print(f"EXIF metadata injection failed: {e}")

            self.btn_save.setText("Export")
            self.btn_save.setEnabled(True)
            print(f"Exported to: {save_path}")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
