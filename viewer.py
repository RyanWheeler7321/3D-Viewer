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
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

ROOT = Path(__file__).resolve().parent
VENDOR_THREE = ROOT / "vendor" / "three" / "build" / "three.module.js"
VENDOR_HDR_LOADER = ROOT / "vendor" / "three" / "examples" / "jsm" / "loaders" / "HDRLoader.js"
LOG_PATH = ROOT / "runtime" / "logs" / "3d_viewer.log"
STATE_PATH = ROOT / "runtime" / "window_state.json"
LAST_MODEL_PATH = ROOT / "runtime" / "last_model.json"
ICON_PATH = ROOT / "assets" / "rIcon.ico"
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
    geometry_hex = str(data.get("geometry_hex") or "")
    if geometry_hex:
        try:
            return bool(window.restoreGeometry(QByteArray.fromHex(geometry_hex.encode("ascii"))))
        except Exception:
            pass
    rect = data.get("normal_rect")
    if isinstance(rect, list) and len(rect) == 4:
        try:
            x, y, w, h = [int(v) for v in rect]
            if w > 200 and h > 200:
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


def place_on_right_monitor(window: QMainWindow, width: int, height: int) -> None:
    screens = QGuiApplication.screens()
    # Prefer the rightmost display when one exists.
    screen = max(screens, key=lambda s: s.geometry().x()) if screens else QGuiApplication.primaryScreen()
    geo = screen.availableGeometry()
    x = geo.x() + max(20, (geo.width() - width) // 2)
    y = geo.y() + max(20, (geo.height() - height) // 2)
    window.setGeometry(x, y, min(width, geo.width() - 40), min(height, geo.height() - 40))


def place_on_right_monitor_physical(window: QMainWindow) -> None:
    # Public build avoids hard-coded monitor coordinates. Qt geometry restore/placement is enough.
    return


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


def build_html(model_name: str, model_url: str, model_path: Path) -> str:
    title = html.escape(model_name)
    path_text = html.escape(str(model_path))
    hdri_json = json.dumps(build_hdri_options(), ensure_ascii=False)
    model_url_json = json.dumps(model_url)
    template = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script type="importmap">
{"imports":{"three":"/vendor/three/build/three.module.js","three/addons/":"/vendor/three/examples/jsm/"}}
</script>
<style>
html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: #050608; color: #e8e8ee; font-family: Segoe UI, system-ui, sans-serif; }
#bar { position: fixed; top: 0; left: 0; right: 0; height: 48px; display: flex; align-items: center; gap: 18px; padding: 0 18px; background: rgba(8,10,14,.92); border-bottom: 1px solid rgba(227,0,140,.42); z-index: 2; -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision; }
#title { color: __ACCENT__; font-weight: 900; font-size: 21px; white-space: nowrap; letter-spacing: .1px; }
#path { opacity: .9; font-size: 16px; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#keys { margin-left: auto; opacity: .9; font-size: 14px; font-weight: 700; white-space: nowrap; }
#viewer { position: fixed; inset: 0; background: #101722; }
#status { position: fixed; left: 14px; bottom: 12px; padding: 7px 10px; background: rgba(8,10,14,.78); border: 1px solid rgba(255,255,255,.12); border-radius: 8px; color: #f0f0f4; font-size: 14px; font-weight: 700; z-index: 3; opacity: 1; transition: opacity .22s ease; text-shadow: 0 1px 2px #000; pointer-events: none; }
#modePopup { position: fixed; left: 14px; top: 56px; padding: 7px 10px; background: rgba(8,10,14,.82); border: 1px solid rgba(227,0,140,.36); border-radius: 8px; color: #fff; font-size: 14px; font-weight: 800; z-index: 4; opacity: 0; transform: translateY(-4px); transition: opacity .16s ease, transform .16s ease; text-shadow: 0 1px 2px #000; pointer-events: none; }
#modePopup.show { opacity: 1; transform: translateY(0); }
#diagnostics { position: fixed; right: 12px; bottom: 12px; min-width: 270px; padding: 9px 11px; background: rgba(4,5,7,.82); border: 1px solid rgba(227,0,140,.36); border-radius: 9px; color: #f4f4f8; font: 700 12px/1.35 Consolas, monospace; white-space: pre; z-index: 5; pointer-events: none; display: none; }
</style>
</head>
<body>
<div id="viewer"></div>
<div id="bar"><div id="title">__TITLE__</div><div id="path">__PATH__</div><div id="keys">LMB orbit | MMB zoom | RMB pan | F frame | A auto | W wire | X xray | C clay | H HDRI | L light | B bg</div></div>
<div id="status">Loading model...</div>
<div id="modePopup"></div>
<div id="diagnostics"></div>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { HDRLoader } from 'three/addons/loaders/HDRLoader.js';

const MODEL_URL = __MODEL_URL__;
const HDRI_OPTIONS = __HDRI_OPTIONS__;
const root = document.getElementById('viewer');
const status = document.getElementById('status');
const modePopup = document.getElementById('modePopup');
const diagnostics = document.getElementById('diagnostics');
let statusFadeTimer = 0;
let popupTimer = 0;
let diagnosticsVisible = false;
let renderPaused = false;
function log(text) { console.log(`[viewer] ${text}`); }
function setStatus(text, fade = false, fadeMs = 1000) {
  status.style.opacity = '1';
  status.textContent = text;
  clearTimeout(statusFadeTimer);
  if (fade) statusFadeTimer = setTimeout(() => { status.style.opacity = '0'; }, fadeMs);
}
function showModePopup(text) {
  modePopup.textContent = text;
  modePopup.classList.add('show');
  clearTimeout(popupTimer);
  popupTimer = setTimeout(() => modePopup.classList.remove('show'), 1150);
}

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, window.innerWidth / window.innerHeight, 0.01, 10000);
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, premultipliedAlpha: false, powerPreference: 'high-performance' });
renderer.setPixelRatio(1.0);
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.setClearColor(0x101722, 1);
renderer.info.autoReset = false;
root.appendChild(renderer.domElement);
const qualityModes = [
  { label: 'performance', pixelRatio: 1.0 },
  { label: 'balanced', pixelRatio: 1.25 },
  { label: 'sharp', pixelRatio: 1.5 }
];
let qualityIndex = 0;
function applyQuality(show = true) {
  const q = qualityModes[qualityIndex];
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, q.pixelRatio));
  renderer.setSize(window.innerWidth, window.innerHeight);
  markDirty();
  log(`quality=${q.label} pr=${renderer.getPixelRatio().toFixed(2)}`);
  if (show) showModePopup(`Quality ${qualityIndex + 1}/${qualityModes.length}: ${q.label}`);
}
function toggleQuality() {
  qualityIndex = (qualityIndex + 1) % qualityModes.length;
  applyQuality(true);
}

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.screenSpacePanning = true;
controls.mouseButtons = {
  LEFT: THREE.MOUSE.ROTATE,
  MIDDLE: -1,
  RIGHT: THREE.MOUSE.PAN
};
let controlsDragging = false;
let middleZooming = false;
let middleZoomPointerId = null;
let middleZoomY = 0;
function applyMiddleDragZoom(clientY) {
  const dy = clientY - middleZoomY;
  if (Math.abs(dy) < 0.25) return;
  middleZoomY = clientY;
  const offset = camera.position.clone().sub(controls.target);
  const oldDistance = Math.max(0.0001, offset.length());
  const scale = Math.exp(dy * 0.006 * controls.zoomSpeed);
  const newDistance = THREE.MathUtils.clamp(oldDistance * scale, controls.minDistance, controls.maxDistance);
  camera.position.copy(controls.target).add(offset.normalize().multiplyScalar(newDistance));
  camera.updateMatrixWorld();
  controls.update(0);
  markDirty();
}
renderer.domElement.addEventListener('pointerdown', (event) => {
  if (event.button !== 1) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  middleZooming = true;
  middleZoomPointerId = event.pointerId;
  middleZoomY = event.clientY;
  controlsDragging = true;
  renderer.domElement.setPointerCapture?.(event.pointerId);
}, { passive: false, capture: true });
renderer.domElement.addEventListener('pointermove', (event) => {
  if (!middleZooming || event.pointerId !== middleZoomPointerId) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  applyMiddleDragZoom(event.clientY);
}, { passive: false, capture: true });
function endMiddleZoom(event) {
  if (!middleZooming || event.pointerId !== middleZoomPointerId) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  middleZooming = false;
  middleZoomPointerId = null;
  controlsDragging = false;
  renderer.domElement.releasePointerCapture?.(event.pointerId);
  markDirty();
}
renderer.domElement.addEventListener('pointerup', endMiddleZoom, { passive: false, capture: true });
renderer.domElement.addEventListener('pointercancel', endMiddleZoom, { passive: false, capture: true });
renderer.domElement.addEventListener('auxclick', (event) => {
  if (event.button === 1) { event.preventDefault(); event.stopImmediatePropagation(); }
}, { passive: false, capture: true });
controls.addEventListener('start', () => { controlsDragging = true; markDirty(); });
controls.addEventListener('end', () => { controlsDragging = false; markDirty(); });
controls.zoomSpeed = 1.1;
log('mouse controls: left rotate, middle drag custom smooth zoom, wheel zoom, right pan');

