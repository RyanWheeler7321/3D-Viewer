# 3D Viewer

A small local desktop viewer for `.glb`, `.gltf`, `.fbx`, and zipped glTF model bundles.

It is built with Python, PySide6 QtWebEngine, and vendored Three.js. It is meant for quick asset inspection: orbit, pan, zoom, frame angles, wireframe, clay mode, lighting modes, optional HDRIs, and a frameless always-on-top window.

## Quick start

```bash
python -m pip install -r requirements.txt
python viewer.py
```

Running without an argument opens the bundled offline sample model:

```bash
python viewer.py samples/planter_box_01/planter_box_01_1k.gltf
```

The bundled sample is Poly Haven's `planter_box_01` model at 1k resolution. It is included so the repo works without internet.

## Controls

- Left mouse: orbit
- Middle mouse drag: smooth zoom
- Mouse wheel: zoom
- Right mouse: pan
- `F`: frame model, cycling through angles
- `A`: auto-rotate
- `W`: wireframe
- `X`: wireframe x-ray
- `C`: clay material
- `H`: HDRI slot
- `Ctrl+B`: show/hide HDRI as the background
- `L`: lighting mode
- `B`: flat background color
- `D`: diagnostics overlay
- `Q`: quality mode
- `T`: always-on-top
- `P`: pause WebGL
- `I`: write a process snapshot to the log
- `Ctrl+O`: open another model in the same viewer window
- `O`: open the model folder
- `Esc`: close

## Optional HDRIs

HDRIs are not committed to keep the repo small. The viewer still works offline without them by falling back to flat background and direct lights.

To download optional 2k Poly Haven HDRIs:

```bash
python scripts/download_hdris.py
```

## Explorer context menu on Windows

Install the right-click entry for `.glb`, `.gltf`, `.fbx`, and `.zip` files:

```bash
python scripts/install_context_menu.py
```

Remove it:

```bash
python scripts/install_context_menu.py --remove
```

## Notes

- glTF sidecar buffers and textures are copied into `runtime/models/` per launch so repeated opens do not collide.
- ZIP inputs are extracted under `runtime/zip_extract/`; nested Sketchfab-style source ZIPs are supported.
- Runtime files, logs, downloaded HDRIs, and copied model bundles are ignored by git.
