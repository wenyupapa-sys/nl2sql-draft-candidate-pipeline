#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


DEFAULT_PATTERNS = [
    r"sample_values\s*=",
    r"\bsamples\s*=\s*\[",
    r"first_rows\s*:",
    r"Observed execution preview",
    r"\bValue Examples\b",
    r"\bvalue_examples\b",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if prompt files contain known DB-value leakage markers.")
    parser.add_argument("--prompt-root", action="append", required=True)
    parser.add_argument("--forbid-pattern", action="append", default=[])
    args = parser.parse_args()

    patterns = [re.compile(p, re.IGNORECASE) for p in (DEFAULT_PATTERNS + args.forbid_pattern)]
    violations: list[tuple[Path, str]] = []
    for root_str in args.prompt_root:
        root = Path(root_str)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.txt")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                if pattern.search(text):
                    violations.append((path, pattern.pattern))
                    break
    if violations:
        print("Prompt audit failed. Potential DB-value leakage markers found:")
        for path, pattern in violations[:100]:
            print(f"- {path}: {pattern}")
        if len(violations) > 100:
            print(f"... {len(violations) - 100} more")
        return 2
    print("Prompt audit passed: no known DB-value leakage markers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