const hemiLight = new THREE.HemisphereLight(0xffffff, 0x2a3140, 1.3);
scene.add(hemiLight);
const keyLight = new THREE.DirectionalLight(0xffffff, 2.15);
keyLight.position.set(2.8, 4.5, 3.5);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0xdde8ff, 0.4);
fillLight.position.set(-3, 1.5, 2);
scene.add(fillLight);
const rimLight = new THREE.DirectionalLight(0xffd6f0, 0.85);
rimLight.position.set(-4, 2, -3);
scene.add(rimLight);

let model = null;
let bounds = new THREE.Box3();
let center = new THREE.Vector3();
let size = new THREE.Vector3();
let radius = 1;
let frameIndex = 0;
let lastFrameTap = 0;
let autoRotate = true;
let autoRotatePauseUntil = 0;
let framePulse = null;
let wireMode = 0;
let wireXray = false;
let clayEnabled = false;
let modelDirty = true;
let lastRenderTime = performance.now();
let windowFocused = true;
let frameStatsWindowStart = performance.now();
let frameStatsFrames = 0;
let frameStatsMaxDt = 0;
let frameStatsSpikeCount = 0;
let frameStatsLastHidden = false;
let lastSpikeLogAt = 0;
let renderStatsTotalMs = 0;
let renderStatsMaxMs = 0;
let renderStatsFrames = 0;
let renderedFramesTotal = 0;
let frameStatsLastLogAt = performance.now();
let animationHandle = 0;
let delayedFrameTimer = 0;
const renderModeLabel = 'continuous';
const wireCache = new Map();
let wireOverlays = [];
let lightingIndex = 0;
let backgroundIndex = 0;
let hdriIndex = 0;
let hdriLoadSerial = 0;
let hdriBackgroundEnabled = false;
const hdriCache = new Map();
const hdrLoader = new HDRLoader();
const pmremGenerator = new THREE.PMREMGenerator(renderer);
pmremGenerator.compileEquirectangularShader();
const frameResetMs = 3600;
const backgroundModes = [
  { label: 'cool slate', inner: '#27303e', mid: '#101722', outer: '#05070b' },
  { label: 'light grey', inner: '#6b7078', mid: '#9aa0a8', outer: '#d3d5d8' },
  { label: 'very light grey', inner: '#9fa4aa', mid: '#d4d6d9', outer: '#f2f2f1' },
  { label: 'warm amber', inner: '#765135', mid: '#3a2418', outer: '#140c07' },
  { label: 'green teal', inner: '#347365', mid: '#1b4841', outer: '#09201d' },
  { label: 'violet dusk', inner: '#7450a0', mid: '#39214f', outer: '#16091f' }
];
const lightingModes = [
  { label: 'studio', hemi: [0xffffff, 0x2a3140, 1.3], key: [0xffffff, 2.15, 2.8, 4.5, 3.5], fill: [0xdde8ff, .4, -3, 1.5, 2], rim: [0xffd6f0, .85, -4, 2, -3], exposure: 1.05 },
  { label: 'flat inspect', hemi: [0xffffff, 0xffffff, 2.4], key: [0xffffff, .55, 0, 3, 4], fill: [0xffffff, .5, -3, 2, 2], rim: [0xffffff, .15, 0, 2, -4], exposure: .95 },
  { label: 'hard forms', hemi: [0xb8caff, 0x151923, .45], key: [0xffffff, 3.4, 4, 5, 2], fill: [0x405080, .08, -4, 1, 1], rim: [0xffffff, .4, -3, 3, -4], exposure: 1.0 },
  { label: 'rim silhouette', hemi: [0x657080, 0x07080a, .25], key: [0x7aa4ff, .75, 0, 2, 5], fill: [0x22304a, .1, 3, 1, 2], rim: [0xff2f9f, 3.1, -4, 2, -3], exposure: 1.05 },
  { label: 'warm material', hemi: [0xffe7c8, 0x2b1b12, .9], key: [0xffcf95, 2.5, 3.2, 3.5, 4], fill: [0x6aa0ff, .3, -4, 1.3, 2], rim: [0xffffff, .55, -2, 2, -4], exposure: 1.0 },
  { label: 'low dramatic', hemi: [0x8890a0, 0x060608, .18], key: [0xffffff, 2.9, -1, .6, 3.6], fill: [0x26305c, .05, 3, 1, 1], rim: [0xff2a65, 1.4, 4, 1.2, -3], exposure: 1.08 }
];
const originalMaterials = new Map();
const clayMaterial = new THREE.MeshStandardMaterial({ color: 0xb8b3aa, roughness: 0.92, metalness: 0.0 });

