#!/usr/bin/env python3
import argparse
import fnmatch
import json
from pathlib import Path
from typing import Any


def collect_json_files(
    input_dir: Path,
    recursive: bool,
    include_pattern: str,
    exclude_patterns: list[str],
    output_file: Path | None = None,
) -> list[Path]:
    files = sorted(input_dir.rglob(include_pattern) if recursive else input_dir.glob(include_pattern))
    output_resolved = output_file.resolve() if output_file else None
    result: list[Path] = []

    for path in files:
        if not path.is_file():
            continue
        resolved_path = path.resolve()
        if output_resolved and resolved_path == output_resolved:
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude_patterns):
            continue
        result.append(path)

    return result


def normalize_records(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    return [data]


def extract_refresh_tokens(files: list[Path]) -> list[str]:
    refresh_tokens: list[str] = []

    for path in files:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        for item in normalize_records(raw):
            if not isinstance(item, dict):
                continue
            refresh_token = str(item.get("refresh_token", "")).strip()
            if refresh_token:
                refresh_tokens.append(refresh_token)

    return refresh_tokens


def build_output_path(output_file: Path, input_dir: Path) -> Path:
    folder_name = input_dir.name.strip()
    if not folder_name:
        return output_file
    return output_file.with_name(f"{output_file.stem}_{folder_name}{output_file.suffix}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract refresh_token values from JSON files into a text file."
    )
    parser.add_argument("-i", "--input", default=".", help="Input directory (default: current directory)")
    parser.add_argument(
        "-o",
        "--output",
        default="refresh_tokens.txt",
        help="Output TXT file (default: refresh_tokens.txt)",
    )
    parser.add_argument("--include", default="codex*.json", help="Input filename pattern (default: codex*.json)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=["sub2api_accounts_import*.json", "*_sub.json"],
        help="Exclude filename pattern (can be used multiple times)",
    )
    parser.add_argument("--recursive", action="store_true", help="Scan subdirectories recursively")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_dir}")

    output_file = build_output_path(Path(args.output).resolve(), input_dir)
    files = collect_json_files(
        input_dir=input_dir,
        recursive=args.recursive,
        include_pattern=args.include,
        exclude_patterns=args.exclude,
        output_file=output_file,
    )
    if not files:
        raise SystemExit("No matching JSON files found.")

    refresh_tokens = extract_refresh_tokens(files)
    if not refresh_tokens:
        raise SystemExit("No refresh_token values found.")

    with output_file.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(refresh_tokens))
        f.write("\n")

    print(f"Extracted {len(refresh_tokens)} refresh_token values -> {output_file}")


if __name__ == "__main__":
    main()
