#!/usr/bin/env python3
import argparse
import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def collect_json_files(
    input_dir: Path,
    output_file: Path,
    recursive: bool,
    include_pattern: str,
    exclude_patterns: list[str],
) -> list[Path]:
    files = sorted(input_dir.rglob(include_pattern) if recursive else input_dir.glob(include_pattern))
    output_resolved = output_file.resolve()
    result: list[Path] = []
    for path in files:
        if not path.is_file():
            continue
        if path.resolve() == output_resolved:
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude_patterns):
            continue
        result.append(path)
    return result


def normalize_records(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    return [data]


def choose_name(
    credentials: dict[str, Any],
    path: Path,
    index: int,
    name_source: str,
    name_prefix: str,
) -> str:
    if name_source == "index":
        return f"{name_prefix}-{index:03d}"

    if name_source == "email":
        email = str(credentials.get("email", "")).strip()
        if email:
            return email
        account_id = str(credentials.get("account_id", "")).strip()
        if account_id:
            return account_id

    # filename fallback and explicit filename mode
    return path.stem


def dedupe_name(name: str, used: dict[str, int]) -> str:
    current = used.get(name, 0) + 1
    used[name] = current
    if current == 1:
        return name
    return f"{name}-{current}"


def build_payload(
    files: list[Path],
    platform: str,
    account_type: str,
    concurrency: int,
    priority: int,
    name_source: str,
    name_prefix: str,
) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}
    counter = 1

    for path in files:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        for item in normalize_records(raw):
            credentials = item if isinstance(item, dict) else {"raw_value": item}
            base_name = choose_name(credentials, path, counter, name_source, name_prefix)
            name = dedupe_name(base_name, used_names)

            accounts.append(
                {
                    "name": name,
                    "platform": platform,
                    "type": account_type,
                    "credentials": credentials,
                    "concurrency": concurrency,
                    "priority": priority,
                }
            )
            counter += 1

    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": exported_at,
        "proxies": [],
        "accounts": accounts,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build sub2api account import payload from token JSON files."
    )
    parser.add_argument("-i", "--input", default=".", help="Input directory (default: current directory)")
    parser.add_argument(
        "-o",
        "--output",
        default="sub2api_accounts_import.json",
        help="Output JSON file (default: sub2api_accounts_import.json)",
    )
    parser.add_argument("--include", default="codex*.json", help="Input filename pattern (default: token_*.json)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=["merged*.json", "import_payload*.json", "sub2api_accounts_import*.json"],
        help="Exclude filename pattern (can be used multiple times)",
    )
    parser.add_argument("--recursive", action="store_true", help="Scan subdirectories recursively")
    parser.add_argument("--platform", default="openai", help="Account platform (default: openai)")
    parser.add_argument("--account-type", default="oauth", help="Account type (default: oauth)")
    parser.add_argument("--concurrency", type=int, default=3, help="Default account concurrency (default: 3)")
    parser.add_argument("--priority", type=int, default=50, help="Default account priority (default: 50)")
    parser.add_argument(
        "--name-source",
        choices=["email", "filename", "index"],
        default="email",
        help="How to generate account names (default: email)",
    )
    parser.add_argument("--name-prefix", default="acc", help="Name prefix when --name-source=index")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    output_file = Path(args.output).resolve()

    if not input_dir.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_dir}")
    if args.concurrency < 0:
        raise SystemExit("concurrency must be >= 0")
    if args.priority < 0:
        raise SystemExit("priority must be >= 0")

    files = collect_json_files(
        input_dir=input_dir,
        output_file=output_file,
        recursive=args.recursive,
        include_pattern=args.include,
        exclude_patterns=args.exclude,
    )
    if not files:
        raise SystemExit("No matching JSON files found.")

    payload = build_payload(
        files=files,
        platform=args.platform,
        account_type=args.account_type,
        concurrency=args.concurrency,
        priority=args.priority,
        name_source=args.name_source,
        name_prefix=args.name_prefix,
    )

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Built sub2api payload with {len(payload['accounts'])} accounts -> {output_file}")


if __name__ == "__main__":
    main()