const frameAngles = [
  { label: 'front', yaw: 0, pitch: 72 },
  { label: 'front 3/4 right', yaw: 35, pitch: 72 },
  { label: 'front 3/4 left', yaw: -35, pitch: 72 },
  { label: 'right side', yaw: 90, pitch: 72 },
  { label: 'left side', yaw: -90, pitch: 72 },
  { label: 'back 3/4 right', yaw: 145, pitch: 72 },
  { label: 'back 3/4 left', yaw: -145, pitch: 72 },
  { label: 'back', yaw: 180, pitch: 72 },
  { label: 'high front', yaw: 25, pitch: 48 },
  { label: 'top', yaw: 0, pitch: 8 },
  { label: 'bottom', yaw: 0, pitch: 166 }
];

function meshList() {
  const out = [];
  if (!model) return out;
  model.traverse((obj) => {
    if (obj.userData?.viewerWireOverlay) return;
    if (obj.isMesh && obj.geometry?.getAttribute('position')) out.push(obj);
  });
  return out;
}
function refreshBounds() {
  bounds.setFromObject(model);
  bounds.getCenter(center);
  bounds.getSize(size);
  radius = Math.max(0.5, size.length() * 1.45);
  controls.target.copy(center);
  controls.minDistance = radius * 0.05;
  controls.maxDistance = radius * 9;
  camera.near = Math.max(0.001, radius / 500);
  camera.far = radius * 80;
  camera.updateProjectionMatrix();
  log(`bounds size=${size.x.toFixed(3)},${size.y.toFixed(3)},${size.z.toFixed(3)} radius=${radius.toFixed(3)}`);
}
function positionForFrame(shot) {
  const theta = THREE.MathUtils.degToRad(shot.yaw);
  const phi = THREE.MathUtils.degToRad(shot.pitch);
  const distance = radius;
  return new THREE.Vector3(center.x + distance * Math.sin(phi) * Math.sin(theta), center.y + distance * Math.cos(phi), center.z + distance * Math.sin(phi) * Math.cos(theta));
}
function frameModel(manual = true) {
  if (!model) return;
  const now = performance.now();
  if (!manual || (now - lastFrameTap) > frameResetMs) frameIndex = 0;
  else frameIndex = (frameIndex + 1) % frameAngles.length;
  lastFrameTap = now;
  const shot = frameAngles[frameIndex];
  camera.position.copy(positionForFrame(shot));
  controls.target.copy(center);
  controls.update();
  if (manual) {
    autoRotatePauseUntil = now + 2000;
    startFramePulse();
  }
  markDirty();
  setStatus(`Framed: ${shot.label}.`, true);
}

