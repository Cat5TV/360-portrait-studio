# Baldnerd's 360 Portrait Studio
# By Robbie Ferguson
# Version 0.8

import sys
import cv2
import numpy as np
import os
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QSlider, QFileDialog, QComboBox,
                             QSizePolicy, QCheckBox, QMessageBox, QToolButton, QDialog)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QUrl
from PyQt6.QtGui import QImage, QPixmap, QShortcut, QKeySequence, QDesktopServices

class EquirectangularProjector:
    def __init__(self, image_path):
        self.image_path = image_path
        self.img = cv2.imread(image_path, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
        self.height, self.width = self.img.shape[:2]

    def get_projection(self, yaw, pitch, roll, fov, aspect_ratio, preview_width=None):
        pixels_per_degree = self.width / 360.0
        out_w = preview_width if preview_width else int(fov * pixels_per_degree)
        out_h = int(out_w / aspect_ratio)

        yaw, pitch, roll = np.radians(yaw), np.radians(pitch), np.radians(roll)
        fov_rad = np.radians(fov)

        x = np.linspace(-1, 1, out_w)
        y = np.linspace(-1 / aspect_ratio, 1 / aspect_ratio, out_h)
        x_mesh, y_mesh = np.meshgrid(x, y)

        z = np.ones_like(x_mesh) / np.tan(fov_rad / 2)
        rays = np.stack((x_mesh, y_mesh, z), axis=-1)

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

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #111; color: #777; font-size: 16px;")
        self.setText("Load a 360 Image to begin.")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 400)
        self.last_pos = None
        self.current_pixmap = None
        self.eyedropper_mode = False
        self.raw_preview_img = None 

    def set_master_pixmap(self, pixmap, raw_img):
        self.current_pixmap = pixmap
        self.raw_preview_img = raw_img
        self.update_display()

    def update_display(self):
        if self.current_pixmap:
            scaled_pixmap = self.current_pixmap.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            super().setPixmap(scaled_pixmap)

    def resizeEvent(self, event):
        self.update_display()
        super().resizeEvent(event)

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

    def wheelEvent(self, event):
        self.scrolled.emit(event.angleDelta().y())

