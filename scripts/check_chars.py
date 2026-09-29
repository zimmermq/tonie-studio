#!/usr/bin/env python3
"""Fail on stray characters that are invisible or easy to mistake in source.

Lookalike substitutions (an em dash for --, a curly quote for ') and invisible
characters (a NUL or a non-breaking space inside a string literal) survive a
read-through of the file: they render as nothing, or as the character that was
meant. Checking the bytes is the only reliable way to catch them.

Deliberate non-ASCII is fine and common here, because the report is German.
Only the characters below are rejected, and a needed one should be written as
an escape so it is visible in the source.

Usage: python3 scripts/check_chars.py [path ...]
"""

import sys
import unicodedata
from pathlib import Path

SUSPECT = {
    "\x00": "NUL",
    "\u00a0": "non-breaking space",
    "\u2014": "em dash",
    "\u2013": "en dash",
    "\u2018": "curly quote",
    "\u2019": "curly quote",
    "\u201c": "curly double quote",
    "\u201d": "curly double quote",
    "\u200b": "zero-width space",
    "\ufeff": "byte order mark",
}

# .html is in here because the Jinja templates are source too, and a curly
# quote pasted into one is exactly as invisible as it is in Python.
CHECKED_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".html"}
SKIPPED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
                "data", "notes-local"}


def files(roots):
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir() or any(part in SKIPPED_DIRS for part in path.parts):
                continue
            if path.suffix in CHECKED_SUFFIXES:
                yield path


def main(argv):
    findings = 0
    for path in files(argv[1:] or ["."]):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            print(f"{path}: not valid UTF-8")
            findings += 1
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for column, char in enumerate(line, start=1):
                if char in SUSPECT:
                    name = unicodedata.name(char, "unnamed")
                    print(f"{path}:{line_number}:{column}: {SUSPECT[char]} "
                          f"(U+{ord(char):04X} {name})")
                    findings += 1
    if findings:
        print(f"\n{findings} suspect character(s). Write the needed ones as escapes.")
        return 1
    print("No suspect characters.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