function startFramePulse() {
  framePulse = {
    start: performance.now(),
    duration: 170,
    baseFov: camera.fov,
    amount: 1.25
  };
  markDirty();
}
function updateFramePulse(now) {
  if (!framePulse) return false;
  const t = Math.min(1, (now - framePulse.start) / framePulse.duration);
  const easeOut = 1 - Math.pow(1 - t, 3.2);
  camera.fov = framePulse.baseFov + framePulse.amount * (1 - easeOut);
  camera.updateProjectionMatrix();
  if (t >= 1) {
    camera.fov = framePulse.baseFov;
    camera.updateProjectionMatrix();
    framePulse = null;
  }
  return true;
}
function resetCamera() { frameIndex = 0; lastFrameTap = 0; frameModel(false); }
function toggleAutoRotate() { autoRotate = !autoRotate; markDirty(); setStatus(`Auto-rotate ${autoRotate ? 'on' : 'off'}.`, true); log(`autoRotate=${autoRotate}`); }
function scheduleFrame() {
  if (renderPaused || animationHandle) return;
  animationHandle = requestAnimationFrame(animate);
}
function scheduleDelayedFrame(ms) {
  if (renderPaused) return;
  const delay = Math.max(0, Math.min(2200, ms));
  if (delayedFrameTimer) clearTimeout(delayedFrameTimer);
  delayedFrameTimer = setTimeout(() => { delayedFrameTimer = 0; scheduleFrame(); }, delay);
}
function markDirty() { modelDirty = true; scheduleFrame(); }
function rendererMemoryText() {
  const mem = renderer.info.memory;
  const prog = renderer.info.programs ? renderer.info.programs.length : 0;
  const heap = performance.memory ? ` heap=${Math.round(performance.memory.usedJSHeapSize / 1048576)}MB` : '';
  return `geom=${mem.geometries} tex=${mem.textures} prog=${prog}${heap}`;
}
function normalizeRotationY(obj) {
  const tau = Math.PI * 2;
  if (!obj || Math.abs(obj.rotation.y) < tau) return;
  obj.rotation.y = ((obj.rotation.y % tau) + tau) % tau;
}
function disposeWire() {
  for (const overlay of wireOverlays) {
    overlay.parent?.remove?.(overlay);
    overlay.geometry?.dispose?.();
    overlay.material?.dispose?.();
  }
  wireCache.clear();
  wireOverlays = [];
}
function makeWireMaterial() {
  const base = { color: clayEnabled ? 0x050505 : 0xff174f, depthTest: !wireXray, depthWrite: false, transparent: false, opacity: 1.0, toneMapped: false };
  return new THREE.MeshBasicMaterial({ ...base, wireframe: true, side: THREE.DoubleSide });
}
function cloneGeometryForWire(mesh) {
  const geometry = mesh.geometry.clone();
  if (!geometry.getAttribute('normal')) geometry.computeVertexNormals();
  const pos = geometry.getAttribute('position');
  const normal = geometry.getAttribute('normal');
  if (pos && normal && pos.count === normal.count) {
    const epsilon = THREE.MathUtils.clamp(radius * 0.000045, 0.000015, 0.00026);
    for (let i = 0; i < pos.count; i++) {
      pos.setXYZ(i, pos.getX(i) + normal.getX(i) * epsilon, pos.getY(i) + normal.getY(i) * epsilon, pos.getZ(i) + normal.getZ(i) * epsilon);
    }
    pos.needsUpdate = true;
  }
  geometry.computeBoundingSphere();
  return geometry;
}
function getWireEntry(mesh) {
  let entry = wireCache.get(mesh.uuid);
  if (!entry) { entry = { mesh, overlay: null }; wireCache.set(mesh.uuid, entry); }
  return entry;
}
function getWireOverlay(mesh) {
  const entry = getWireEntry(mesh);
  if (entry.overlay) return entry.overlay;
  const overlay = new THREE.Mesh(cloneGeometryForWire(mesh), makeWireMaterial());
  overlay.userData.viewerWireOverlay = true;
  overlay.name = 'viewer-wire-overlay';
  overlay.matrixAutoUpdate = true;
  overlay.position.set(0, 0, 0);
  overlay.rotation.set(0, 0, 0);
  overlay.scale.set(1, 1, 1);
  overlay.frustumCulled = false;
  overlay.visible = false;
  mesh.add(overlay);
  entry.overlay = overlay;
  wireOverlays.push(overlay);
  return overlay;
}
function applyWireAppearance() {
  const color = clayEnabled ? 0x050505 : 0xff174f;
  for (const overlay of wireOverlays) {
    overlay.visible = false;
    overlay.renderOrder = wireXray ? 999 : 30;
    overlay.material.color.setHex(color);
    overlay.material.depthTest = !wireXray;
    overlay.material.needsUpdate = true;
  }
}
function rebuildWire() {
  if (!model) return;
  let count = 0;
  if (wireMode === 0) {
    disposeWire();
    markDirty();
    log(`wire hidden mode=0 meshes=0 cached=0 xray=${wireXray} clay=${clayEnabled}`);
    return;
  }
  applyWireAppearance();
  for (const mesh of meshList()) {
    const overlay = getWireOverlay(mesh);
    overlay.visible = true;
    overlay.renderOrder = wireXray ? 999 : 30;
    overlay.material.color.setHex(clayEnabled ? 0x050505 : 0xff174f);
    overlay.material.depthTest = !wireXray;
    overlay.material.needsUpdate = true;
    count += 1;
  }
  markDirty();
  log(`wire shown mode=${wireMode} meshes=${count} cached=${wireOverlays.length} xray=${wireXray} clay=${clayEnabled}`);
}
function applyClay() {
  for (const mesh of meshList()) {
    if (!originalMaterials.has(mesh.uuid)) originalMaterials.set(mesh.uuid, mesh.material);
    mesh.material = clayEnabled ? clayMaterial : originalMaterials.get(mesh.uuid);
  }
  rebuildWire();
  markDirty();
}
function toggleWire() { wireMode = wireMode === 0 ? 1 : 0; rebuildWire(); const label = wireMode === 0 ? 'off' : 'tri surface'; setStatus(`Wireframe ${label}.`, true); markDirty(); }
function toggleWireXray() { wireXray = !wireXray; rebuildWire(); setStatus(`Wireframe X-ray ${wireXray ? 'on' : 'off'}.`, true); markDirty(); }
function toggleClay() { clayEnabled = !clayEnabled; applyClay(); setStatus(`Clay ${clayEnabled ? 'on' : 'off'}.`, true); log(`clay=${clayEnabled}`); }
function applyLighting() {
  const m = lightingModes[lightingIndex];
  hemiLight.color.setHex(m.hemi[0]); hemiLight.groundColor.setHex(m.hemi[1]); hemiLight.intensity = m.hemi[2];
  keyLight.color.setHex(m.key[0]); keyLight.intensity = m.key[1]; keyLight.position.set(m.key[2], m.key[3], m.key[4]);
  fillLight.color.setHex(m.fill[0]); fillLight.intensity = m.fill[1]; fillLight.position.set(m.fill[2], m.fill[3], m.fill[4]);
  rimLight.color.setHex(m.rim[0]); rimLight.intensity = m.rim[1]; rimLight.position.set(m.rim[2], m.rim[3], m.rim[4]);
  renderer.toneMappingExposure = m.exposure;
  markDirty();
  log(`lighting=${m.label}`);
}
function toggleLighting() {
  lightingIndex = (lightingIndex + 1) % lightingModes.length;
  applyLighting();
  showModePopup(`Light ${lightingIndex + 1}/${lightingModes.length}: ${lightingModes[lightingIndex].label}`);
}
function applyBackground() {
  const b = backgroundModes[backgroundIndex];
  root.style.background = b.mid;
  renderer.setClearColor(new THREE.Color(b.mid), 1);
  if (!hdriBackgroundEnabled) scene.background = new THREE.Color(b.mid);
  markDirty();
  log(`background=${b.label}`);
}
function toggleBackground() {
  backgroundIndex = (backgroundIndex + 1) % backgroundModes.length;
  applyBackground();
  showModePopup(`BG ${backgroundIndex + 1}/${backgroundModes.length}: ${backgroundModes[backgroundIndex].label}`);
}
async function applyHdri(show = true) {
  const serial = ++hdriLoadSerial;
  const opt = HDRI_OPTIONS[hdriIndex] || HDRI_OPTIONS[0];
  if (!opt.url) {
    scene.environment = null;
    const b = backgroundModes[backgroundIndex];
    scene.background = new THREE.Color(b.mid);
    markDirty();
    log(`hdri=${opt.label} none bg=${hdriBackgroundEnabled ? 'wanted' : 'off'}`);
    if (show) showModePopup(`HDRI ${hdriIndex + 1}/${HDRI_OPTIONS.length}: ${opt.label}`);
    return;
  }
  if (show) showModePopup(`HDRI ${hdriIndex + 1}/${HDRI_OPTIONS.length}: loading ${opt.label}`);
  const started = performance.now();
  try {
    if (!hdriCache.has(opt.url)) {
      const texture = await hdrLoader.loadAsync(opt.url);
      if (serial !== hdriLoadSerial) { texture.dispose(); return; }
      texture.mapping = THREE.EquirectangularReflectionMapping;
      const envMap = pmremGenerator.fromEquirectangular(texture).texture;
      envMap.userData = { label: opt.label, fixedWorld: true };
      texture.dispose();
      hdriCache.set(opt.url, envMap);
      log(`hdri loaded ${opt.label} ms=${Math.round(performance.now() - started)} fixedWorld=true`);
    } else {
      log(`hdri cached ${opt.label}`);
    }
    const envMap = hdriCache.get(opt.url);
    scene.environment = envMap;
    if (hdriBackgroundEnabled) scene.background = envMap;
    else {
      const b = backgroundModes[backgroundIndex];
      scene.background = new THREE.Color(b.mid);
    }
    markDirty();
    if (show) showModePopup(`HDRI ${hdriIndex + 1}/${HDRI_OPTIONS.length}: ${opt.label}`);
  } catch (err) {
    console.error(err);
    log(`hdri failed ${opt.label}: ${err}`);
    if (show) showModePopup(`HDRI failed: ${opt.label}`);
  }
}
function toggleHdri() {
  hdriIndex = (hdriIndex + 1) % HDRI_OPTIONS.length;
  applyHdri(true);
}
function toggleHdriBackground() {
  hdriBackgroundEnabled = !hdriBackgroundEnabled;
  const opt = HDRI_OPTIONS[hdriIndex] || HDRI_OPTIONS[0];
  if (!hdriBackgroundEnabled) {
    const b = backgroundModes[backgroundIndex];
    scene.background = new THREE.Color(b.mid);
    markDirty();
    log('hdri background=flat');
    showModePopup('Background: flat');
    return;
  }
  if (!opt.url) {
    const b = backgroundModes[backgroundIndex];
    scene.background = new THREE.Color(b.mid);
    markDirty();
    log('hdri background requested but active hdri is none');
    showModePopup('Background: no active HDRI');
    return;
  }
  if (hdriCache.has(opt.url)) {
    scene.background = hdriCache.get(opt.url);
    markDirty();
    log(`hdri background=${opt.label}`);
    showModePopup(`Background: ${opt.label} HDRI`);
    return;
  }
  showModePopup(`Background: loading ${opt.label}`);
  applyHdri(false).then(() => {
    const ready = hdriBackgroundEnabled && hdriCache.has(opt.url);
    if (ready) {
      scene.background = hdriCache.get(opt.url);
      markDirty();
      showModePopup(`Background: ${opt.label} HDRI`);
    }
  });
}
applyLighting();
applyBackground();
applyHdri(false);

