# 3D Viewer

![3D Viewer screenshot grid](media/screenshots/3d_viewer_github_main.png)

I made this quickly because I wanted a faster way to inspect 3D assets without opening Blender, Unity, or another heavier tool every time. It is meant for quick checks: open a model, move around it, test lighting, look at the texture and material read, check topology, and decide what to do with it next.

It opens `.glb`, `.gltf`, `.fbx`, and `.zip` files, including zipped glTF/GLB bundles. It also includes a small bundled sample model so the repo works immediately after install. Optional Poly Haven HDRIs can be downloaded by the app when internet is available, but the viewer still works without them.

It runs as a small Python/PySide6 desktop app with an embedded QtWebEngine view for the local Three.js viewer. In my own setup, I have it hooked into AutoHotkey and some 3D model development pipelines so generated or downloaded assets can be checked quickly before moving to the next step.

## Quick start

```bash
python -m pip install -r requirements.txt
python viewer.py
```

Running with no model opens the bundled sample:

```bash
python viewer.py samples/planter_box_01/planter_box_01_1k.gltf
```

## Controls

- Left mouse: orbit
- Middle mouse drag: smooth zoom
- Mouse wheel: zoom
- Right mouse: pan
- `F`: frame model and cycle through saved angles
- `A`: auto-rotate
- `W`: wireframe
- `X`: wireframe x-ray
- `C`: clay material
- `H`: HDRI slot
- `Ctrl+B`: show or hide HDRI background
- `L`: lighting mode
- `B`: flat background color
- `D`: diagnostics overlay
- `Q`: quality mode
- `T`: always-on-top
- `P`: pause WebGL
- `I`: write a process snapshot to the log
- `Ctrl+O`: open another model in the same window
- `O`: open the model folder
- `Esc`: close

## Windows right-click menu

Install the Explorer entry for `.glb`, `.gltf`, `.fbx`, and `.zip` files:

```bash
python scripts/install_context_menu.py
```

Remove it:

```bash
python scripts/install_context_menu.py --remove
```
