#!/usr/bin/env python3
"""Install 3D Viewer Explorer context menu entries for supported model files."""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

if os.name != "nt":
    print("This installer must run with Windows Python because it writes HKCU registry keys.", file=sys.stderr)
    raise SystemExit(2)

import winreg


ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "viewer.py"
ICON = ROOT / "assets" / "rIcon.ico"
LABEL = "Open in 3D Viewer"
VERB = "r7321_3d_viewer"
SUPPORTED_EXTS = [".glb", ".gltf", ".fbx", ".zip"]


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def viewer_command() -> str:
    return f'"{pythonw_path()}" "{VIEWER}" "%1"'


def read_default_value(root, subkey: str) -> str | None:
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, None)
            return value if isinstance(value, str) and value else None
    except OSError:
        return None


def registry_targets(ext: str) -> list[str]:
    targets = [
        rf"Software\Classes\SystemFileAssociations\{ext}\shell\{VERB}",
        rf"Software\Classes\{ext}\shell\{VERB}",
    ]
    progid = read_default_value(winreg.HKEY_CLASSES_ROOT, ext)
    if progid:
        targets.append(rf"Software\Classes\{progid}\shell\{VERB}")
    return targets


def set_value(root, subkey: str, name: str | None, value: str) -> None:
    with winreg.CreateKeyEx(root, subkey, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def delete_tree(root, subkey: str) -> None:
    try:
        winreg.DeleteKey(root, subkey)
        return
    except OSError:
        pass
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                delete_tree(root, subkey + "\\" + child)
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        return


def install_ext(ext: str) -> None:
    command = viewer_command()
    icon = str(ICON if ICON.exists() else pythonw_path())
    for base in registry_targets(ext):
        set_value(winreg.HKEY_CURRENT_USER, base, None, LABEL)
        set_value(winreg.HKEY_CURRENT_USER, base, "MUIVerb", LABEL)
        set_value(winreg.HKEY_CURRENT_USER, base, "Icon", icon)
        set_value(winreg.HKEY_CURRENT_USER, base + r"\command", None, command)
    print(f"installed {ext}: {LABEL}")


def remove_ext(ext: str) -> None:
    for base in registry_targets(ext):
        delete_tree(winreg.HKEY_CURRENT_USER, base)
    print(f"removed {ext}: {LABEL}")


def notify_shell() -> None:
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install or remove 3D Viewer Explorer context menu entries.")
    parser.add_argument("--remove", action="store_true", help="Remove the context menu entries instead of installing them.")
    parser.add_argument("--ext", action="append", choices=SUPPORTED_EXTS, help="Limit to one extension. Can be repeated.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not VIEWER.exists():
        print(f"viewer script not found: {VIEWER}", file=sys.stderr)
        return 3
    exts = args.ext or SUPPORTED_EXTS
    for ext in exts:
        if args.remove:
            remove_ext(ext)
        else:
            install_ext(ext)
    notify_shell()
    print(f"command: {viewer_command()}")
    print("Windows 11 may show this under Show more options.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