new GLTFLoader().load(MODEL_URL, (gltf) => {
  model = gltf.scene;
  scene.add(model);
  refreshBounds();
  frameModel(false);
  const meshes = meshList();
  let materialCount = 0;
  let textureCount = 0;
  const seenTextures = new Set();
  for (const mesh of meshes) {
    const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    materialCount += mats.filter(Boolean).length;
    for (const mat of mats) {
      if (!mat) continue;
      for (const value of Object.values(mat)) {
        if (value?.isTexture && !seenTextures.has(value.uuid)) { seenTextures.add(value.uuid); textureCount += 1; }
      }
    }
  }
  log(`model loaded meshes=${meshes.length} materials=${materialCount} textures=${textureCount} ${rendererMemoryText()}`);
  setStatus('Loaded. Orbit LMB, smooth zoom MMB drag/wheel, pan RMB.', true, 2400);
}, undefined, (err) => {
  console.error(err);
  log(`model load failed: ${err}`);
  setStatus('Model failed to load.');
});

function animate(now = performance.now()) {
  animationHandle = 0;
  const rawDt = Math.max(0.0, now - lastRenderTime);
  const dt = Math.min(0.05, Math.max(0.0, rawDt / 1000));
  lastRenderTime = now;
  frameStatsFrames += 1;
  frameStatsMaxDt = Math.max(frameStatsMaxDt, rawDt);
  const isSpike = rawDt > 35 && !document.hidden;
  if (isSpike) {
    frameStatsSpikeCount += 1;
    if ((now - lastSpikeLogAt) > 900) {
      lastSpikeLogAt = now;
      const info = renderer.info.render;
      log(`frame spike dt_ms=${Math.round(rawDt)} focused=${windowFocused} hidden=${document.hidden} wire=${wireMode} calls=${info.calls} tris=${info.triangles} pr=${renderer.getPixelRatio().toFixed(2)} dpr=${(window.devicePixelRatio || 1).toFixed(2)} mode=${renderModeLabel}`);
    }
  }
  if (frameStatsLastHidden !== document.hidden || (now - frameStatsLastLogAt) > 10000) {
    const elapsed = Math.max(1, now - frameStatsWindowStart);
    const logElapsed = Math.max(1, now - frameStatsLastLogAt);
    const info = renderer.info.render;
    const avgRenderMs = renderStatsFrames ? (renderStatsTotalMs / renderStatsFrames) : 0;
    log(`frame stats fps=${Math.round(frameStatsFrames * 1000 / logElapsed)} max_dt_ms=${Math.round(frameStatsMaxDt)} spikes=${frameStatsSpikeCount} render_ms_avg=${avgRenderMs.toFixed(2)} render_ms_max=${renderStatsMaxMs.toFixed(2)} focused=${windowFocused} hidden=${document.hidden} wire=${wireMode} calls=${info.calls} tris=${info.triangles} pr=${renderer.getPixelRatio().toFixed(2)} mode=${renderModeLabel} elapsed_s=${Math.round(elapsed / 1000)} frames=${renderedFramesTotal} ${rendererMemoryText()}`);
    frameStatsLastLogAt = now;
    frameStatsFrames = 0;
    frameStatsMaxDt = 0;
    frameStatsSpikeCount = 0;
    renderStatsTotalMs = 0;
    renderStatsMaxMs = 0;
    renderStatsFrames = 0;
    frameStatsLastHidden = document.hidden;
  }
  const pulseChanged = renderPaused ? false : updateFramePulse(now);
  const rotating = !renderPaused && autoRotate && model && !document.hidden && !controlsDragging && now >= autoRotatePauseUntil;
  if (rotating) { model.rotation.y = (model.rotation.y + dt * 0.42) % (Math.PI * 2); markDirty(); }
  const controlsChanged = renderPaused ? false : controls.update(dt);
  const shouldRender = !renderPaused && (modelDirty || controlsChanged || rotating || pulseChanged);
  if (shouldRender) {
    normalizeRotationY(model);
    renderer.info.reset();
    const renderStarted = performance.now();
    renderer.render(scene, camera);
    const renderMs = performance.now() - renderStarted;
    renderStatsTotalMs += renderMs;
    renderStatsMaxMs = Math.max(renderStatsMaxMs, renderMs);
    renderStatsFrames += 1;
    renderedFramesTotal += 1;
    modelDirty = false;
  }
  if (diagnosticsVisible) {
    const info = renderer.info.render;
    const recentFps = Math.round(frameStatsFrames * 1000 / Math.max(250, now - frameStatsLastLogAt));
    diagnostics.textContent =
      `fps ${recentFps}  maxdt ${Math.round(frameStatsMaxDt)}ms  spikes ${frameStatsSpikeCount}
` +
      `focused ${windowFocused}  hidden ${document.hidden}  wire ${wireMode}  auto ${autoRotate}  paused ${renderPaused}
` +
      `mode ${renderModeLabel}  dirty ${modelDirty}  calls ${info.calls}  tris ${info.triangles}  lines ${info.lines}
` +
      `render avg ${renderStatsFrames ? (renderStatsTotalMs / renderStatsFrames).toFixed(2) : '0.00'}ms max ${renderStatsMaxMs.toFixed(2)}ms  frames ${renderedFramesTotal}
` +
      `${rendererMemoryText()}  pr ${renderer.getPixelRatio().toFixed(2)}  quality ${qualityModes[qualityIndex].label}  dpr ${(window.devicePixelRatio || 1).toFixed(2)}  size ${window.innerWidth}x${window.innerHeight}`;
  }
  const waitingForAutoRotate = autoRotate && model && !document.hidden && !controlsDragging && now < autoRotatePauseUntil;
  const wantsContinuous = !renderPaused && (
    rotating || controlsDragging || middleZooming || framePulse || diagnosticsVisible || controlsChanged
  );
  if (wantsContinuous) scheduleFrame();
  else if (waitingForAutoRotate) scheduleDelayedFrame(autoRotatePauseUntil - now);
}
scheduleFrame();
window.addEventListener('focus', () => { windowFocused = true; });
window.addEventListener('blur', () => { windowFocused = false; });
document.addEventListener('visibilitychange', () => { markDirty(); log(`visibility hidden=${document.hidden}`); });
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  markDirty();
});
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'b' || e.key === 'B')) { e.preventDefault(); toggleHdriBackground(); return; }
  if (e.key === 'r' || e.key === 'R') resetCamera();
  if (e.key === 'f' || e.key === 'F') frameModel(true);
  if (e.key === 'a' || e.key === 'A') toggleAutoRotate();
  if (e.key === 'w' || e.key === 'W') toggleWire();
  if (e.key === 'x' || e.key === 'X') toggleWireXray();
  if (e.key === 'c' || e.key === 'C') toggleClay();
  if (e.key === 'l' || e.key === 'L') toggleLighting();
  if (e.key === 'h' || e.key === 'H') toggleHdri();
  if (e.key === 'b' || e.key === 'B') toggleBackground();
  if (e.key === 'd' || e.key === 'D') { diagnosticsVisible = !diagnosticsVisible; diagnostics.style.display = diagnosticsVisible ? 'block' : 'none'; markDirty(); log(`diagnostics=${diagnosticsVisible}`); }
  if (e.key === 'q' || e.key === 'Q') toggleQuality();
  if (e.key === 'p' || e.key === 'P') { renderPaused = !renderPaused; if (!renderPaused) markDirty(); log(`renderPaused=${renderPaused}`); showModePopup(`WebGL ${renderPaused ? 'paused' : 'running'}`); }
});
window.viewerResetCamera = resetCamera;
window.viewerFrameModel = () => frameModel(true);
window.viewerToggleAutoRotate = toggleAutoRotate;
window.viewerToggleWire = toggleWire;
window.viewerToggleWireXray = toggleWireXray;
window.viewerToggleClay = toggleClay;
window.viewerToggleLighting = toggleLighting;
window.viewerToggleHdri = toggleHdri;
window.viewerToggleHdriBackground = toggleHdriBackground;
window.viewerToggleBackground = toggleBackground;
window.viewerToggleDiagnostics = () => { diagnosticsVisible = !diagnosticsVisible; diagnostics.style.display = diagnosticsVisible ? 'block' : 'none'; markDirty(); log(`diagnostics=${diagnosticsVisible}`); };
window.viewerToggleQuality = toggleQuality;
window.viewerToggleRenderPause = () => { renderPaused = !renderPaused; if (!renderPaused) markDirty(); log(`renderPaused=${renderPaused}`); showModePopup(`WebGL ${renderPaused ? 'paused' : 'running'}`); };
</script>
</body>
</html>'''
    return (
        template.replace("__ACCENT__", ACCENT)
        .replace("__TITLE__", title)
        .replace("__PATH__", path_text)
        .replace("__MODEL_URL__", model_url_json)
        .replace("__HDRI_OPTIONS__", hdri_json)
    )



class ViewerWindow(QMainWindow):
    def __init__(self, model_path: Path, display_path: Path, server_root: Path, port: int) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.model_path = display_path
        self.served_model_path = model_path
        self._diag_last_wall = time.perf_counter()
        self._diag_last_cpu = time.process_time()
        self._diag_process_snapshot_index = 0
        self._topmost_enabled = True
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
        model_rel = model_path.absolute().relative_to(server_root.resolve()).as_posix()
        model_url = "/" + quote(model_rel)
        html_path = server_root / "runtime" / "viewer.html"
        html_path.write_text(build_html(display_path.name, model_url, display_path), encoding="utf-8")
        self.view.load(QUrl(f"http://127.0.0.1:{port}/runtime/viewer.html"))
        self._add_shortcuts()

    def _log_qt_diagnostics(self) -> None:
        now = time.perf_counter()
        cpu = time.process_time()
        wall_delta = max(0.001, now - self._diag_last_wall)
        cpu_pct = max(0.0, (cpu - self._diag_last_cpu) / wall_delta * 100.0)
        self._diag_last_wall = now
        self._diag_last_cpu = cpu
        rect = self.geometry()
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
            f"rect={rect.x()},{rect.y()},{rect.width()}x{rect.height()}"
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
            ("Reset Camera", "R", lambda: self.view.page().runJavaScript("window.viewerResetCamera && window.viewerResetCamera();")),
            ("Frame Model", "F", lambda: self.view.page().runJavaScript("window.viewerFrameModel && window.viewerFrameModel();")),
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
        if suffix not in {".glb", ".gltf"}:
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
    handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(root), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open a lightweight GLB/GLTF/ZIP viewer.")
    parser.add_argument("model", nargs="?", help="Path to .glb, .gltf, or .zip model bundle. Defaults to the bundled sample model.")
    parser.add_argument("--last", action="store_true", help="Reopen the last model launched in this viewer.")
    parser.add_argument("--width", type=int, default=1500)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--reset-position", action="store_true", help="Ignore saved viewer window geometry for this launch.")
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
    missing_vendor = [p for p in (VENDOR_THREE, VENDOR_HDR_LOADER) if not p.exists()]
    if missing_vendor:
        print(f"viewer vendor runtime missing: {missing_vendor[0]}", file=sys.stderr)
        return 3
    mimetypes.add_type("model/gltf-binary", ".glb")
    mimetypes.add_type("model/gltf+json", ".gltf")
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
        place_on_right_monitor(window, args.width, args.height)
    window.show()
    if not restored:
        QTimer.singleShot(60, lambda: place_on_right_monitor_physical(window))
    QTimer.singleShot(100, lambda: window.apply_topmost(True))
    QTimer.singleShot(120, window.raise_)
    exit_code = app.exec()
    server.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
