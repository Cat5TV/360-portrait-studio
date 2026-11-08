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
from datetime import datetime
import sqlite3
import hashlib
from PyQt6.QtCore import QStandardPaths, QThread, pyqtSignal, QSize, Qt
from PyQt6.QtWidgets import (QListWidget, QListWidgetItem, QLineEdit, QListView)
from PyQt6.QtGui import QBrush
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QSlider, QFileDialog, QComboBox,
                             QSizePolicy, QCheckBox, QMessageBox, QToolButton, QDialog, QScrollArea, QProgressBar)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QUrl, QVariantAnimation, QSettings, QPointF
from PyQt6.QtGui import QImage, QPixmap, QShortcut, QKeySequence, QDesktopServices, QPainter, QColor, QPen, QIcon, QPolygonF, QBrush

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
from PyQt6.QtGui import QImage, QPixmap, QShortcut, QKeySequence, QDesktopServices, QPainter, QColor, QPen, QIcon

VERSION = "1.2"

# ==============================================================================
# Cross-Platform SQLite Cache & History Manager
# ==============================================================================
CACHE_DIR = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
APP_CACHE_DIR = os.path.join(CACHE_DIR, "Baldnerds360PortraitStudio")
os.makedirs(APP_CACHE_DIR, exist_ok=True)
DB_PATH = os.path.join(APP_CACHE_DIR, "image_cache.db")

class DatabaseManager:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        self.conn.execute('''CREATE TABLE IF NOT EXISTS image_cache (
                             file_path TEXT PRIMARY KEY,
                             last_modified REAL,
                             is_360 INTEGER,
                             is_opened INTEGER,
                             thumb_path TEXT)''')
        self.conn.commit()

    def get_file_info(self, file_path, current_mtime):
        cursor = self.conn.execute("SELECT last_modified, is_360, is_opened, thumb_path FROM image_cache WHERE file_path=?", (file_path,))
        row = cursor.fetchone()
        if row and row[0] == current_mtime:
            return {"is_360": bool(row[1]), "is_opened": bool(row[2]), "thumb_path": row[3]}
        return None

    def update_file_info(self, file_path, last_modified, is_360, is_opened, thumb_path):
        self.conn.execute('''INSERT OR REPLACE INTO image_cache
                             (file_path, last_modified, is_360, is_opened, thumb_path)
                             VALUES (?, ?, ?, ?, ?)''',
                          (file_path, last_modified, int(is_360), int(is_opened), thumb_path))
        self.conn.commit()

    def mark_opened(self, file_path):
        self.conn.execute("UPDATE image_cache SET is_opened = 1 WHERE file_path=?", (file_path,))
        self.conn.commit()

    def prune_missing_files(self):
        # Maintenance task to clear orphaned file references from cache
        cursor = self.conn.execute("SELECT file_path, thumb_path FROM image_cache")
        for row in cursor.fetchall():
            if not os.path.exists(row[0]):
                self.conn.execute("DELETE FROM image_cache WHERE file_path=?", (row[0],))
                if os.path.exists(row[1]):
                    try: os.remove(row[1])
                    except: pass
        self.conn.commit()

DB_MANAGER = DatabaseManager()


# ==============================================================================
# Camera Identification & Affiliate Dictionary
# translates messy raw EXIF identifiers into readable consumer branding
# and appends optional referral links for purchase.
# ==============================================================================
RECOGNIZED_CAMERAS = {
    # DJI 360 & Drone Series
    ("osmo", "oq002"): { # Tested
        "maker": "DJI",
        "model": "Osmo 360 II",
        "url": "https://link.amazon/B0bku4lo0"
    },
    ("dji", "osmo 360"): {
        "maker": "DJI",
        "model": "Osmo 360",
        "url": "https://link.amazon/B02ZCXbKD"
    },
    ("dji", "fca188"): { # Tested
        "maker": "DJI",
        "model": "Avata 360 Drone",
        "url": "https://link.amazon/B03FJWVvK"
    },
    # Insta360 Series
    ("insta360", "x4"): {
        "maker": "Insta360",
        "model": "X4 8K 360",
        "url": "https://link.amazon/B0iBwAPuV"
    },
    ("insta360", "x3"): {
        "maker": "Insta360",
        "model": "X3 360",
        "url": "https://link.amazon/B0bAzirmP"
    },
    ("insta360", "one x2"): {
        "maker": "Insta360",
        "model": "ONE X2 360",
        "url": "https://link.amazon/B0cG3Xpqq"
    },
    ("insta360", "one rs 1-inch 360"): {
        "maker": "Insta360",
        "model": "ONE RS 1-Inch 360 Edition",
        "url": "https://link.amazon/B0dUG13Bz"
    },
    # GoPro 360 Series
    ("gopro", "max"): {
        "maker": "GoPro",
        "model": "MAX 360",
        "url": "https://link.amazon/B0aGMMq4s"
    },
    ("gopro", "fusion"): {
        "maker": "GoPro",
        "model": "Fusion 360",
        "url": "https://link.amazon/B05hsilgt"
    }
}


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

