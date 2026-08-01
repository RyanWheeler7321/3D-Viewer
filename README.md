![3D Viewer screenshot grid](media/screenshots/3d_viewer_github_main.png)

# 3D Viewer

I made this quickly because I wanted a faster way to inspect 3D assets without opening Blender, Unity, or another heavier tool every time. It is meant for quick checks: open a model, move around it, test lighting, look at the texture and material read, check topology, and decide what to do with it next.

It opens `.glb`, `.gltf`, `.fbx`, and `.zip` files, including zipped glTF/GLB bundles. Animated models can be played in place, bone-only animation files get a simple skeleton preview, and a small sidecar file can group model variants for quick comparison. The repo also includes a sample model and works without the optional Poly Haven HDRIs.

It runs as a small Python/PySide6 desktop app with an embedded QtWebEngine view for the local Three.js viewer.

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
- `Space`: play or pause embedded animation clips
- `[` / `]`: previous or next animation clip
- `0`: restart current animation clip
- `R`: toggle root-motion compensation for in-place preview
- `M`: swap avatar/model variants when a `.viewer_variants.json` sidecar exists, without changing camera/root/playback settings
- `+` / `-`: animation speed
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
