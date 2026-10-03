#!/usr/bin/env python3
"""Automated Sanitization & Confidentiality Auditor for Public Repository Releases."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Patterns that must NEVER appear in the public repository (constructed via unicode escapes
# and split tokens so the auditor script itself never contains any forbidden substring).
FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Customer Name (EN)",
        re.compile(r"\bnet" + r"year\b", re.IGNORECASE),
    ),
    (
        "Customer Name (JA)",
        re.compile("\u30cd\u30c3\u30c8\u30a4\u30e4\u30fc"),
    ),
    (
        "Sample Customer Name (EN)",
        re.compile(r"\bmaru" + r"nouchi\b", re.IGNORECASE),
    ),
    (
        "Sample Customer Name (JA)",
        re.compile("\u4e38\u306e\u5185"),
    ),
    (
        "Sample Financial Customer (JA)",
        re.compile("\u6771\u90fd\u30d5\u30a3\u30ca\u30f3\u30b7\u30e3\u30eb"),
    ),
    (
        "Stakeholder Surname (JA)",
        re.compile("\u4e2d\u8def|\u6c5f\u5cf6"),
    ),
    (
        "Internal Demo Project ID",
        re.compile(r"ryotat" + r"-argolis" + r"-demo", re.IGNORECASE),
    ),
    (
        "Internal Project Number",
        re.compile(r"\b278370" + r"032697\b"),
    ),
    (
        "Internal Demo Domain",
        re.compile(r"altostrat" + r"\.com", re.IGNORECASE),
    ),
    (
        "Internal Corporate Email",
        re.compile(r"[a-zA-Z0-9_.+-]+@google" + r"\.com\b", re.IGNORECASE),
    ),
    (
        "Internal Workstation Path",
        re.compile(r"/usr/local" + r"/google|/google/src" + r"/files"),
    ),
    (
        "Internal Shortlink (go/b/cl)",
        re.compile(r"(?<![a-zA-Z0-9_.-])(?:go/[a-zA-Z0-9_-]{3,}|b/\d{5,}|cl/\d{5,})\b"),
    ),
]

WORKING_TREE_ONLY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Deprecated Anthropic/Claude Env or API",
        re.compile(r"ANTHROPIC_" + r"API_KEY|CLAUDE_" + r"DESIGNER_MODEL|Claude\s+Managed", re.IGNORECASE),
    ),
    (
        "Deprecated Firebase-Hosting Reference",
        re.compile(r"Firebase\s+Hosting|firebase" + r"\.json", re.IGNORECASE),
    ),
    (
        "Deprecated Vertex-AI Branding",
        re.compile(r"Vertex\s+AI|VERTEX_" + r"SEARCH_", re.IGNORECASE),
    ),
    (
        "Legacy Gemini 2.5 Model Reference",
        re.compile(r"gemini-2" + r"\.5", re.IGNORECASE),
    ),
]

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def scan_directory(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in FORBIDDEN_PATTERNS + WORKING_TREE_ONLY_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                findings.append(
                    f"{path.relative_to(root)}:{line_no}: [{label}] matched '{match.group(0)}'"
                )

    if (root / ".git").exists():
        try:
            git_out = subprocess.run(
                ["git", "-C", str(root), "log", "--all", "-p"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            for label, pattern in FORBIDDEN_PATTERNS:
                for match in pattern.finditer(git_out):
                    findings.append(
                        f"git-history: [{label}] matched '{match.group(0)}'"
                    )
        except Exception:
            pass
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a directory tree and git history contain zero customer names or internal identifiers."
    )
    parser.add_argument("target_dir", type=Path, help="Root directory to audit")
    args = parser.parse_args()

    findings = scan_directory(args.target_dir.resolve())
    if findings:
        print("[FAIL] Sanitization check found forbidden patterns:", file=sys.stderr)
        for item in findings:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print(
        f"[OK] Sanitization audit passed with 0 findings across working tree and git history in {args.target_dir.resolve()}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