class ThumbnailWorker(QThread):
    thumb_ready = pyqtSignal(str, str, bool, bool)
    folder_ready = pyqtSignal(str)
    finished_dir = pyqtSignal()
    progress = pyqtSignal(int, int)

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.is_running = True

    def stop(self): self.is_running = False

    def run(self):
        valid_exts = ('.jpg', '.jpeg', '.png', '.hdr', '.exr', '.tiff', '.tif', '.webp')
        try:
            raw_items = os.listdir(self.directory)

            # Sort alphabetically, folders first
            folders = sorted([f for f in raw_items if os.path.isdir(os.path.join(self.directory, f))], key=str.lower)
            files = sorted([f for f in raw_items if not os.path.isdir(os.path.join(self.directory, f))], key=str.lower)
            items = folders + files

        except Exception:
            self.finished_dir.emit()
            return

        total_items = len(items)

        # Loop with an index for the progress bar
        for idx, f in enumerate(items, start=1):
            if not self.is_running: break

            # Emit current progress
            self.progress.emit(idx, total_items)

            full_path = os.path.join(self.directory, f)

            if os.path.isdir(full_path):
                self.folder_ready.emit(full_path)
                continue

            if not f.lower().endswith(valid_exts): continue

            try: mtime = os.path.getmtime(full_path)
            except: continue

            cached = DB_MANAGER.get_file_info(full_path, mtime)
            if cached and os.path.exists(cached["thumb_path"]):
                self.thumb_ready.emit(full_path, cached["thumb_path"], cached["is_360"], cached["is_opened"])
                continue

            try:
                img = cv2.imread(full_path, cv2.IMREAD_UNCHANGED)
                if img is None: continue

                h, w = img.shape[:2]
                is_360 = (w == h * 2)
                thumb_size = 256

                if is_360:
                    # Generate 3-frame spritesheet (Left, Center, Right)
                    left = cv2.resize(img[:, :h], (thumb_size, thumb_size), interpolation=cv2.INTER_AREA)
                    center = cv2.resize(img[:, w//2 - h//2 : w//2 + h//2], (thumb_size, thumb_size), interpolation=cv2.INTER_AREA)
                    right = cv2.resize(img[:, w-h : w], (thumb_size, thumb_size), interpolation=cv2.INTER_AREA)
                    final_thumb = np.hstack((left, center, right))
                else:
                    start_x = (w - h) // 2 if w > h else 0
                    cropped = img[:, start_x:start_x+h] if w > h else img
                    final_thumb = cv2.resize(cropped, (thumb_size, thumb_size), interpolation=cv2.INTER_AREA)

                path_hash = hashlib.md5(full_path.encode('utf-8')).hexdigest()
                thumb_path = os.path.join(APP_CACHE_DIR, f"{path_hash}.jpg")
                cv2.imwrite(thumb_path, final_thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 80])

                DB_MANAGER.update_file_info(full_path, mtime, is_360, False, thumb_path)
                self.thumb_ready.emit(full_path, thumb_path, is_360, False)
            except Exception: pass

        self.finished_dir.emit()

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
        self.show_scopes = False
        self.scope_pixmap = None

        self.fade_anim = QVariantAnimation(self)
        self.fade_anim.setDuration(250)
        self.fade_anim.valueChanged.connect(self._update_fade)
        self.fade_anim.finished.connect(self._fade_finished)
        self.fade_alpha = 1.0

    def set_scopes_visible(self, visible):
        self.show_scopes = visible
        self._update_scope()
        self.update()

    def _update_scope(self):
        if not self.show_scopes or self.raw_preview_img is None:
            self.scope_pixmap = None
            return

        img = self.raw_preview_img
        scope_w, scope_h = 256, 150

        # Calculate fast RGB Histograms
        hists = []
        for i in [0, 1, 2]: # B, G, R
            hist = cv2.calcHist([img], [i], None, [256], [0, 256])
            cv2.normalize(hist, hist, 0, scope_h - 20, cv2.NORM_MINMAX)
            hists.append(hist)

        pm = QPixmap(scope_w, scope_h)
        pm.fill(QColor(15, 15, 15, 230))

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw Graticules
        painter.setPen(QPen(QColor(100, 100, 100, 100), 1, Qt.PenStyle.DashLine))
        for i in range(5):
            y = int((i / 4.0) * (scope_h - 20)) + 10
            painter.drawLine(0, y, scope_w, y)

        colors = [QColor(50, 150, 255, 180), QColor(50, 255, 50, 180), QColor(255, 50, 50, 180)]
        for i in range(3):
            poly = QPolygonF()
            poly.append(QPointF(0, scope_h))
            for x in range(256):
                # Fix: Safely extract scalar using .item(x)
                y = (scope_h - 10) - float(hists[i].item(x))
                poly.append(QPointF(x, y))
            poly.append(QPointF(255, scope_h))

            painter.setPen(QPen(colors[i], 1.5))
            painter.setBrush(QBrush(QColor(colors[i].red(), colors[i].green(), colors[i].blue(), 40)))
            painter.drawPolygon(poly)

        # Draw Scope Labels
        painter.setPen(QColor(150, 150, 150))
        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.drawText(5, 10, "255")
        painter.drawText(5, scope_h - 2, "0")

        painter.end()
        self.scope_pixmap = pm

    def set_master_pixmap(self, pixmap, raw_img, fade=False):
        self.raw_preview_img = raw_img
        self._update_scope()

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
        self.bypass_raw_cache = self.raw_preview_img
        self.bypass_scope_cache = self.scope_pixmap

    def restore_bypass_cache(self):
        if hasattr(self, 'bypass_pixmap_cache') and self.bypass_pixmap_cache:
            self.fade_anim.stop()
            self.old_pixmap = None
            self.current_pixmap = self.bypass_pixmap_cache
            self.overlay_text = getattr(self, 'bypass_text_cache', "")
            self.raw_preview_img = getattr(self, 'bypass_raw_cache', self.raw_preview_img)
            self.scope_pixmap = getattr(self, 'bypass_scope_cache', self.scope_pixmap)
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

        # Draw the scopes overlay when active
        if self.show_scopes and self.scope_pixmap and w > 0:
            sx = self.width() - self.scope_pixmap.width() - 20
            sy = self.height() - self.scope_pixmap.height() - 20
            painter.setOpacity(1.0)
            painter.drawPixmap(sx, sy, self.scope_pixmap)

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
        self.toggle_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.update_button_style(expanded)

        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(5, 5, 5, 5)
        self.content_area.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content_area)

    def update_button_style(self, is_expanded):
        if is_expanded:
            self.toggle_button.setStyleSheet("""
                QToolButton { border: none; font-weight: bold; text-align: center;
                              background-color: #0078D7; color: white; padding: 8px; border-radius: 4px; }
            """)
        else:
            self.toggle_button.setStyleSheet("""
                QToolButton { border: none; font-weight: bold; text-align: center;
                              background-color: #e0e0e0; color: black; padding: 8px; border-radius: 4px; }
                QToolButton:hover { background-color: #d0d0d0; }
            """)

    def set_expanded(self, expanded):
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.update_button_style(expanded)
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
        <p>Created by <b>Robbie Ferguson // The Bald Nerd</b>.</p>
        <p>macOS Binary thanks to <b>Bill Marshall</b>.</p>
        <p>Robbie is the founder of <a href="https://Category5.TV/">Category5 TV Network</a>, host of several technology broadcasts,
        and the creator of <a href="https://nemslinux.com/">NEMS Linux</a>.</p>
        <p>I build this software and many other creative projects to share with the community.
        If you find it useful, please consider supporting my work!</p>
        <p>Visit <a href="https://baldnerd.com/">baldnerd.com</a> to learn more about me.</p>
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

            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))

            latest_version = data.get("currentVersion", "")
            if not latest_version: return

            def parse_v(v): return tuple(map(int, (v.split("."))))

            if parse_v(latest_version) > parse_v(VERSION):
                msg = f"A new version (<b>v{latest_version}</b>) is available!<br>You are currently running v{VERSION}.<br><br>"
                changes = data.get("changes", [])
                if changes:
                    msg += "<b>Changes:</b><ul>" + "".join([f"<li>{c}</li>" for c in changes]) + "</ul><br>"
                download_url = data.get("downloadUrl", "https://github.com/Cat5TV/360-portrait-studio")
                msg += f"Please <a href='{download_url}'>download the latest release</a>."
                box = QMessageBox(self)
                box.setWindowTitle("Update Available")
                box.setTextFormat(Qt.TextFormat.RichText)
                box.setText(msg)
                box.exec()
            elif parse_v(latest_version) < parse_v(VERSION):
                QMessageBox.information(self, "Development Build", f"You are running v{VERSION}, which is an unreleased development build. The latest stable release is v{latest_version}.")
            else:
                QMessageBox.information(self, "Up to Date", f"You are running the latest version (v{VERSION}).")
        except Exception as e:
            QMessageBox.warning(self, "Update Check Failed", f"Could not connect to the update server.\n\nError: {e}")

