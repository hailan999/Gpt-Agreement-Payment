#!/usr/bin/env python3
"""Batch-convert JSON files to single-line JSON without changing originals."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_DIR = "json_one_line_output"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert one JSON file or a folder of JSON files to single-line JSON.",
    )
    parser.add_argument(
        "input",
        help="Input JSON file or folder containing JSON files.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Folder for converted files. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "-m",
        "--merge-file",
        help=(
            "Write all converted JSON files into one output file, one JSON per line. "
            "Relative paths are resolved inside --output-dir."
        ),
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="When input is a folder, also process JSON files in subfolders.",
    )
    parser.add_argument(
        "--suffix",
        default=".min.json",
        help="Suffix for output files when processing one folder. Default: .min.json",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files.",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Escape non-ASCII characters in output.",
    )
    return parser.parse_args()


def collect_json_files(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    pattern = "**/*.json" if recursive else "*.json"
    return sorted(p for p in input_path.glob(pattern) if p.is_file())


def output_path_for(source: Path, input_root: Path, output_dir: Path, suffix: str) -> Path:
    if input_root.is_file():
        relative_parent = Path()
    else:
        relative_parent = source.parent.relative_to(input_root)

    output_name = source.stem + suffix
    return output_dir / relative_parent / output_name


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_single_line_json(path: Path, data: Any, ensure_ascii: bool, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=ensure_ascii, separators=(",", ":"))
        f.write("\n")


def to_single_line(data: Any, ensure_ascii: bool) -> str:
    return json.dumps(data, ensure_ascii=ensure_ascii, separators=(",", ":"))


def resolve_merge_path(output_dir: Path, merge_file: str) -> Path:
    path = Path(merge_file).expanduser()
    if path.is_absolute():
        return path.resolve()
    return output_dir / path


def merge_json_files(files: list[Path], destination: Path, ensure_ascii: bool, overwrite: bool) -> tuple[int, int]:
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    converted = 0
    failed = 0
    with destination.open("w", encoding="utf-8", newline="\n") as f:
        for source in files:
            try:
                data = load_json(source)
                f.write(to_single_line(data, ensure_ascii))
                f.write("\n")
            except Exception as exc:  # Keep batch conversion going after one bad file.
                failed += 1
                print(f"[FAIL] {source} -> {exc}", file=sys.stderr)
                continue

            converted += 1
            print(f"[OK] {source} -> {destination}")
    return converted, failed


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 2
    if input_path.is_dir() and output_dir == input_path:
        print("Output folder cannot be the same as the input folder.", file=sys.stderr)
        return 2

    files = collect_json_files(input_path, args.recursive)
    if not files:
        print(f"No JSON files found: {input_path}", file=sys.stderr)
        return 1

    if args.merge_file:
        merge_path = resolve_merge_path(output_dir, args.merge_file)
        try:
            converted, failed = merge_json_files(files, merge_path, args.ascii, args.overwrite)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Done. Merged: {converted}, failed: {failed}, output: {merge_path}")
        return 1 if failed else 0

    converted = 0
    failed = 0
    for source in files:
        destination = output_path_for(source, input_path, output_dir, args.suffix)
        try:
            data = load_json(source)
            write_single_line_json(destination, data, args.ascii, args.overwrite)
        except Exception as exc:  # Keep batch conversion going after one bad file.
            failed += 1
            print(f"[FAIL] {source} -> {exc}", file=sys.stderr)
            continue

        converted += 1
        print(f"[OK] {source} -> {destination}")

    print(f"Done. Converted: {converted}, failed: {failed}, output: {output_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
