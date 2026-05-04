#!/usr/bin/env python3
"""ADB coordinate helper for GoPay linked-app unlinking.

This script automates only normal Android UI input through ADB. It does not
call private GoPay APIs and intentionally pauses before destructive unlink
actions so the operator can verify the screen.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(__file__).resolve().with_name("gopay_adb_coords.example.json")


class ADBError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def apply_coord_overrides(config: dict[str, Any], overrides: list[str]) -> None:
    if not overrides:
        return
    coords = config.setdefault("coords", {})
    for item in overrides:
        try:
            name, raw_ratio = item.split("=", 1)
            rx_raw, ry_raw = raw_ratio.split(",", 1)
            rx = float(rx_raw)
            ry = float(ry_raw)
        except ValueError as exc:
            raise ADBError(f"Invalid --set-ratio value '{item}'. Use name=rx,ry") from exc
        coords[name] = {
            "ratio": [rx, ry],
            "description": "Runtime override from --set-ratio",
        }


def resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return ROOT / p


class ADBSession:
    def __init__(self, adb_path: str = "adb", device: str = ""):
        self.adb_path = adb_path
        self.device = device.strip()
        self._screen_size: tuple[int, int] | None = None

    def cmd(self, *args: str, text: bool = True, check: bool = True) -> subprocess.CompletedProcess:
        command = [self.adb_path]
        if self.device:
            command.extend(["-s", self.device])
        command.extend(args)
        try:
            return subprocess.run(
                command,
                check=check,
                capture_output=True,
                text=text,
            )
        except FileNotFoundError as exc:
            raise ADBError(f"ADB not found: {self.adb_path}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            detail = (stderr or stdout or str(exc)).strip()
            raise ADBError(f"ADB command failed: {' '.join(command)}\n{detail}") from exc

    def list_devices(self) -> str:
        try:
            result = subprocess.run(
                [self.adb_path, "devices", "-l"],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ADBError(f"ADB not found: {self.adb_path}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ADBError(f"ADB command failed: {self.adb_path} devices -l\n{detail}") from exc
        return result.stdout.strip()

    def screen_size(self) -> tuple[int, int]:
        if self._screen_size:
            return self._screen_size
        result = self.cmd("shell", "wm", "size")
        match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", result.stdout)
        if not match:
            raise ADBError(f"Could not parse screen size from: {result.stdout.strip()}")
        self._screen_size = (int(match.group(1)), int(match.group(2)))
        return self._screen_size

    def tap(self, x: int, y: int) -> None:
        self.cmd("shell", "input", "tap", str(x), str(y))

    def tap_ratio(self, rx: float, ry: float) -> tuple[int, int]:
        width, height = self.screen_size()
        x = round(width * rx)
        y = round(height * ry)
        self.tap(x, y)
        return x, y

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 350) -> None:
        self.cmd(
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        )

    def back(self) -> None:
        self.cmd("shell", "input", "keyevent", "BACK")

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self.cmd("exec-out", "screencap", "-p", text=False)
        path.write_bytes(result.stdout)
        return path


def timestamped_name(prefix: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_") or "screen"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{safe}.png"


def get_coord(config: dict[str, Any], name: str) -> dict[str, Any]:
    coords = config.get("coords") or {}
    if name not in coords:
        available = ", ".join(sorted(coords)) or "(none)"
        raise ADBError(f"Unknown coordinate '{name}'. Available: {available}")
    return coords[name]


def tap_named(session: ADBSession, config: dict[str, Any], name: str) -> tuple[int, int]:
    coord = get_coord(config, name)
    if "xy" in coord:
        x, y = coord["xy"]
        session.tap(int(x), int(y))
        return int(x), int(y)
    if "ratio" in coord:
        rx, ry = coord["ratio"]
        return session.tap_ratio(float(rx), float(ry))
    raise ADBError(f"Coordinate '{name}' must define either 'xy' or 'ratio'.")


def screenshot_path(config: dict[str, Any], name: str) -> Path:
    out_dir = resolve_path(config.get("screenshot_dir", "output/gopay_adb_screenshots"))
    return out_dir / timestamped_name(name)


def run_flow(
    session: ADBSession,
    config: dict[str, Any],
    flow_name: str,
    *,
    execute: bool,
    allow_unlink: bool,
    yes: bool,
) -> None:
    flows = config.get("flows") or {}
    if flow_name not in flows:
        available = ", ".join(sorted(flows)) or "(none)"
        raise ADBError(f"Unknown flow '{flow_name}'. Available: {available}")
    if flow_name.startswith("unlink") and not allow_unlink:
        raise ADBError("Unlink flows require --allow-unlink.")

    steps = flows[flow_name]
    print(f"Flow: {flow_name}")
    print(f"Mode: {'execute' if execute else 'dry-run'}")
    for index, step in enumerate(steps, start=1):
        if "tap" in step:
            name = step["tap"]
            coord = get_coord(config, name)
            print(f"{index}. tap {name}: {coord}")
            if execute:
                x, y = tap_named(session, config, name)
                print(f"   tapped at {x},{y}")
        elif "wait" in step:
            seconds = float(step["wait"])
            print(f"{index}. wait {seconds}s")
            if execute:
                time.sleep(seconds)
        elif "screenshot" in step:
            name = step["screenshot"]
            path = screenshot_path(config, name)
            print(f"{index}. screenshot -> {path}")
            if execute:
                session.screenshot(path)
        elif "back" in step:
            print(f"{index}. back")
            if execute:
                session.back()
        elif "pause" in step:
            message = str(step["pause"])
            print(f"{index}. pause: {message}")
            if execute and not yes:
                input(message + " ")
            elif execute:
                print("   skipped because --yes was provided")
        else:
            raise ADBError(f"Unsupported flow step #{index}: {step}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control GoPay linked-app pages with ADB coordinate taps.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to coordinate config JSON. Copy the example before editing real coords.",
    )
    parser.add_argument("--device", default="", help="ADB device id, overriding config.device.")
    parser.add_argument("--adb", default="", help="ADB executable path, overriding config.adb_path.")
    parser.add_argument(
        "--set-ratio",
        action="append",
        default=[],
        metavar="NAME=RX,RY",
        help="Override a named coordinate ratio for this run. Can be repeated.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="List connected ADB devices.")
    sub.add_parser("size", help="Print the current device screen size.")

    shot = sub.add_parser("screenshot", help="Save a screenshot for coordinate calibration.")
    shot.add_argument("--name", default="manual", help="Screenshot label.")

    tap = sub.add_parser("tap", help="Tap an absolute, ratio, or named coordinate.")
    tap_group = tap.add_mutually_exclusive_group(required=True)
    tap_group.add_argument("--coord", help="Named coordinate from config.coords.")
    tap_group.add_argument("--xy", nargs=2, type=int, metavar=("X", "Y"))
    tap_group.add_argument("--ratio", nargs=2, type=float, metavar=("RX", "RY"))

    swipe = sub.add_parser("swipe", help="Swipe using absolute coordinates.")
    swipe.add_argument("x1", type=int)
    swipe.add_argument("y1", type=int)
    swipe.add_argument("x2", type=int)
    swipe.add_argument("y2", type=int)
    swipe.add_argument("--duration-ms", type=int, default=350)

    sub.add_parser("back", help="Send Android back key.")

    flow = sub.add_parser("run-flow", help="Run a configured tap flow.")
    flow.add_argument("name", help="Flow name from config.flows.")
    flow.add_argument("--execute", action="store_true", help="Actually run taps/screenshots. Omit for dry-run.")
    flow.add_argument("--allow-unlink", action="store_true", help="Required for unlink flows.")
    flow.add_argument("--yes", action="store_true", help="Skip pause prompts during the flow.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(resolve_path(args.config))
    apply_coord_overrides(config, args.set_ratio)
    session = ADBSession(
        adb_path=args.adb or config.get("adb_path", "adb"),
        device=args.device or config.get("device", ""),
    )

    try:
        if args.command == "devices":
            print(session.list_devices())
        elif args.command == "size":
            width, height = session.screen_size()
            print(f"{width}x{height}")
        elif args.command == "screenshot":
            path = screenshot_path(config, args.name)
            session.screenshot(path)
            print(path)
        elif args.command == "tap":
            if args.coord:
                x, y = tap_named(session, config, args.coord)
            elif args.xy:
                x, y = args.xy
                session.tap(x, y)
            else:
                rx, ry = args.ratio
                x, y = session.tap_ratio(rx, ry)
            print(f"tapped {x},{y}")
        elif args.command == "swipe":
            session.swipe(args.x1, args.y1, args.x2, args.y2, args.duration_ms)
            print("swiped")
        elif args.command == "back":
            session.back()
            print("back")
        elif args.command == "run-flow":
            run_flow(
                session,
                config,
                args.name,
                execute=args.execute,
                allow_unlink=args.allow_unlink,
                yes=args.yes,
            )
        else:
            raise ADBError(f"Unsupported command: {args.command}")
    except ADBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