class ImageInfoDialog(QDialog):
    # Added src_dims to the initialization
    def __init__(self, parent, file_path, current_exif, export_dims, src_dims):
        super().__init__(parent)
        self.setWindowTitle("Image Information")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll_layout = QVBoxLayout(content)

        # Pull basic image meta (O(1) file system touch for sizing)
        short_name = os.path.basename(file_path)
        img_size_bytes = os.path.getsize(file_path)
        img_size_mb = img_size_bytes / (1024 * 1024)

        # Retrieve width and height without OpenCV re-reads
        w, h = src_dims

        # Metadata parsing (Safety-checked defaults)
        make, model, exif_datetime, date_taken, shutter, f_stop, iso = "Unknown", "Unknown", "Unknown", "Unknown", "Unknown", "Unknown", "Unknown"
        lat_val, lon_val, map_url = None, None, None
        camera_buy_url = None

        if current_exif:
            # Camera details
            try: make = current_exif.get("0th", {}).get(piexif.ImageIFD.Make, b"").decode('utf-8').strip()
            except Exception: pass
            try: model = current_exif.get("0th", {}).get(piexif.ImageIFD.Model, b"").decode('utf-8').strip()
            except Exception: pass

            # Run normalization & affiliate URL lookup against recognizing array
            lookup_key = (make.lower(), model.lower())
            if lookup_key in RECOGNIZED_CAMERAS:
                camera_info = RECOGNIZED_CAMERAS[lookup_key]
                make = camera_info["maker"]
                model = camera_info["model"]
                camera_buy_url = camera_info["url"]

            def format_exif_date(raw_date_bytes):
                if not raw_date_bytes:
                    return "Unknown"
                try:
                    # Safely convert bytes to string
                    raw_str = raw_date_bytes.decode('utf-8', 'ignore').strip() if isinstance(raw_date_bytes, bytes) else str(raw_date_bytes).strip()

                    # Ensure standard EXIF datetime string is normalized (YYYY:MM:DD HH:MM:SS)
                    # Often includes sub-second or time-zone suffixes we want to strip
                    clean_str = raw_str.split('.')[0].split('+')[0].split('-')[0].strip()

                    # Target correct standard EXIF format using colons: YYYY:MM:DD HH:MM:SS
                    # NOTE: %Y:%m:%d is required. Colons are native, NOT hyphens or slashes.
                    parsed_dt = datetime.strptime(clean_str, "%Y:%m:%d %H:%M:%S")

                    # Returns fully formatted: October 31, 2026 01:35 PM
                    return parsed_dt.strftime("%B %d, %Y %I:%M %p")
                except Exception as e:
                    # If EXIF format didn't match standard, print the parsing crash to terminal for inspection,
                    # and return raw values gracefully so the UI doesn't fail.
                    print(f"Debug: EXIF date parsing failed for input '{raw_date_bytes}': {e}")
                    return str(raw_str) if 'raw_str' in locals() else str(raw_date_bytes)

            try:
                datetime_val = current_exif.get("0th", {}).get(piexif.ImageIFD.DateTime)
                exif_datetime = format_exif_date(datetime_val)
            except Exception:
                exif_datetime = "Unknown"

            try:
                date_taken_val = current_exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
                date_taken = format_exif_date(date_taken_val)
            except Exception:
                date_taken = "Unknown"

            # Photo exposure settings
            try:
                shut_data = current_exif.get("Exif", {}).get(piexif.ExifIFD.ExposureTime)
                if shut_data: shutter = f"{shut_data[0]}/{shut_data[1]}s" if shut_data[0] != 1 else f"1/{shut_data[1]}s"
            except Exception: pass
            try:
                f_data = current_exif.get("Exif", {}).get(piexif.ExifIFD.FNumber)
                if f_data: f_stop = f"f/{f_data[0]/f_data[1]:.1f}"
            except Exception: pass
            try:
                iso_data = current_exif.get("Exif", {}).get(piexif.ExifIFD.ISOSpeedRatings)
                if iso_data: iso = str(iso_data)
            except Exception: pass

            # Coordinates extraction (converts Rational HMS to decimal)
            try:
                gps = current_exif.get("GPS", {})
                lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef)
                lat = gps.get(piexif.GPSIFD.GPSLatitude)
                lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef)
                lon = gps.get(piexif.GPSIFD.GPSLongitude)

                if lat and lon and lat_ref and lon_ref:
                    # Rational decoding
                    def to_decimal(val, ref):
                        deg = val[0][0] / val[0][1]
                        min_val = val[1][0] / val[1][1]
                        sec = val[2][0] / val[2][1]
                        decimal = deg + (min_val / 60.0) + (sec / 3600.0)
                        if ref in [b'S', b'W', 'S', 'W']:
                            decimal = -decimal
                        return decimal

                    lat_val = to_decimal(lat, lat_ref)
                    lon_val = to_decimal(lon, lon_ref)

                    # Validate against 0,0x0,0 default strings
                    if abs(lat_val) > 0.0001 or abs(lon_val) > 0.0001:
                        map_url = f"https://www.openstreetmap.org/?mlat={lat_val}&mlon={lon_val}#map=16/{lat_val}/{lon_val}"
            except Exception:
                lat_val, lon_val, map_url = None, None, None

        # Fallback to general file metadata timestamp if EXIF was empty
        display_time = date_taken if date_taken and date_taken != "Unknown" else (exif_datetime if exif_datetime and exif_datetime != "Unknown" else "Unknown")

        # Setup display format with dynamic referral logic for known devices
        maker_display = make
        model_display = model
        if camera_buy_url:
            maker_display = f"{make}"
            model_display = f'{model} <a href="{camera_buy_url}" style="color: #FF9900; font-size: 11px; text-decoration: none;">🛒 [View on Amazon]</a>'

        info_html = f"""
        <h2>File</h2>
        <p>
          <b>Filename:</b> {short_name}
          <br />
          <b>Timestamp:</b> {display_time}
          <br />
          <b>Source Dimensions:</b> {w} x {h}
          <br />
          <b>Source File Size:</b> {img_size_mb:.2f} MB
          <br />
          <b>Current Export Dimensions:</b> {export_dims[0]} x {export_dims[1]} pixels
        </p>
        <hr>
        <h2>Camera</h2>
        <p>
          <b>Camera:</b> {maker_display} {model_display}
          <br />
          <b>Aperture:</b> {f_stop}
          <br />
          <b>Shutter Speed:</b> {shutter}
          <br />
          <b>ISO Speed:</b> {iso}
        </p>
        """

        if map_url:
            info_html += f"""
            <hr>
            <h2>Location</h2>
            <p>
              <b>Geocoordinates:</b> {lat_val:.6f}, {lon_val:.6f}
              <br />
              <a href="{map_url}" style="color: #FF9900; font-size: 11px; text-decoration: none;">🌐 [View on OpenStreetMap]</a>
            </p>
            """

        lbl = QLabel(info_html)
        lbl.setOpenExternalLinks(True)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        scroll_layout.addWidget(lbl)

        content.setLayout(scroll_layout)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

