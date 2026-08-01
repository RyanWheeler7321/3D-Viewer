#!/usr/bin/env python3
"""Small local GLB/GLTF/ZIP viewer."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    " ".join(
        [
            os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""),
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-gpu-rasterization",
            "--disable-zero-copy",
            "--disable-partial-raster",
            "--disable-features=CalculateNativeWinOcclusion",
        ]
    ).strip(),
)

from PySide6.QtCore import QUrl, Qt, QTimer, QByteArray
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

ROOT = Path(__file__).resolve().parent
VENDOR_THREE = ROOT / "vendor" / "three" / "build" / "three.module.js"
VENDOR_HDR_LOADER = ROOT / "vendor" / "three" / "examples" / "jsm" / "loaders" / "HDRLoader.js"
VENDOR_FBX_LOADER = ROOT / "vendor" / "three" / "examples" / "jsm" / "loaders" / "FBXLoader.js"
LOG_PATH = ROOT / "runtime" / "logs" / "3d_viewer.log"
STATE_PATH = ROOT / "runtime" / "window_state.json"
LAST_MODEL_PATH = ROOT / "runtime" / "last_model.json"
ICON_PATH = ROOT / "assets" / "rIcon.ico"
VIEWER_TEMPLATE_PATH = ROOT / "web" / "viewer.html"
ACCENT = "#E3008C"

HDRI_ASSETS = [
    ("Cloud Sky", "kloppenheim_03_puresky_2k.hdr"),
    ("None", None),
    ("Studio", "cyclorama_hard_light_2k.hdr"),
    ("Nature", "forest_slope_2k.hdr"),
    ("Night Desert", "rogland_moonlit_night_2k.hdr"),
    ("City", "canary_wharf_2k.hdr"),
    ("Night City", "neuer_zollhof_2k.hdr"),
]
HDRI_CACHE_DIR = ROOT / "runtime" / "hdri_cache"
HDRI_DOWNLOAD_BASE = "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/{filename}"


def log_line(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


class LoggingPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message: str, line_number: int, source_id: str) -> None:
        log_line(f"js {level}: {message} ({Path(source_id).name}:{line_number})")
        super().javaScriptConsoleMessage(level, message, line_number, source_id)



class QuietHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, fmt: str, *args) -> None:
        try:
            log_line("http " + (fmt % args))
        except Exception:
            pass

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class ThreadingHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sample_process_snapshot(parent_pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        script = (
            "$targetPid=" + str(int(parent_pid)) + ";"
            "$parent=Get-CimInstance Win32_Process -Filter " + ps_quote(f"ProcessId={int(parent_pid)}") + ";"
            "$children=Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $targetPid -or $_.Name -eq 'QtWebEngineProcess.exe' };"
            "$ids=@($parent.ProcessId)+@($children.ProcessId);"
            "$out=@();"
            "foreach($id in $ids | Select-Object -Unique){"
            "$p=Get-Process -Id $id -ErrorAction SilentlyContinue;"
            "if($p){$out += [pscustomobject]@{id=$p.Id;name=$p.ProcessName;cpu=[math]::Round($p.CPU,3);ws=[math]::Round($p.WorkingSet64/1MB,1);pm=[math]::Round($p.PrivateMemorySize64/1MB,1)}}"
            "};"
            "$out | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=2.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (completed.stdout or "").strip()
    except Exception as exc:
        return f"process_snapshot_error={exc}"


def win_path(path: Path) -> str:
    return str(path).replace("/", "\\")


def open_folder(path: Path) -> None:
    import subprocess

    subprocess.Popen(["explorer.exe", "/select,", win_path(path)], close_fds=True)


def load_window_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_window_state(window: QMainWindow) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "geometry_hex": bytes(window.saveGeometry().toHex()).decode("ascii"),
            "normal_rect": [window.x(), window.y(), window.width(), window.height()],
        }
        STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"window_state_save_failed: {exc}\n")
        except Exception:
            pass


def restore_window_state(window: QMainWindow) -> bool:
    data = load_window_state()
    rect = data.get("normal_rect")
    expected_rect: tuple[int, int, int, int] | None = None
    if isinstance(rect, list) and len(rect) == 4:
        try:
            x, y, w, h = [int(v) for v in rect]
            if w > 200 and h > 200:
                expected_rect = (x, y, w, h)
        except Exception:
            expected_rect = None

    geometry_hex = str(data.get("geometry_hex") or "")
    if geometry_hex:
        try:
            restored = bool(window.restoreGeometry(QByteArray.fromHex(geometry_hex.encode("ascii"))))
            if restored:
                if not expected_rect:
                    return True
                _, _, expected_w, expected_h = expected_rect
                if abs(window.width() - expected_w) <= 8 and abs(window.height() - expected_h) <= 8:
                    return True
        except Exception:
            pass

    if expected_rect:
        try:
            x, y, w, h = expected_rect
            window.setGeometry(x, y, w, h)
            return True
        except Exception:
            pass
    return False


def save_last_model(model_path: Path) -> None:
    try:
        LAST_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_MODEL_PATH.write_text(
            json.dumps({"model_path": str(model_path), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log_line(f"last model save failed: {exc}")


def load_last_model_from_state() -> Path | None:
    try:
        data = json.loads(LAST_MODEL_PATH.read_text(encoding="utf-8"))
        raw = str(data.get("model_path") or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.exists():
                return path.resolve()
    except Exception:
        pass
    return None


def load_last_model_from_log() -> Path | None:
    try:
        if not LOG_PATH.exists():
            return None
        for line in reversed(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]):
            match = re.search(r"launch model=(.*?) resolved=", line)
            if not match:
                continue
            raw = match.group(1).strip()
            if raw:
                path = Path(raw).expanduser()
                if path.exists():
                    return path.resolve()
    except Exception:
        pass
    return None


def resolve_last_model() -> Path | None:
    return load_last_model_from_state() or load_last_model_from_log()


def monitor_label(screen) -> str:
    if screen is None:
        return "unknown"
    geometry = screen.geometry()
    return (
        f"{screen.name()} "
        f"{geometry.x()},{geometry.y()},{geometry.width()}x{geometry.height()} "
        f"{screen.refreshRate():.1f}Hz"
    )


def place_on_screen(window: QMainWindow, screen, width: int, height: int) -> None:
    if screen is None:
        return
    geometry = screen.availableGeometry()
    target_width = min(width, max(200, geometry.width() - 40))
    target_height = min(height, max(200, geometry.height() - 40))
    x = geometry.x() + max(20, (geometry.width() - target_width) // 2)
    y = geometry.y() + max(20, (geometry.height() - target_height) // 2)
    window.setGeometry(x, y, target_width, target_height)


def place_on_right_monitor(window: QMainWindow, width: int, height: int) -> None:
    screens = QGuiApplication.screens()
    screen = max(screens, key=lambda s: s.geometry().x()) if screens else QGuiApplication.primaryScreen()
    place_on_screen(window, screen, width, height)


def place_on_monitor(window: QMainWindow, width: int, height: int, preference: str) -> None:
    if preference == "right":
        place_on_right_monitor(window, width, height)
        return

    screens = QGuiApplication.screens()
    if preference == "fast" and screens:
        screen = max(screens, key=lambda candidate: candidate.refreshRate())
    else:
        screen = QGuiApplication.primaryScreen()
    place_on_screen(window, screen, width, height)


def download_missing_hdris() -> None:
    """Best-effort HDRI cache fill for normal interactive launches."""
    HDRI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for label, filename in HDRI_ASSETS:
        if filename is None:
            continue
        path = HDRI_CACHE_DIR / filename
        if path.exists() and path.stat().st_size > 0:
            continue
        url = HDRI_DOWNLOAD_BASE.format(filename=filename)
        try:
            log_line(f"hdri download start label={label} url={url}")
            req = urllib.request.Request(url, headers={"User-Agent": "r7321-3d-viewer/1.0"})
            tmp = path.with_suffix(path.suffix + ".download")
            with urllib.request.urlopen(req, timeout=45) as response, tmp.open("wb") as fh:
                shutil.copyfileobj(response, fh)
            tmp.replace(path)
            log_line(f"hdri download ok label={label} file={path.name} bytes={path.stat().st_size}")
        except Exception as exc:
            log_line(f"hdri download failed label={label} file={filename}: {exc}")


def build_hdri_options() -> list[dict]:
    options: list[dict] = []
    for label, filename in HDRI_ASSETS:
        if filename is None:
            options.append({"label": label, "url": None})
            continue
        path = HDRI_CACHE_DIR / filename
        url = None
        if path.exists():
            url = "/" + quote(path.resolve().relative_to(ROOT.resolve()).as_posix())
        options.append({"label": label, "url": url, "filename": filename})
    return options


VARIANT_SIDECAR_SUFFIX = ".viewer_variants.json"


def _resolve_variant_path(manifest_path: Path, value: str) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    try:
        return path.resolve()
    except Exception:
        return path.absolute()


def _variant_manifest_candidates(model_path: Path) -> list[Path]:
    candidates = [
        model_path.with_name(model_path.stem + VARIANT_SIDECAR_SUFFIX),
        model_path.parent / "viewer_variants.json",
    ]
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            key = candidate.resolve()
        except Exception:
            key = candidate.absolute()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def load_preview_variants(model_path: Path) -> tuple[list[dict], int, Path | None]:
    current = model_path.expanduser().resolve()
    for manifest_path in _variant_manifest_candidates(current):
        if not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log_line(f"avatar variant manifest failed path={manifest_path}: {exc}")
            continue
        variants: list[dict] = []
        for item in data.get("variants", []):
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict):
                continue
            path = _resolve_variant_path(manifest_path, str(item.get("path", "")))
            if not path or not path.exists():
                continue
            label = str(item.get("label") or path.stem).strip() or path.stem
            variants.append({"label": label, "path": path})
        if not variants:
            continue
        index = 0
        for idx, variant in enumerate(variants):
            if variant["path"] == current:
                index = idx
                break
        return variants, index, manifest_path
    return [], -1, None

def build_html(model_name: str, model_url: str, model_path: Path, model_kind: str, initial_view_state: dict | None = None) -> str:
    title = html.escape(model_name)
    path_text = html.escape(str(model_path))
    hdri_json = json.dumps(build_hdri_options(), ensure_ascii=False)
    model_url_json = json.dumps(model_url)
    model_kind_json = json.dumps(model_kind)
    initial_view_state_json = json.dumps(initial_view_state or None, ensure_ascii=False)
    template = VIEWER_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__ACCENT__", ACCENT)
        .replace("__TITLE__", title)
        .replace("__PATH__", path_text)
        .replace("__MODEL_URL__", model_url_json)
        .replace("__MODEL_KIND__", model_kind_json)
        .replace("__HDRI_OPTIONS__", hdri_json)
        .replace("__INITIAL_VIEW_STATE__", initial_view_state_json)
    )



class ViewerWindow(QMainWindow):
    def __init__(self, model_path: Path, display_path: Path, server_root: Path, port: int) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.server_root = server_root
        self.port = port
        self.runtime_root = STATE_PATH.parent
        self.model_path = display_path
        self.served_model_path = model_path
        self._diag_last_wall = time.perf_counter()
        self._diag_last_cpu = time.process_time()
        self._diag_process_snapshot_index = 0
        self._topmost_enabled = True
        self.preview_variants: list[dict] = []
        self.preview_variant_index = -1
        self.preview_variant_manifest: Path | None = None
        self._pending_view_state: dict | None = None
        self._last_view_state: dict | None = None
        self.setWindowTitle(f"3D Viewer - {display_path.name}")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.timeout.connect(lambda: save_window_state(self))
        self._diag_timer = QTimer(self)
        self._diag_timer.timeout.connect(self._log_qt_diagnostics)
        self._diag_timer.start(10000)
        self.view = QWebEngineView(self)
        self.view.setPage(LoggingPage(self.view))
        self.setCentralWidget(self.view)
        self.resize(1500, 900)
        self.setStyleSheet("background:#050608;")
        self._load_served_model(model_path, display_path)
        self._add_shortcuts()

    def _viewer_html_path(self) -> Path:
        return self.runtime_root / "viewer.html"

    def _viewer_url(self) -> QUrl:
        rel = self._viewer_html_path().resolve().relative_to(self.server_root.resolve()).as_posix()
        return QUrl(f"http://127.0.0.1:{self.port}/{rel}")

    def _load_served_model(self, served_model_path: Path, display_path: Path) -> None:
        self.model_path = display_path
        self.served_model_path = served_model_path
        self._refresh_preview_variants(display_path)
        self.setWindowTitle(f"3D Viewer - {display_path.name}")
        model_rel = served_model_path.absolute().relative_to(self.server_root.resolve()).as_posix()
        model_url = "/" + quote(model_rel)
        model_kind = served_model_path.suffix.lower().lstrip(".")
        html_path = self._viewer_html_path()
        html_path.parent.mkdir(parents=True, exist_ok=True)
        initial_view_state = self._pending_view_state
        self._pending_view_state = None
        html_path.write_text(build_html(display_path.name, model_url, display_path, model_kind, initial_view_state), encoding="utf-8")
        self.view.load(self._viewer_url())

    def open_model_dialog(self) -> None:
        start_dir = str(self.model_path.parent if self.model_path else Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open 3D Model",
            start_dir,
            "3D Models (*.glb *.gltf *.fbx *.zip);;glTF (*.glb *.gltf);;FBX (*.fbx);;ZIP bundles (*.zip);;All Files (*)",
        )
        if selected:
            self.open_model_path(Path(selected))

    def open_model_path(self, model_path: Path) -> None:
        try:
            model_path = model_path.expanduser().resolve()
            save_window_state(self)
            resolved_model = resolve_model_input(model_path, self.runtime_root)
            served_model = prepare_served_model(resolved_model, self.runtime_root)
            save_last_model(model_path)
            log_line(f"open model selected={model_path} resolved={resolved_model} served={served_model}")
            self._load_served_model(served_model, model_path)
        except Exception as exc:
            log_line(f"open model failed model={model_path}: {exc}")

    def _refresh_preview_variants(self, display_path: Path) -> None:
        self.preview_variants, self.preview_variant_index, self.preview_variant_manifest = load_preview_variants(display_path)
        if self.preview_variants:
            labels = ", ".join(v["label"] for v in self.preview_variants)
            log_line(
                f"avatar variants count={len(self.preview_variants)} index={self.preview_variant_index + 1} "
                f"manifest={self.preview_variant_manifest} labels={labels}"
            )

    def show_viewer_popup(self, text: str) -> None:
        payload = json.dumps(text)
        self.view.page().runJavaScript(f"window.viewerShowModePopup && window.viewerShowModePopup({payload});")

    def swap_avatar_variant(self) -> None:
        if len(self.preview_variants) < 2:
            log_line(f"avatar variant swap unavailable model={self.model_path}")
            self.show_viewer_popup("No avatar variants")
            return
        next_index = (self.preview_variant_index + 1) % len(self.preview_variants)
        variant = self.preview_variants[next_index]
        label = variant["label"]
        path = variant["path"]
        log_line(f"avatar variant swap {self.preview_variant_index + 1}->{next_index + 1} label={label} path={path}")

        def open_with_state(state_text=None, label=label, path=path):
            parsed_state = None
            if isinstance(state_text, str) and state_text.strip():
                try:
                    parsed = json.loads(state_text)
                    if isinstance(parsed, dict):
                        parsed_state = parsed
                except Exception as exc:
                    log_line(f"avatar variant view state parse failed label={label}: {exc}")
            elif isinstance(state_text, dict):
                parsed_state = state_text

            if isinstance(parsed_state, dict):
                self._pending_view_state = parsed_state
                self._last_view_state = parsed_state
                log_line(f"avatar variant preserving view state label={label} keys={','.join(sorted(parsed_state.keys()))}")
            elif isinstance(self._last_view_state, dict):
                self._pending_view_state = self._last_view_state
                log_line(f"avatar variant using last view state label={label} state_type={type(state_text).__name__}")
            else:
                self._pending_view_state = None
                log_line(f"avatar variant no view state label={label} state_type={type(state_text).__name__}")
            self.open_model_path(path)
            QTimer.singleShot(350, lambda label=label: self.show_viewer_popup(f"Avatar: {label}"))

        self.view.page().runJavaScript(
            "window.viewerSnapshotStateJson ? window.viewerSnapshotStateJson() : '';",
            open_with_state,
        )

    def _log_qt_diagnostics(self) -> None:
        now = time.perf_counter()
        cpu = time.process_time()
        wall_delta = max(0.001, now - self._diag_last_wall)
        cpu_pct = max(0.0, (cpu - self._diag_last_cpu) / wall_delta * 100.0)
        self._diag_last_wall = now
        self._diag_last_cpu = cpu
        rect = self.geometry()
        try:
            screen_text = monitor_label(self.windowHandle().screen() if self.windowHandle() else self.screen())
        except Exception:
            screen_text = "unknown"
        hwnd = int(self.winId()) if os.name == "nt" else 0
        foreground = False
        topmost = False
        if os.name == "nt" and hwnd:
            try:
                user32 = ctypes.windll.user32
                foreground = int(user32.GetForegroundWindow() or 0) == hwnd
                GWL_EXSTYLE = -20
                WS_EX_TOPMOST = 0x00000008
                ex_style = int(user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE))
                topmost = bool(ex_style & WS_EX_TOPMOST)
            except Exception:
                pass
        log_line(
            "qt stats "
            f"active={self.isActiveWindow()} visible={self.isVisible()} minimized={self.isMinimized()} "
            f"foreground={foreground} topmost={topmost} cpu_pct={cpu_pct:.1f} "
            f"rect={rect.x()},{rect.y()},{rect.width()}x{rect.height()} screen={screen_text}"
        )

    def log_process_snapshot(self) -> None:
        snapshot = sample_process_snapshot(os.getpid())
        if snapshot:
            log_line(f"process snapshot manual {snapshot}")

    def apply_topmost(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = self._topmost_enabled
        self._topmost_enabled = bool(enabled)
        if os.name != "nt":
            if enabled:
                self.raise_()
            log_line(f"qt topmost={self._topmost_enabled} method=qt-only")
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            insert_after = ctypes.c_void_p(-1 if enabled else -2)
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(ctypes.c_void_p(hwnd), insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            log_line(f"qt topmost={self._topmost_enabled} method=win32")
        except Exception as exc:
            log_line(f"qt topmost failed enabled={enabled}: {exc}")

    def toggle_topmost(self) -> None:
        self.apply_topmost(not self._topmost_enabled)

    def toggle_webgl_pause(self) -> None:
        self.view.page().runJavaScript("window.viewerToggleRenderPause && window.viewerToggleRenderPause();")

    def _add_shortcuts(self) -> None:
        actions = [
            ("Toggle Root Motion", "R", lambda: self.view.page().runJavaScript("window.viewerToggleRootMotion && window.viewerToggleRootMotion();")),
            ("Frame Model", "F", lambda: self.view.page().runJavaScript("window.viewerFrameModel && window.viewerFrameModel();")),
            ("Play/Pause Animation", "Space", lambda: self.view.page().runJavaScript("window.viewerToggleAnimationPlayback && window.viewerToggleAnimationPlayback();")),
            ("Previous Animation Clip", "[", lambda: self.view.page().runJavaScript("window.viewerPreviousAnimationClip && window.viewerPreviousAnimationClip();")),
            ("Next Animation Clip", "]", lambda: self.view.page().runJavaScript("window.viewerNextAnimationClip && window.viewerNextAnimationClip();")),
            ("Restart Animation", "0", lambda: self.view.page().runJavaScript("window.viewerRestartAnimation && window.viewerRestartAnimation();")),
            ("Swap Avatar", "M", self.swap_avatar_variant),
            ("Animation Faster", "+", lambda: self.view.page().runJavaScript("window.viewerAnimationFaster && window.viewerAnimationFaster();")),
            ("Animation Slower", "-", lambda: self.view.page().runJavaScript("window.viewerAnimationSlower && window.viewerAnimationSlower();")),
            ("Toggle Auto-Rotate", "A", lambda: self.view.page().runJavaScript("window.viewerToggleAutoRotate && window.viewerToggleAutoRotate();")),
            ("Toggle Wire", "W", lambda: self.view.page().runJavaScript("window.viewerToggleWire && window.viewerToggleWire();")),
            ("Toggle Wire Xray", "X", lambda: self.view.page().runJavaScript("window.viewerToggleWireXray && window.viewerToggleWireXray();")),
            ("Toggle Clay", "C", lambda: self.view.page().runJavaScript("window.viewerToggleClay && window.viewerToggleClay();")),
            ("Toggle Lighting", "L", lambda: self.view.page().runJavaScript("window.viewerToggleLighting && window.viewerToggleLighting();")),
            ("Toggle HDRI", "H", lambda: self.view.page().runJavaScript("window.viewerToggleHdri && window.viewerToggleHdri();")),
            ("Toggle Background", "B", lambda: self.view.page().runJavaScript("window.viewerToggleBackground && window.viewerToggleBackground();")),
            ("Toggle Diagnostics", "D", lambda: self.view.page().runJavaScript("window.viewerToggleDiagnostics && window.viewerToggleDiagnostics();")),
            ("Toggle Quality", "Q", lambda: self.view.page().runJavaScript("window.viewerToggleQuality && window.viewerToggleQuality();")),
            ("Toggle Topmost", "T", self.toggle_topmost),
            ("Pause WebGL", "P", self.toggle_webgl_pause),
            ("Log Process Snapshot", "I", self.log_process_snapshot),
            ("Open Model", "Ctrl+O", self.open_model_dialog),
            ("Open Folder", "O", lambda: open_folder(self.model_path)),
            ("Close", "Esc", self.close),
        ]
        for name, seq, callback in actions:
            action = QAction(name, self)
            action.setShortcut(QKeySequence(seq))
            action.triggered.connect(callback)
            self.addAction(action)

    def _schedule_geometry_save(self) -> None:
        if hasattr(self, "_geometry_save_timer"):
            self._geometry_save_timer.start(450)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._schedule_geometry_save()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def closeEvent(self, event) -> None:
        save_window_state(self)
        super().closeEvent(event)


def safe_runtime_name(model_path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_path.stem).strip("._") or "model"
    return stem[:80]


def runtime_bundle_name(model_path: Path) -> str:
    try:
        key = str(model_path.resolve()).lower()
    except Exception:
        key = str(model_path).lower()
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:10]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{safe_runtime_name(model_path)}_{digest}_{stamp}"


def cleanup_old_runtime_dirs(parent: Path, label: str, max_age_hours: int = 24) -> None:
    if not parent.exists():
        return
    cutoff = time.time() - (max_age_hours * 3600)
    for child in parent.iterdir():
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            log_line(f"cleaned old {label} {child}")
        except Exception as exc:
            log_line(f"old {label} cleanup skipped {child}: {exc}")


def collect_gltf_dependencies(model_path: Path) -> set[Path]:
    try:
        data = json.loads(model_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_line(f"gltf dependency parse failed {model_path}: {exc}")
        return set()
    deps: set[Path] = set()
    for section in ("buffers", "images"):
        for item in data.get(section, []) or []:
            uri = item.get("uri") if isinstance(item, dict) else None
            if not uri or uri.startswith("data:"):
                continue
            parsed = urlparse(uri)
            if parsed.scheme or parsed.netloc:
                continue
            rel = Path(unquote(parsed.path))
            if rel.is_absolute() or ".." in rel.parts:
                log_line(f"skipped unsafe gltf dependency uri={uri}")
                continue
            deps.add(rel)
    return deps


def prepare_served_model(model_path: Path, runtime_root: Path) -> Path:
    cleanup_old_runtime_dirs(runtime_root / "models", "model bundle")
    bundle_dir = runtime_root / "models" / runtime_bundle_name(model_path)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    if model_path.suffix.lower() == ".gltf":
        deps = collect_gltf_dependencies(model_path)
        shutil.copy2(model_path, bundle_dir / model_path.name)
        copied = 1
        for rel in sorted(deps, key=lambda p: p.as_posix()):
            src = model_path.parent / rel
            if not src.exists():
                log_line(f"missing gltf dependency {src}")
                continue
            dest = bundle_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
        log_line(f"prepared gltf bundle model={model_path} deps={len(deps)} copied={copied} dest={bundle_dir}")
        return bundle_dir / model_path.name

    model_link = bundle_dir / model_path.name
    try:
        os.symlink(str(model_path), str(model_link))
        log_line(f"prepared model symlink src={model_path} dest={model_link}")
        return model_link
    except OSError:
        shutil.copy2(model_path, model_link)
        log_line(f"prepared model copy src={model_path} dest={model_link}")
        return model_link


def safe_extract_zip(zip_path: Path, dest: Path) -> int:
    dest_resolved = dest.resolve()
    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
                log_line(f"zip skipped unsafe path archive={zip_path} member={info.filename}")
                continue
            target = (dest / name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                log_line(f"zip skipped escaping path archive={zip_path} member={info.filename}")
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            extracted += 1
    return extracted


def score_extracted_model(path: Path, root: Path) -> tuple[int, int, int, str]:
    rel = path.relative_to(root)
    parts = {part.lower() for part in rel.parts}
    suffix = path.suffix.lower()
    bad = 1 if "__macosx" in parts else 0
    source_bonus = 0 if "source" in parts else 1
    type_rank = 0 if suffix == ".glb" else 1
    return (bad, type_rank, source_bonus, len(rel.parts), rel.as_posix().lower())


def find_best_extracted_model(root: Path) -> Path | None:
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".glb", ".gltf"}
        and "__MACOSX" not in {part.upper() for part in path.parts}
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: score_extracted_model(path, root))[0]


def resolve_model_input(model_path: Path, runtime_root: Path) -> Path:
    suffix = model_path.suffix.lower()
    if suffix != ".zip":
        if suffix not in {".glb", ".gltf", ".fbx"}:
            raise ValueError(f"unsupported model type: {model_path.suffix or model_path.name}")
        return model_path

    cleanup_old_runtime_dirs(runtime_root / "zip_extract", "zip extract")
    extract_root = runtime_root / "zip_extract" / runtime_bundle_name(model_path)
    extract_root.mkdir(parents=True, exist_ok=True)
    extracted = safe_extract_zip(model_path, extract_root)
    log_line(f"zip extracted archive={model_path} files={extracted} dest={extract_root}")

    nested_zips = [
        path
        for path in sorted(extract_root.rglob("*.zip"), key=lambda p: (0 if "source" in {part.lower() for part in p.parts} else 1, len(p.parts), p.name.lower()))
        if "__MACOSX" not in {part.upper() for part in path.parts}
    ]
    for index, nested_zip in enumerate(nested_zips[:5], start=1):
        nested_dest = extract_root / "_nested" / safe_runtime_name(nested_zip)
        nested_dest.mkdir(parents=True, exist_ok=True)
        nested_count = safe_extract_zip(nested_zip, nested_dest)
        log_line(f"nested zip extracted index={index} archive={nested_zip} files={nested_count} dest={nested_dest}")

    selected = find_best_extracted_model(extract_root)
    if not selected:
        raise ValueError(f"zip does not contain a .glb or .gltf model: {model_path}")
    log_line(f"zip selected model archive={model_path} selected={selected}")
    return selected


def start_server(root: Path) -> tuple[ThreadingHTTPServer, int]:
    def handler(*args, **kwargs):
        return QuietHandler(*args, directory=str(root), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open a lightweight GLB/GLTF/FBX/ZIP viewer.")
    parser.add_argument("model", nargs="?", help="Path to .glb, .gltf, .fbx, or .zip model bundle. Defaults to the bundled sample model.")
    parser.add_argument("--last", action="store_true", help="Reopen the last model launched in this viewer.")
    parser.add_argument("--width", type=int, default=1500)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--reset-position", action="store_true", help="Ignore saved viewer window geometry for this launch.")
    parser.add_argument("--monitor", choices=["right", "primary", "fast"], default="right", help="Initial monitor: right keeps the normal side-display workflow; fast chooses the highest-refresh display.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("r7321.3DViewer")
        except Exception:
            pass
    if args.last:
        last_model = resolve_last_model()
        if not last_model:
            print("no previous 3D viewer model found", file=sys.stderr)
            return 2
        model_path = last_model
    elif args.model:
        model_path = Path(args.model).expanduser().resolve()
    else:
        model_path = ROOT / "samples" / "planter_box_01" / "planter_box_01_1k.gltf"
    if not model_path.exists():
        print(f"model not found: {model_path}", file=sys.stderr)
        return 2
    save_last_model(model_path)
    missing_vendor = [p for p in (VENDOR_THREE, VENDOR_HDR_LOADER, VENDOR_FBX_LOADER) if not p.exists()]
    if missing_vendor:
        print(f"viewer vendor runtime missing: {missing_vendor[0]}", file=sys.stderr)
        return 3
    download_missing_hdris()
    mimetypes.add_type("model/gltf-binary", ".glb")
    mimetypes.add_type("model/gltf+json", ".gltf")
    mimetypes.add_type("application/octet-stream", ".fbx")
    mimetypes.add_type("application/octet-stream", ".hdr")
    # Serve the repo root so the local vendored Three.js runtime and prepared model bundle can both be reached.
    runtime_root = ROOT / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    try:
        resolved_model = resolve_model_input(model_path, runtime_root)
        served_model = prepare_served_model(resolved_model, runtime_root)
    except Exception as exc:
        log_line(f"model prepare failed model={model_path}: {exc}")
        print(str(exc), file=sys.stderr)
        return 4
    log_line(f"launch model={model_path} resolved={resolved_model} served={served_model}")
    server_root = ROOT
    server, port = start_server(server_root)
    app = QApplication(sys.argv)
    app.setApplicationName("3D Viewer")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = ViewerWindow(served_model, model_path, server_root, port)
    restored = False if args.reset_position else restore_window_state(window)
    if not restored:
        place_on_monitor(window, args.width, args.height, args.monitor)
    window.show()
    QTimer.singleShot(100, lambda: window.apply_topmost(True))
    QTimer.singleShot(120, window.raise_)
    exit_code = app.exec()
    server.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
