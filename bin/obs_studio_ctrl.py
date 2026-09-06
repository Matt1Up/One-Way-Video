#!/usr/bin/env python3
# obs_studio_ctrl.py — universal OBS WebSocket controller (ReqClient, v5.x)
# Requires: pip install obsws-python
#
# Usage examples:
#   python3 obs_studio_ctrl.py ping
#   python3 obs_studio_ctrl.py start
#   python3 obs_studio_ctrl.py stop
#   python3 obs_studio_ctrl.py toggle
#   python3 obs_studio_ctrl.py rec-status
#   python3 obs_studio_ctrl.py scenes
#   python3 obs_studio_ctrl.py scene "Camera Only"
#   python3 obs_studio_ctrl.py inputs
#   python3 obs_studio_ctrl.py mute "Mic/Aux"
#   python3 obs_studio_ctrl.py unmute "Mic/Aux"
#   python3 obs_studio_ctrl.py vol "Mic/Aux" 0.8
#   python3 obs_studio_ctrl.py GetStreamStatus
#   python3 obs_studio_ctrl.py SetCurrentProgramScene '{"sceneName":"Camera Only"}'
#
# Connection (precedence: CLI > env > defaults):
#   CLI flags: --host 127.0.0.1 --port 4455 --password "" --timeout 5
#   Env vars:
#     OBS_HOST=127.0.0.1
#     OBS_PORT=4455
#     OBS_PASSWORD=""
#     OBS_INPUT="Mic/Aux"         # default input for mute/unmute/vol
#     OBS_SCENE="Camera Only"     # default scene for 'scene' alias

import os
import sys
import json
import argparse
from dataclasses import is_dataclass, asdict

try:
    from obsws_python import ReqClient
    from obsws_python.error import OBSSDKRequestError
except Exception:
    print("This tool requires 'obsws-python' (pip install obsws-python).", file=sys.stderr)
    raise

USAGE = """\
Examples:
  python3 obs_studio_ctrl.py ping
  python3 obs_studio_ctrl.py start
  python3 obs_studio_ctrl.py stop
  python3 obs_studio_ctrl.py toggle
  python3 obs_studio_ctrl.py rec-status
  python3 obs_studio_ctrl.py scenes
  python3 obs_studio_ctrl.py scene "Camera Only"
  python3 obs_studio_ctrl.py inputs
  python3 obs_studio_ctrl.py mute "Mic/Aux"
  python3 obs_studio_ctrl.py unmute "Mic/Aux"
  python3 obs_studio_ctrl.py vol "Mic/Aux" 0.8
  python3 obs_studio_ctrl.py GetStreamStatus
  python3 obs_studio_ctrl.py SetCurrentProgramScene '{"sceneName":"Camera Only"}'

Aliases:
  ping                         -> GetVersion (no side effects)
  start                        -> StartRecord
  stop                         -> StopRecord
  toggle                       -> ToggleRecord
  rec-status                   -> GetRecordStatus
  stream-start                 -> StartStream
  stream-stop                  -> StopStream
  stream-toggle                -> ToggleStream
  stats                        -> GetStats
  scenes                       -> GetSceneList
  scene [name]                 -> SetCurrentProgramScene
  inputs                       -> GetInputList
  mute [inputName]             -> SetInputMute true
  unmute [inputName]           -> SetInputMute false
  vol [inputName] [0.0-1.0]    -> SetInputVolume (mul)
  vcam-start                   -> StartVirtualCam
  vcam-stop                    -> StopVirtualCam
"""

def pretty(obj) -> str:
    """Best-effort pretty printer for obsws dataclasses / dicts."""
    try:
        if is_dataclass(obj):
            return json.dumps(asdict(obj), indent=2, default=str)
        if hasattr(obj, "__dict__"):
            return json.dumps(obj.__dict__, indent=2, default=str)
        if isinstance(obj, (dict, list, tuple)):
            return json.dumps(obj, indent=2, default=str)
        return str(obj)
    except Exception:
        return repr(obj)