class ImageBrowserDialog(QDialog):
    def __init__(self, parent, start_dir):
        super().__init__(parent)
        self.setWindowTitle("Browse 360 Images")
        self.resize(1100, 750)
        self.current_dir = os.path.normpath(start_dir.strip(' "\''))
        self.selected_file = None
        self.worker = None
        self.item_pixmaps = {}

        # Hover Animation State
        self.hovered_item = None
        self.hover_frame = 0
        self.hover_timer = QTimer(self)
        self.hover_timer.timeout.connect(self.animate_hover)

        main_layout = QHBoxLayout(self)

        # Sidebar Navigation
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(200)
        self.sidebar.setStyleSheet("QListWidget { font-size: 14px; }")
        self.populate_sidebar()
        self.sidebar.itemClicked.connect(self.sidebar_nav)
        main_layout.addWidget(self.sidebar)

        # Main Browser Area
        browser_layout = QVBoxLayout()

        nav_layout = QHBoxLayout()
        self.btn_up = QPushButton("⬆ Up")
        self.btn_up.setAutoDefault(False)
        self.btn_up.clicked.connect(self.go_up)
        self.path_edit = QLineEdit(self.current_dir)
        self.path_edit.returnPressed.connect(self.load_directory)
        nav_layout.addWidget(self.btn_up)
        nav_layout.addWidget(self.path_edit)
        browser_layout.addLayout(nav_layout)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListView.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(256, 256))
        self.list_widget.setGridSize(QSize(280, 320)) # <--- Forces a strict uniform grid
        self.list_widget.setWordWrap(True)            # <--- Wraps text onto a second line
        self.list_widget.setTextElideMode(Qt.TextElideMode.ElideRight) # <--- Truncates with "..."
        self.list_widget.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_widget.setSpacing(15)
        self.list_widget.setMouseTracking(True)
        self.list_widget.setStyleSheet("""
            QListWidget::item:selected { background-color: #0078D7; color: white; border: 2px solid white; border-radius: 5px; }
            QListWidget::item:hover { background-color: #333; color: white; border-radius: 5px; }
        """)
        self.list_widget.itemDoubleClicked.connect(self.accept_selection)
        self.list_widget.itemEntered.connect(self.on_item_entered)
        self.list_widget.viewport().leaveEvent = self.on_viewport_leave
        browser_layout.addWidget(self.list_widget)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setTextVisible(False)
        browser_layout.addWidget(self.progress_bar)

        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_open = QPushButton("Open Selected")
        self.btn_open.setAutoDefault(False)
        self.btn_open.clicked.connect(self.accept_selection)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_open)
        browser_layout.addLayout(btn_layout)

        main_layout.addLayout(browser_layout)
        self.load_directory()

    def populate_sidebar(self):
        places = [
            ("Home", QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)),
            ("Desktop", QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)),
            ("Pictures", QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)),
            ("Documents", QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)),
            ("Downloads", QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation))
        ]
        for name, path in places:
            if path:
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.sidebar.addItem(item)

        from PyQt6.QtCore import QDir
        for drive in QDir.drives():
            drive_path = drive.absolutePath()
            # Convert Qt's universal forward slash into the OS's native slash for the display label
            native_path = os.path.normpath(drive_path)
            item = QListWidgetItem(f"Drive ({native_path})")

            # Store the native path for the actual navigation action
            item.setData(Qt.ItemDataRole.UserRole, native_path)
            self.sidebar.addItem(item)

    def sidebar_nav(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        self.path_edit.setText(path)
        self.load_directory()

    def go_up(self):
        new_dir = os.path.dirname(self.current_dir)
        if new_dir and new_dir != self.current_dir:
            self.path_edit.setText(new_dir)
            self.load_directory()

    def load_directory(self):
        clean_path = os.path.normpath(self.path_edit.text().strip(' "\''))
        if not os.path.exists(clean_path): return

        self.current_dir = clean_path
        self.path_edit.setText(self.current_dir)

        # Stop animations BEFORE clearing the list to prevent C++ deletion crashes
        self.hover_timer.stop()
        self.hovered_item = None

        self.list_widget.clear()
        self.item_pixmaps.clear()

        if self.worker:
            self.worker.stop()
            self.worker.wait()

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self.worker = ThumbnailWorker(self.current_dir)
        self.worker.folder_ready.connect(self.add_folder)
        self.worker.thumb_ready.connect(self.add_thumbnail)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished_dir.connect(self.hide_progress)
        self.worker.start()

    def add_folder(self, folder_path):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, folder_path)
        item.setData(Qt.ItemDataRole.UserRole + 1, "folder")
        item.setText(f"📁 {os.path.basename(folder_path)}")

        pixmap = QPixmap(256, 256)
        pixmap.fill(QColor(40, 40, 40))
        item.setIcon(QIcon(pixmap))
        self.list_widget.addItem(item)

    def add_thumbnail(self, file_path, thumb_path, is_360, is_opened):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, file_path)
        item.setData(Qt.ItemDataRole.UserRole + 1, "image")
        item.setData(Qt.ItemDataRole.UserRole + 2, is_360)
        item.setData(Qt.ItemDataRole.UserRole + 3, is_opened) # Save opened state for animation redraws
        item.setText(os.path.basename(file_path))

        full_pixmap = QPixmap(thumb_path)
        self.item_pixmaps[file_path] = full_pixmap

        # Default to Center Frame (1) for 360s, or Frame (0) for flats
        start_frame = 1 if is_360 else 0
        self.render_icon(item, full_pixmap, start_frame, is_360, is_opened)
        self.list_widget.addItem(item)

    def render_icon(self, item, full_pixmap, frame_idx, is_360, is_opened):
        crop = full_pixmap.copy(frame_idx * 256, 0, 256, 256) if is_360 else full_pixmap.copy()
        painter = QPainter(crop)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        badge_text = "360°" if is_360 else "2D FLAT"
        badge_color = QColor(0, 120, 215, 230) if is_360 else QColor(100, 100, 100, 230)
        painter.setBrush(badge_color)
        painter.setPen(Qt.GlobalColor.transparent)
        painter.drawRect(0, 0, 75, 30)
        painter.setPen(Qt.GlobalColor.white)
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(14)
        painter.setFont(font)
        painter.drawText(0, 0, 75, 30, Qt.AlignmentFlag.AlignCenter, badge_text)

        if is_opened:
            painter.setBrush(QColor(40, 167, 69, 230))
            painter.setPen(Qt.GlobalColor.transparent)
            painter.drawEllipse(crop.width() - 40, 5, 35, 35)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(crop.width() - 40, 5, 35, 35, Qt.AlignmentFlag.AlignCenter, "✓")

        painter.end()
        item.setIcon(QIcon(crop))

    def on_viewport_leave(self, event):
        self.hover_timer.stop()
        self.reset_hovered()

    def on_item_entered(self, item):
        self.reset_hovered() # Reset previous item to center before animating new one
        try:
            if item.data(Qt.ItemDataRole.UserRole + 1) == "image" and item.data(Qt.ItemDataRole.UserRole + 2):
                self.hovered_item = item
                self.hover_frame = 1 # Start animation from Center frame
                self.hover_timer.start(500)
        except RuntimeError:
            pass

    def reset_hovered(self):
        if self.hovered_item:
            try:
                fp = self.hovered_item.data(Qt.ItemDataRole.UserRole)
                if fp in self.item_pixmaps:
                    is_360 = self.hovered_item.data(Qt.ItemDataRole.UserRole + 2)
                    is_opened = self.hovered_item.data(Qt.ItemDataRole.UserRole + 3)
                    # Reset back to Center Frame (1)
                    self.render_icon(self.hovered_item, self.item_pixmaps[fp], 1 if is_360 else 0, is_360, is_opened)
            except RuntimeError:
                pass # Item was deleted (e.g., user navigated away), ignore safely
            self.hovered_item = None

    def animate_hover(self):
        if not self.hovered_item: return
        try:
            self.hover_frame = (self.hover_frame + 1) % 3
            fp = self.hovered_item.data(Qt.ItemDataRole.UserRole)
            is_360 = self.hovered_item.data(Qt.ItemDataRole.UserRole + 2)
            is_opened = self.hovered_item.data(Qt.ItemDataRole.UserRole + 3)
            self.render_icon(self.hovered_item, self.item_pixmaps[fp], self.hover_frame, is_360, is_opened)
        except RuntimeError:
            # Catch deletion mid-animation and stop timer
            self.hover_timer.stop()
            self.hovered_item = None

    def accept_selection(self, item=None):
        selected = self.list_widget.selectedItems()
        if not selected and not item: return

        target = item if item else selected[0]
        path = target.data(Qt.ItemDataRole.UserRole)
        item_type = target.data(Qt.ItemDataRole.UserRole + 1)

        if item_type == "folder":
            self.path_edit.setText(path)
            self.load_directory()
        else:
            self.selected_file = path
            self.accept()

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
            self.worker.wait()
        super().closeEvent(event)

    def update_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def hide_progress(self):
        self.progress_bar.setVisible(False)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Baldnerd's 360 Portrait Studio v{VERSION}")
        self.resize(1300, 900)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.projector = None

        if sys.platform == "darwin":
            self.setWindowIcon(QIcon(resource_path("assets/icons/icon.icns")))
        else:
            self.setWindowIcon(QIcon(resource_path("assets/icons/icon.ico")))

        # Enable drag-and-drop file loading
        self.setAcceptDrops(True)

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

        self.btn_info = QPushButton("Image Info")
        self.btn_info.clicked.connect(self.show_image_info)
        self.btn_info.setMinimumHeight(35)
        self.btn_info.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_info.setEnabled(False) # Start disabled until an image is loaded

        self.btn_about = QPushButton("About")
        self.btn_about.clicked.connect(self.show_about)
        self.btn_about.setMinimumHeight(35)
        self.btn_about.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        top_btn_layout.addWidget(self.btn_load)
        top_btn_layout.addWidget(self.btn_import)
        top_btn_layout.addWidget(self.btn_about)
        top_btn_layout.addWidget(self.btn_info)
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
        self.pos_box = CollapsibleBox("Camera Position", expanded=False)
        self.sliders['yaw'] = self.create_slider_row(self.pos_box.content_layout, "Pan (Yaw):", -180, 180, 0)
        self.sliders['pitch'] = self.create_slider_row(self.pos_box.content_layout, "Tilt (Pitch):", -90, 90, 0)
        self.sliders['roll'] = self.create_slider_row(self.pos_box.content_layout, "Horizon (Roll):", -180, 180, 0)
        self.sliders['zoom'] = self.create_slider_row(self.pos_box.content_layout, "Zoom:", 0, 110, 30)
        self.boxes.append(self.pos_box)

        # --- Collapsible: Color ---
        self.color_box = CollapsibleBox("Color Grading", expanded=False)
        self.create_wb_row(self.color_box.content_layout)
        self.sliders['tint'] = self.create_slider_row(self.color_box.content_layout, "Tint:", -50, 50, 0)
        self.sliders['bright'] = self.create_slider_row(self.color_box.content_layout, "Brightness:", -100, 100, 0)
        self.sliders['contrast'] = self.create_slider_row(self.color_box.content_layout, "Contrast:", -50, 50, 0)
        self.sliders['shadows'] = self.create_slider_row(self.color_box.content_layout, "Boost Shadows:", 0, 100, 0)
        self.sliders['highlights'] = self.create_slider_row(self.color_box.content_layout, "Boost Highlights:", 0, 100, 0)
        self.sliders['sat'] = self.create_slider_row(self.color_box.content_layout, "Saturation:", -50, 100, 0)
        self.sliders['clarity'] = self.create_slider_row(self.color_box.content_layout, "Clarity (Auto Levels):", 0, 100, 0)
        self.boxes.append(self.color_box)

        # --- Collapsible: Effects ---
        self.fx_box = CollapsibleBox("Enhancements and Effects", expanded=False)
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

    # Drop handler for opening files by dropping onto the window
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(url.toLocalFile().lower().endswith(('.jpg', '.jpeg', '.png', '.hdr', '.exr', '.tiff', '.tif', '.webp')) for url in urls):
                event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            file_path = url.toLocalFile()
            if file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.hdr', '.exr', '.tiff', '.tif', '.webp')):
                self.load_image_from_path(file_path)
                break
    # /Drop handler for opening files by dropping onto the window

    def show_image_info(self):
        if not self.projector: return

        # Determine planned export resolution using current settings & source dimensions
        yaw, pitch, roll = self.sliders['yaw'].value(), self.sliders['pitch'].value(), self.sliders['roll'].value()
        dewarp = self.sliders['dewarp'].value()

        # Use the mathematical pipeline logic to extract target export dimensions
        pixels_per_degree = self.projector.width / 360.0
        out_w = int(self.get_fov() * pixels_per_degree)
        out_h = int(out_w / self.get_aspect_ratio())

        # Grab dimensions directly from the projector
        src_dimensions = (self.projector.width, self.projector.height)

        # Pass our pre-loaded coordinates and properties to keep the UI snappy
        dlg = ImageInfoDialog(self, self.projector.image_path, self.current_exif, (out_w, out_h), src_dimensions)
        dlg.exec()

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

            # Show content and apply active styling
            clicked_box.content_area.setVisible(True)
            clicked_box.update_button_style(True)

            # Toggle scopes if Color Adjustments is the active box
            self.image_label.set_scopes_visible(clicked_box == self.color_box)
        else:
            clicked_box.set_expanded(False)
            if clicked_box == self.color_box:
                self.image_label.set_scopes_visible(False)

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

        self.aspect_combo.setCurrentIndex(state.get('aspect', 0))
        self.orient_combo.setCurrentIndex(state.get('orient', 0))

        self.float_yaw = state.get('float_yaw', 0.0)
        self.float_pitch = state.get('float_pitch', 0.0)

        slider_states = state.get('sliders', {})
        for k, v in slider_states.items():
            if k in self.sliders:
                self.sliders[k].setValue(v)

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

        if not self.projector:
            self.btn_info.setEnabled(False)

        self.aspect_combo.setCurrentIndex(0)
        self.orient_combo.setCurrentIndex(0)

        self.float_yaw = 0.0
        self.float_pitch = 0.0

        self.sliders['yaw'].setValue(0)
        self.sliders['pitch'].setValue(0)
        self.sliders['roll'].setValue(0)
        self.sliders['zoom'].setValue(30)

        self.sliders['temp'].setValue(0)
        self.sliders['tint'].setValue(0)
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
        DB_MANAGER.prune_missing_files()
        start_path = self.last_load_dir if self.last_load_dir else os.path.expanduser("~")

        dlg = ImageBrowserDialog(self, start_path)
        # PyQt6 returns an int (1 for Accepted). Check for 1 explicitly.
        if dlg.exec() == 1 and dlg.selected_file:
            self.load_image_from_path(dlg.selected_file)

    def load_image_from_path(self, file_name):
        # Leave the OS-native path exactly as-is for the database and projector
        file_name = file_name.strip(' "\'')

        DB_MANAGER.mark_opened(file_name) # Registers correctly in DB

        self.last_load_dir = os.path.dirname(file_name)
        self.settings.setValue("last_load_dir", self.last_load_dir)

        short_name = os.path.basename(file_name)
        self.setWindowTitle(f"Baldnerd's 360 Portrait Studio v{VERSION} - {short_name}")

        self.image_label.set_master_pixmap(None, None)
        self.image_label.setText("Loading... Please Wait.")
        self.projector = None
        QApplication.processEvents()

        self.projector = EquirectangularProjector(file_name)

        self.current_exif = None
        if HAS_PIEXIF:
            try:
                # ONLY manipulate the path for piexif's fragile parser
                exif_path = file_name.replace('\\', '/')
                self.current_exif = piexif.load(exif_path)
            except Exception:
                pass

        self.btn_info.setEnabled(True)

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
        tint = self.sliders['tint'].value()
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

        if temp != 0 or tint != 0:
            img_float = img.astype(np.float32)
            b, g, r = cv2.split(img_float)

            if temp != 0:
                r = np.clip(r * (1.0 + (temp / 150.0)), 0, 255)
                b = np.clip(b * (1.0 - (temp / 150.0)), 0, 255)

            if tint != 0:
                # Positive tint pushes Magenta (reduces green channel)
                # Negative tint pushes Green (increases green channel)
                g = np.clip(g * (1.0 - (tint / 150.0)), 0, 255)

            img = cv2.merge([b, g, r]).astype(np.uint8)

        if bright != 0 or cont != 0:
            alpha = 1.0 + (cont / 100.0)
            beta = bright
            img = cv2.convertScaleAbs(img, alpha=alpha, beta=beta)

        if clarity_val > 0:
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)

            # Process a solid CLAHE pass
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            cl = clahe.apply(l)

            # Opacity blend the effect in on an exponential curve for ultra-smooth low-end control
            blend = (clarity_val / 100.0) ** 2
            l_blended = cv2.addWeighted(cl, blend, l, 1.0 - blend, 0)

            lab = cv2.merge((l_blended, a, b))
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
            # Use float32 math to prevent integer overflow clipping with Vignette/Contrast
            img = (img.astype(np.float32) * (1.0 - mask) + blurred.astype(np.float32) * mask).astype(np.uint8)

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
                is_current_source = os.path.abspath(save_path) == os.path.abspath(self.projector.image_path)

                is_tool_export = False
                json_sidecar = os.path.splitext(save_path)[0] + ".json"

                if os.path.exists(json_sidecar):
                    is_tool_export = True
                elif HAS_PIEXIF and save_path.lower().endswith(('.jpg', '.jpeg')):
                    try:
                        target_exif = piexif.load(save_path)
                        software = target_exif.get("0th", {}).get(piexif.ImageIFD.Software, b"").decode('utf-8', 'ignore')
                        if "Baldnerd" in software:
                            is_tool_export = True
                    except Exception:
                        pass

                if is_current_source or not is_tool_export:
                    QMessageBox.critical(self, "Overwrite Protected",
                                         f"The file {os.path.basename(save_path)} appears to be an original source image.\n\nTo prevent accidental data loss, the software will only overwrite images that it previously exported.")
                    return

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

            if HAS_PIEXIF and (ext.endswith('.jpg') or ext.endswith('.jpeg')):
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
    window.showMaximized()
    sys.exit(app.exec())
