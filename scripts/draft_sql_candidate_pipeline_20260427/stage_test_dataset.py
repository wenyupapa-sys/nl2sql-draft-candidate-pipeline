#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def _replace_path(src: Path, dst: Path, *, copy_dir: bool) -> None:
    if dst.is_symlink() or dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if copy_dir:
            shutil.copytree(src, dst)
        else:
            os.symlink(src.resolve(), dst, target_is_directory=True)
    else:
        shutil.copy2(src, dst)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a BIRD test dataset root with expected file names.")
    parser.add_argument("--test-json", required=True)
    parser.add_argument("--test-tables", required=True)
    parser.add_argument("--test-databases", required=True)
    parser.add_argument("--column-meaning")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--copy-databases", action="store_true", help="Copy test_databases instead of symlinking.")
    args = parser.parse_args()

    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    _replace_path(Path(args.test_json), output_root / "test.json", copy_dir=False)
    _replace_path(Path(args.test_tables), output_root / "test_tables.json", copy_dir=False)
    _replace_path(Path(args.test_databases), output_root / "test_databases", copy_dir=args.copy_databases)
    if args.column_meaning:
        _replace_path(Path(args.column_meaning), output_root / "column_meaning.json", copy_dir=False)
    print(f"Prepared dataset root: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