def parse_json_arg(s: str) -> dict:
    try:
        return json.loads(s) if s is not None else {}
    except json.JSONDecodeError as e:
        print(f"Invalid JSON args: {e}")
        sys.exit(2)

def main():
    # ----- CLI -----
    ap = argparse.ArgumentParser(description="OBS WebSocket controller (v5.x)", formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", help="Alias or OBS request name (see below).")
    ap.add_argument("arg2", nargs="?", help="Optional arg or JSON for raw requests.")
    ap.add_argument("arg3", nargs="?", help="Optional second arg for some aliases (e.g., vol).")
    ap.add_argument("--host", default=os.getenv("OBS_HOST", "127.0.0.1"), help="OBS host (default: env OBS_HOST or 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(os.getenv("OBS_PORT", "4455")), help="OBS port (default: env OBS_PORT or 4455)")
    ap.add_argument("--password", default=os.getenv("OBS_PASSWORD", ""), help='OBS password (default: env OBS_PASSWORD or "")')
    ap.add_argument("--timeout", type=float, default=float(os.getenv("OBS_TIMEOUT", "5")), help="Request timeout seconds (default: 5)")
    ap.add_argument("--help-commands", action="store_true", help="Show alias help and exit.")
    args = ap.parse_args()

    if args.help_commands or args.command in {"-h", "--help", "help"}:
        print(USAGE)
        ap.exit(0)

    command = args.command
    arg2 = args.arg2
    arg3 = args.arg3

    # Defaults for convenient aliases (env-overridable)
    default_input = os.getenv("OBS_INPUT", "Mic/Aux")
    default_scene = os.getenv("OBS_SCENE", "Camera Only")

    try:
        ws = ReqClient(host=args.host, port=args.port, password=args.password, timeout=args.timeout)

        # ---- Aliases (simple one-liners) ----
        simple_aliases = {
            "ping": ("GetVersion", {}),
            "start": ("StartRecord", {}),
            "stop": ("StopRecord", {}),
            "toggle": ("ToggleRecord", {}),
            "rec-status": ("GetRecordStatus", {}),
            "stream-start": ("StartStream", {}),
            "stream-stop": ("StopStream", {}),
            "stream-toggle": ("ToggleStream", {}),
            "vcam-start": ("StartVirtualCam", {}),
            "vcam-stop": ("StopVirtualCam", {}),
            "stats": ("GetStats", {}),
            "scenes": ("GetSceneList", {}),
            "inputs": ("GetInputList", {}),
        }

        if command in simple_aliases:
            req_name, params = simple_aliases[command]
            resp = ws.send(req_name, params)
            print(pretty(resp))
            return

        if command == "scene":
            scene_name = arg2 or default_scene
            if not scene_name:
                print("Error: scene alias needs a scene name (or set OBS_SCENE).")
                sys.exit(1)
            resp = ws.send("SetCurrentProgramScene", {"sceneName": scene_name})
            print(pretty(resp))
            return

        if command == "mute":
            input_name = arg2 or default_input
            resp = ws.send("SetInputMute", {"inputName": input_name, "inputMuted": True})
            print(pretty(resp))
            return

        if command == "unmute":
            input_name = arg2 or default_input
            resp = ws.send("SetInputMute", {"inputName": input_name, "inputMuted": False})
            print(pretty(resp))
            return

        if command == "vol":
            input_name = arg2 or default_input
            if arg3 is None:
                print('Error: vol alias needs a number (0.0-1.0). Example: vol "Mic/Aux" 0.75')
                sys.exit(1)
            try:
                vol = float(arg3)
            except ValueError:
                print("Error: volume must be a float like 0.8")
                sys.exit(1)
            resp = ws.send("SetInputVolume", {"inputName": input_name, "inputVolumeMul": vol})
            print(pretty(resp))
            return

        # ---- Raw request passthrough ----
        # If it's not an alias, treat it as an official OBS request name.
        params = parse_json_arg(arg2)
        resp = ws.send(command, params)
        print(pretty(resp))

    except OBSSDKRequestError as e:
        print(f"Error: {e}")
        sys.exit(3)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(4)

if __name__ == "__main__":
    main()
