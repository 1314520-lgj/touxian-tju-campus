#!/usr/bin/env python3
"""Rewrite absolute paths inside delivered text evidence to relative ones.

An acceptance run records the paths it happened to run from. Shipped verbatim,
those paths leak the machine layout — user name, drive, checkout directory,
virtualenv location — into artifacts that are supposed to be about the product,
and two runs from different directories then differ for no reason.

This rewrites every path it can place inside the repository as a
repository-relative POSIX path, and every path it cannot (an interpreter's
``site-packages``, a scratch directory) as a marked placeholder. Only path text
changes: line endings, ordering and every measured value are untouched.

    python tools/normalize_evidence_paths.py --root .            # rewrite
    python tools/normalize_evidence_paths.py --root . --check    # report only

It is idempotent — a second run reports zero rewrites — so it is safe to run
after every acceptance run and before rebuilding RELEASE_MANIFEST.json.

Three deliberate limits:

* Placeholders are bracketed, never angle-bracketed, because the same text also
  has to stay legal inside XML element content.
* A path is allowed to contain spaces (a checkout can live under a folder whose
  name has one), so the scan is anchored at the start of a token rather than at
  the first space. The lookbehind keeps it from starting inside a URL or a
  ``file.py:12:`` suffix, where a letter happens to precede a colon.
* The machine-specific prefixes come from the running interpreter, never from a
  constant, so this file is not itself a place where a user name is written
  down. `test_evidence_tree_hygiene.py` keeps it that way.
"""

from __future__ import annotations

import argparse
import re
import sys
import sysconfig
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_release_manifest import iter_shipped

# An absolute Windows path. The separator may be doubled, because the same path
# appears escaped inside JSON and with single separators inside logs. Newlines
# end a token; the quote and angle brackets that end one inside JSON and XML do
# too. The lookbehind refuses to start in the middle of `https://` or `.py:12:`.
WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\{1,2}|[^\\\"'<>\n\r|])*")

# A path that walks up out of the tree and lands in an interpreter's packages,
# which is how pytest prints the source of a warning. Requiring the leading ".."
# keeps this from rewriting a regular expression that merely mentions the word.
RELATIVE_SITE_PACKAGES = re.compile(r"(?:\.\.\\{1,2})+(?:[^\\\"'<>\n\r|]*\\{1,2})+site-packages\\{1,2}", re.IGNORECASE)

# Anchors inside an absolute path whose tail is meaningful but whose prefix is
# not. Checked after the repository roots and before the generic home prefix, so
# a checkout always wins and an interpreter's tree never keeps the user name.
ANCHORS: tuple[tuple[str, str], ...] = (
    ("\\site-packages\\", "[site-packages]"),
    ("\\appdata\\local\\temp\\", "[tmp]"),
)

# pytest stamps the build machine's name into the JUnit suite element. The
# lookahead skips a value that is already the placeholder, so a second run is a
# genuine no-op rather than a match that rewrites to the same text.
HOSTNAME_ATTR = re.compile(r'hostname="(?!localhost")[^"]*"')
PLACEHOLDER_HOSTNAME = 'hostname="localhost"'


def decode(token: str) -> str:
    """Undo JSON backslash doubling so a token can be compared as a real path."""
    return token.replace("\\\\", "\\")


def build_roots(root: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(specific, generic) prefix/replacement pairs, longest prefix first.

    Longest-first matters: a release copy on the Desktop sits *under* the home
    directory, and stripping the shorter home prefix first would leave a
    fragment of the checkout name behind. `generic` is tried last, after the
    anchors, so that an interpreter path under the home directory is reported as
    an interpreter path rather than as `<home>/...`.
    """
    home = Path.home()
    specific: list[tuple[str, str]] = [(str(root), "")]

    desktop = home / "Desktop"
    if desktop.is_dir():
        try:
            for child in desktop.iterdir():
                if child.is_dir():
                    specific.append((str(child), ""))
        except OSError:
            pass

    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        specific.append((purelib, "[site-packages]"))

    scratch = tempfile.gettempdir()
    if scratch:
        specific.append((scratch, "[tmp]"))

    specific.sort(key=lambda entry: len(entry[0]), reverse=True)
    return specific, [(str(home), "[home]")]


def _strip_prefix(real: str, prefix: str, replacement: str) -> str | None:
    for separator in ("\\", "/"):
        candidate = prefix.replace("\\", separator)
        if real.lower().startswith(candidate.lower()):
            tail = real[len(candidate) :].lstrip("\\/").replace("\\", "/")
            if not replacement:
                return tail
            return f"{replacement}/{tail}" if tail else replacement
    return None


def rewrite_absolute(token: str, specific: list[tuple[str, str]], generic: list[tuple[str, str]]) -> str | None:
    """Return the replacement for one absolute path token, or None to keep it."""
    real = decode(token)

    for prefix, replacement in specific:
        result = _strip_prefix(real, prefix, replacement)
        if result is not None:
            return result

    lowered = real.lower()
    for anchor, placeholder in ANCHORS:
        index = lowered.find(anchor)
        if index != -1:
            tail = real[index + len(anchor) :].replace("\\", "/")
            return f"{placeholder}/{tail}" if tail else placeholder

    for prefix, replacement in generic:
        result = _strip_prefix(real, prefix, replacement)
        if result is not None:
            return result

    return None


def normalize_text(
    text: str, specific: list[tuple[str, str]], generic: list[tuple[str, str]], *, is_xml: bool
) -> tuple[str, int]:
    changed = 0

    def absolute(match: re.Match) -> str:
        nonlocal changed
        token = match.group(0)
        replacement = rewrite_absolute(token, specific, generic)
        if replacement is None:
            return token
        changed += 1
        return replacement

    text = WINDOWS_ABSOLUTE.sub(absolute, text)

    text, hits = RELATIVE_SITE_PACKAGES.subn("[site-packages]/", text)
    changed += hits

    if is_xml:
        text, hits = HOSTNAME_ATTR.subn(PLACEHOLDER_HOSTNAME, text)
        changed += hits

    return text, changed


def is_text(raw: bytes) -> bool:
    return b"\x00" not in raw[:8192]


def read_text(path: Path) -> str | None:
    """Return the file's text, or None when it is not UTF-8 text.

    `is_text` only rules out a NUL byte, which a short binary file — a truncated
    PNG, say — can easily lack. The decode is the real test, so callers use this
    rather than decoding themselves.
    """
    raw = path.read_bytes()
    if not is_text(raw):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", type=Path)
    parser.add_argument("--check", action="store_true", help="report what would change without writing")
    args = parser.parse_args()

    root = args.root.resolve()
    specific, generic = build_roots(root)

    touched = 0
    total = 0
    for relative, path in iter_shipped(root):
        text = read_text(path)
        if text is None:
            continue
        updated, changed = normalize_text(text, specific, generic, is_xml=path.suffix.lower() == ".xml")
        if not changed:
            continue
        touched += 1
        total += changed
        print(f"{'would rewrite' if args.check else 'rewrote'} {changed:3d} path(s) in {relative}")
        if not args.check:
            # newline='' keeps the file's own CRLF/LF choice exactly as it was
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)

    verb = "would be rewritten" if args.check else "rewritten"
    print(f"\n{touched} file(s), {total} path(s) {verb}.")
    if args.check and total:
        print("Run without --check to apply.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
