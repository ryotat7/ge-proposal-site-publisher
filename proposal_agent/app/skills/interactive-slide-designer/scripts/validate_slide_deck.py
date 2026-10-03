#!/usr/bin/env python3
"""Deterministic DOM & structure validator for 6-slide interactive HTML5 presentations."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def validate_html_deck(html_str: str) -> list[str]:
    """Returns a list of validation error strings (empty list if valid)."""
    errors: list[str] = []
    if "<!DOCTYPE html>" not in html_str and "<!doctype html>" not in html_str.lower():
        errors.append("Missing <!DOCTYPE html> declaration.")
    if "{{" in html_str or "}}" in html_str:
        errors.append("Unrendered Jinja2 template expressions ({{ or }}) detected.")

    slide_indices = re.findall(r'data-slide-index="(\d+)"', html_str)
    if slide_indices != ["0", "1", "2", "3", "4", "5"]:
        errors.append(
            f"Expected 6 slides with sequential data-slide-index 0..5, got {slide_indices}."
        )

    layouts = re.findall(r'data-layout="([^"]+)"', html_str)
    if len(set(layouts)) < 4:
        errors.append(
            f"Expected bespoke per-slide layouts (at least 4 distinct data-layout values), got {layouts}."
        )

    required_markers = [
        "https://cdn.tailwindcss.com",
        "gsap.min.js",
        "font-awesome",
        "Noto+Sans+JP",
        "Plus+Jakarta+Sans",
        "JetBrains+Mono",
        "slide-counter",
        "progress-bar",
        "ArrowRight",
        "ArrowLeft",
    ]
    for marker in required_markers:
        if marker not in html_str:
            errors.append(f"Missing required interactive slide marker: {marker}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an interactive HTML5 slide deck file."
    )
    parser.add_argument("html_file", type=Path, help="Path to index.html")
    args = parser.parse_args()

    if not args.html_file.exists():
        print(f"[ERROR] File not found: {args.html_file}", file=sys.stderr)
        return 1

    content = args.html_file.read_text(encoding="utf-8")
    errors = validate_html_deck(content)
    if errors:
        for err in errors:
            print(f"[FAIL] {err}", file=sys.stderr)
        return 1

    print(
        f"[OK] {args.html_file} passed all 6-slide HTML5 DOM & layout checks ({len(content)} bytes)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