class CollapsibleBox(QWidget):
    def __init__(self, title="", parent=None, expanded=True):
        super().__init__(parent)
        self.toggle_button = QToolButton(text=title, checkable=True, checked=expanded)
        self.toggle_button.setStyleSheet("QToolButton { border: none; font-weight: bold; text-align: left; background-color: #e0e0e0; padding: 5px; }")
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.toggle_button.toggled.connect(self.on_pressed)
        self.toggle_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(5, 5, 5, 5)
        self.content_area.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content_area)

    def on_pressed(self, checked):
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.content_area.setVisible(checked)

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Baldnerd's 360 Studio")
        self.setMinimumWidth(450) # Allows dynamic height calculation
        layout = QVBoxLayout(self)
        
        text = """
        <h3>Baldnerd's 360 Studio</h3>
        <p>Created by Robbie Ferguson // The Bald Nerd</p>
        <p>Robbie is the founder of Category5 TV Network, host of several technology broadcasts, 
        and the creator of NEMS Linux.</p>
        <p>Visit <a href="https://baldnerd.com/">https://baldnerd.com/</a> to learn more.</p>
        """
        lbl = QLabel(text)
        lbl.setOpenExternalLinks(True)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(lbl)
        
        btn_layout = QHBoxLayout()
        
        btn_donate = QPushButton("Donate")
        btn_donate.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://donate.category5.tv/")))
        
        btn_patron = QPushButton("Become a Patron")
        btn_patron.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://patreon.com/Category5")))
        
        btn_layout.addWidget(btn_donate)
        btn_layout.addWidget(btn_patron)
        
        layout.addLayout(btn_layout)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Baldnerd's 360 Studio")
        self.resize(1300, 900)
        self.projector = None
        
        self.float_yaw = 0.0
        self.float_pitch = 0.0
        self.base_proj_img = None 
        self.is_previewing = False
        
        self.render_timer = QTimer()
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._do_update_view)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        self.image_label = InteractiveImageLabel()
        self.image_label.dragged.connect(self.handle_image_drag)
        self.image_label.scrolled.connect(self.handle_scroll)
        self.image_label.clicked_color.connect(self.handle_eyedropper)
        layout.addWidget(self.image_label, stretch=4)

        control_panel = QVBoxLayout()
        control_panel.setContentsMargins(10, 10, 10, 10)
        layout.addLayout(control_panel, stretch=1)

        # Top Buttons
        top_btn_layout = QHBoxLayout()
        self.btn_load = QPushButton("Load 360 Image")
        self.btn_load.clicked.connect(self.load_image)
        self.btn_load.setMinimumHeight(35)
        
        self.btn_about = QPushButton("About")
        self.btn_about.clicked.connect(self.show_about)
        self.btn_about.setMinimumHeight(35)
        self.btn_about.setFixedWidth(70)
        
        top_btn_layout.addWidget(self.btn_load)
        top_btn_layout.addWidget(self.btn_about)
        control_panel.addLayout(top_btn_layout)

        # Aspect, Orientation, and Grid Row
        row_aspect = QHBoxLayout()
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["4:6", "5:7", "8:10", "16:9", "1:1"])
        self.aspect_combo.currentIndexChanged.connect(self.update_view)
        
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Landscape", "Portrait"])
        self.orient_combo.currentIndexChanged.connect(self.update_view)
        
        self.chk_grid = QCheckBox("Show Grid")
        self.chk_grid.stateChanged.connect(self.refresh_display)

        row_aspect.addWidget(QLabel("Ratio:"))
        row_aspect.addWidget(self.aspect_combo)
        row_aspect.addWidget(self.orient_combo)
        row_aspect.addSpacing(10)
        row_aspect.addWidget(self.chk_grid)
        control_panel.addLayout(row_aspect)

        self.sliders = {}

        # --- Positioning ---
        pos_box = CollapsibleBox("Positioning (Pan/Tilt/Zoom)", expanded=False)
        self.sliders['yaw'] = self.create_slider_row(pos_box.content_layout, "Pan (Yaw):", -180, 180, 0)
        self.sliders['pitch'] = self.create_slider_row(pos_box.content_layout, "Tilt (Pitch):", -90, 90, 0)
        self.sliders['roll'] = self.create_slider_row(pos_box.content_layout, "Horizon (Roll):", -180, 180, 0)
        self.sliders['zoom'] = self.create_slider_row(pos_box.content_layout, "Zoom:", 0, 110, 30)
        control_panel.addWidget(pos_box)

        # --- Color ---
        color_box = CollapsibleBox("Color Adjustments", expanded=True)
        self.create_wb_row(color_box.content_layout)
        self.sliders['sat'] = self.create_slider_row(color_box.content_layout, "Saturation:", -50, 100, 0)
        control_panel.addWidget(color_box)

        # --- Effects ---
        fx_box = CollapsibleBox("Effects", expanded=True)
        self.sliders['dof'] = self.create_slider_row(fx_box.content_layout, "Depth of Field (Blur):", 0, 50, 0)
        self.sliders['vig'] = self.create_slider_row(fx_box.content_layout, "Vignette Amount:", 0, 100, 0)
        
        # Beautify Tool
        beautify_layout = QHBoxLayout()
        self.chk_beautify = QCheckBox("Apply Beautify on Export")
        self.btn_preview = QPushButton("Preview")
        self.btn_preview.clicked.connect(self.toggle_preview)
        beautify_layout.addWidget(self.chk_beautify)
        beautify_layout.addWidget(self.btn_preview)
        fx_box.content_layout.addLayout(beautify_layout)
        control_panel.addWidget(fx_box)

        # Export Button
        self.btn_save = QPushButton("Export")
        self.btn_save.clicked.connect(self.save_image)
        self.btn_save.setStyleSheet("background-color: #0078D7; color: white; padding: 12px; font-weight: bold;")
        control_panel.addSpacing(20)
        control_panel.addWidget(self.btn_save)
        control_panel.addStretch()

        # Keyboard nudging
        QShortcut(QKeySequence(Qt.Key.Key_Left), self).activated.connect(lambda: self.nudge_view(-0.02, 0))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self).activated.connect(lambda: self.nudge_view(0.02, 0))
        QShortcut(QKeySequence(Qt.Key.Key_Up), self).activated.connect(lambda: self.nudge_view(0, 0.02))
        QShortcut(QKeySequence(Qt.Key.Key_Down), self).activated.connect(lambda: self.nudge_view(0, -0.02))

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

    def create_slider_row(self, layout, label_text, min_val, max_val, default_val):
        row_layout = QHBoxLayout()
        row_layout.addWidget(QLabel(label_text))
        btn_reset = QPushButton("↺")
        btn_reset.setFixedWidth(30)
        row_layout.addWidget(btn_reset)
        
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setMinimum(min_val)
        slider.setMaximum(max_val)
        slider.setValue(default_val)
        
        col_layout = QVBoxLayout()
        col_layout.addLayout(row_layout)
        col_layout.addWidget(slider)
        layout.addLayout(col_layout)

        slider.valueChanged.connect(self.update_view)
        
        def reset_action():
            slider.setValue(default_val)
            if 'yaw' in label_text.lower(): self.float_yaw = float(default_val)
            if 'pitch' in label_text.lower(): self.float_pitch = float(default_val)
            
        btn_reset.clicked.connect(reset_action)
        return slider

    def create_wb_row(self, layout):
        row_layout = QHBoxLayout()
        row_layout.addWidget(QLabel("White Balance:"))
        
        self.btn_eye = QPushButton("🖌")
        self.btn_eye.setFixedWidth(30)
        self.btn_eye.setToolTip("Click here, then click a white or neutral gray area on the image to auto-adjust White Balance.")
        self.btn_eye.clicked.connect(self.toggle_eyedropper)
        row_layout.addWidget(self.btn_eye)
        
        btn_reset = QPushButton("↺")
        btn_reset.setFixedWidth(30)
        row_layout.addWidget(btn_reset)
        
        self.sliders['temp'] = QSlider(Qt.Orientation.Horizontal)
        self.sliders['temp'].setMinimum(-50)
        self.sliders['temp'].setMaximum(50)
        self.sliders['temp'].setValue(0)
        self.sliders['temp'].valueChanged.connect(self.update_view)
        btn_reset.clicked.connect(lambda: self.sliders['temp'].setValue(0))
        
        col_layout = QVBoxLayout()
        col_layout.addLayout(row_layout)
        col_layout.addWidget(self.sliders['temp'])
        layout.addLayout(col_layout)

    def load_image(self):
        file_filter = "Image Files (*.jpg *.jpeg *.JPG *.JPEG *.png *.PNG *.hdr *.exr *.tiff *.tif *.webp *.WEBP)"
        file_name, _ = QFileDialog.getOpenFileName(self, "Open 360 Image", "", file_filter)
        if file_name:
            self.projector = EquirectangularProjector(file_name)
            self.float_yaw = 0.0
            self.float_pitch = 0.0
            self.update_view()

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

    def toggle_eyedropper(self):
        self.image_label.eyedropper_mode = True
        self.image_label.setCursor(Qt.CursorShape.CrossCursor)

    def handle_eyedropper(self, r, b):
        diff = r - b
        new_temp = self.sliders['temp'].value() - int(diff * 0.5)
        self.sliders['temp'].setValue(max(-50, min(50, new_temp)))

    def apply_image_adjustments(self, img, apply_beautify=False):
        if img is None: return None
        
        temp, sat, dof, vig = self.sliders['temp'].value(), self.sliders['sat'].value(), self.sliders['dof'].value(), self.sliders['vig'].value()

        if img.dtype != np.uint8: 
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

        if temp != 0:
            img_float = img.astype(np.float32)
            b, g, r = cv2.split(img_float)
            r = np.clip(r * (1.0 + (temp / 150.0)), 0, 255)
            b = np.clip(b * (1.0 - (temp / 150.0)), 0, 255)
            img = cv2.merge([b, g, r]).astype(np.uint8)

        if sat != 0:
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + (sat / 100.0)), 0, 255)
            img = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

        if dof > 0:
            blur_kernel = max(3, (dof * 2) + 1)
            blurred = cv2.GaussianBlur(img, (blur_kernel, blur_kernel), dof)
            h, w = img.shape[:2]
            y, x = np.ogrid[:h, :w]
            mask = np.power(np.clip(np.sqrt((x - w/2)**2 + (y - h/2)**2) / np.hypot(w/2, h/2), 0, 1), 1.5)
            mask = np.dstack([mask]*3)
            img = (img * (1 - mask) + blurred * mask).astype(np.uint8)

        if vig > 0:
            h, w = img.shape[:2]
            kernel = cv2.getGaussianKernel(h, h/2) * cv2.getGaussianKernel(w, w/2).T
            mask = 255 * kernel / np.linalg.norm(kernel)
            mask = mask / mask.max()
            intensity = vig / 100.0
            img = (img.astype(np.float32) * (1.0 - intensity * (1.0 - np.dstack([mask]*3)))).astype(np.uint8)

        if apply_beautify:
            img = cv2.fastNlMeansDenoisingColored(img, None, 4, 4, 7, 21)

        return img

    def refresh_display(self):
        if self.base_proj_img is None: return
        processed = self.apply_image_adjustments(self.base_proj_img, apply_beautify=self.is_previewing and self.chk_beautify.isChecked())
        self.render_to_ui(processed, is_preview=self.is_previewing)

    def toggle_preview(self):
        if self.is_previewing:
            self.is_previewing = False
            self.btn_preview.setText("Preview")
            self.update_view()
        else:
            if self.base_proj_img is not None:
                self.is_previewing = True
                self.btn_preview.setText("Processing...")
                QApplication.processEvents()
                self.refresh_display()
                self.btn_preview.setText("Restore")

    def render_to_ui(self, img, is_preview=False):
        if img is None: return
        
        disp_img = img.copy() 
        h, w = disp_img.shape[:2]
        
        if self.chk_grid.isChecked():
            overlay = disp_img.copy()
            color = (255, 255, 255)
            thickness = max(1, h // 400)
            
            cv2.line(overlay, (w // 3, 0), (w // 3, h), color, thickness, cv2.LINE_AA)
            cv2.line(overlay, (2 * w // 3, 0), (2 * w // 3, h), color, thickness, cv2.LINE_AA)
            cv2.line(overlay, (0, h // 3), (w, h // 3), color, thickness, cv2.LINE_AA)
            cv2.line(overlay, (0, 2 * h // 3), (w, 2 * h // 3), color, thickness, cv2.LINE_AA)
            
            cx, cy = w // 2, h // 2
            cross_len = min(w, h) // 20
            cv2.line(overlay, (cx - cross_len, cy), (cx + cross_len, cy), color, thickness + 1, cv2.LINE_AA)
            cv2.line(overlay, (cx, cy - cross_len), (cx, cy + cross_len), color, thickness + 1, cv2.LINE_AA)
            
            cv2.addWeighted(overlay, 0.3, disp_img, 0.7, 0, disp_img)

        if is_preview:
            cv2.putText(disp_img, "PREVIEW", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 215, 255), 2, cv2.LINE_AA)

        preview_rgb = cv2.cvtColor(disp_img, cv2.COLOR_BGR2RGB)
        q_img = QImage(preview_rgb.data, w, h, preview_rgb.shape[2] * w, QImage.Format.Format_RGB888)
        self.image_label.set_master_pixmap(QPixmap.fromImage(q_img), img)

    def update_view(self):
        if not self.projector: return
        
        if self.is_previewing:
            self.is_previewing = False
            self.btn_preview.setText("Preview")
        
        self.render_timer.start(15) 

    def _do_update_view(self):
        if not self.projector: return
        
        yaw = self.sliders['yaw'].value()
        pitch = self.sliders['pitch'].value()
        roll = self.sliders['roll'].value()
        
        # FIX: Only overwrite the high-precision float if the user manually dragged the slider
        if round(self.float_yaw) != yaw:
            self.float_yaw = float(yaw)
        if round(self.float_pitch) != pitch:
            self.float_pitch = float(pitch)
        
        self.base_proj_img = self.projector.get_projection(self.float_yaw, self.float_pitch, roll, self.get_fov(), self.get_aspect_ratio(), preview_width=800)
        self.refresh_display()

    def save_image(self):
        if not self.projector: return
        
        base_name = os.path.splitext(os.path.basename(self.projector.image_path))[0]
        aspect_str = self.aspect_combo.currentText().replace(":", "x")
        yaw, pitch, roll = self.sliders['yaw'].value(), self.sliders['pitch'].value(), self.sliders['roll'].value()
        
        default_save_name = f"{base_name}_Y{yaw}_P{pitch}_R{roll}_{aspect_str}"
        file_filter = "JPEG High Quality (*.jpg *.jpeg);;TIFF Lossless (*.tiff *.tif);;PNG Lossless (*.png);;WebP Compressed (*.webp)"
        
        save_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Export", default_save_name, file_filter, 
            options=QFileDialog.Option.DontUseNativeDialog
        )
        
        if save_path:
            if "JPEG" in selected_filter and not save_path.lower().endswith(('.jpg', '.jpeg')):
                save_path += ".jpg"
            elif "TIFF" in selected_filter and not save_path.lower().endswith(('.tiff', '.tif')):
                save_path += ".tiff"
            elif "PNG" in selected_filter and not save_path.lower().endswith('.png'):
                save_path += ".png"
            elif "WebP" in selected_filter and not save_path.lower().endswith('.webp'):
                save_path += ".webp"

            master_img = self.projector.get_projection(yaw, pitch, roll, self.get_fov(), self.get_aspect_ratio(), preview_width=None)
            master_img = self.apply_image_adjustments(master_img, apply_beautify=self.chk_beautify.isChecked())
            
            ext = save_path.lower()
            if ext.endswith('.jpg') or ext.endswith('.jpeg'):
                cv2.imwrite(save_path, master_img, [int(cv2.IMWRITE_JPEG_QUALITY), 98])
            elif ext.endswith('.webp'):
                cv2.imwrite(save_path, master_img, [int(cv2.IMWRITE_WEBP_QUALITY), 95])
            else:
                cv2.imwrite(save_path, master_img)
            
            print(f"Exported to: {save_path}")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
