"""Fail the build when a utility class produces no CSS.

Tailwind silently ignores a class name it does not recognise. ``h-4.5``
looks entirely plausible, sits between two real steps of the spacing scale,
and generates nothing at all, so every icon written that way rendered at the
SVG's intrinsic size. Nothing warns about it, which is how it shipped.

Rather than guess at substrings, this extracts the class names the stylesheet
actually defines and checks membership, so compound utilities such as
``space-y-2`` (whose selector carries a suffix) are handled correctly.

Run with:  python scripts/check_tailwind_classes.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "backend" / "app" / "static" / "assets"
SOURCE = ROOT / "frontend" / "src"

BACKSLASH = chr(92)
QUOTES = set(chr(34) + chr(39) + chr(96))

#: Only size and spacing utilities. Those are the ones where a missing rule
#: is invisible in review but glaring on screen.
CHECKED = re.compile(
    r"^-?(?:h|w|size|p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|gap|gap-x|gap-y"
    r"|space-x|space-y|top|bottom|left|right|inset|translate-x|translate-y)"
    r"-[0-9]+(?:[.][0-9]+)?$"
)
VARIANT = re.compile(
    r"^(?:hover|focus|active|disabled|sm|md|lg|xl|2xl|dark|group-hover"
    r"|focus-visible|max-md|max-sm):"
)
SELECTOR = re.compile(
    r"[.]((?:[A-Za-z0-9_-]|" + re.escape(BACKSLASH) + r"[^A-Za-z0-9])+)"
)
LITERAL = re.compile(r"[" + chr(34) + chr(39) + chr(96) + r"]([^" + chr(34) + chr(39) + chr(96) + r"]*)")


def defined_classes(css: str) -> set[str]:
    """Every class name the stylesheet defines, with escapes removed."""
    return {m.replace(BACKSLASH, "") for m in SELECTOR.findall(css)}


def used_classes() -> dict[str, set[str]]:
    """Candidate utilities mentioned anywhere in the frontend sources."""
    found: dict[str, set[str]] = {}
    for path in sorted(SOURCE.rglob("*.ts")) + sorted(SOURCE.rglob("*.tsx")):
        # Line by line, so a quoted literal can never span a newline.
        for line in path.read_text(encoding="utf-8").splitlines():
            for literal in LITERAL.findall(line):
                for token in literal.split():
                    bare = VARIANT.sub("", token)
                    # Record the token as written: Tailwind names the rule after the
                    # full class including any variant prefix, so sm:p-6 is defined as
                    # sm:p-6 and looking up a bare p-6 would wrongly report it missing.
                    if CHECKED.match(bare):
                        rel = str(path.relative_to(ROOT)).replace(BACKSLASH, "/")
                        found.setdefault(token, set()).add(rel)
    return found


def main() -> int:
    stylesheets = sorted(ASSETS.glob("index-*.css"))
    if not stylesheets:
        print("No built stylesheet found. Build the frontend first.", file=sys.stderr)
        return 1

    defined = defined_classes(stylesheets[-1].read_text(encoding="utf-8"))
    missing = {c: f for c, f in used_classes().items() if c not in defined}

    if not missing:
        print("All size and spacing classes resolve to generated CSS.")
        return 0

    print(str(len(missing)) + " class name(s) generate no CSS:", file=sys.stderr)
    for cls, files in sorted(missing.items()):
        print("  " + cls.ljust(20) + " " + ", ".join(sorted(files)), file=sys.stderr)
    print("", file=sys.stderr)
    print("Either the name is a typo, or the value belongs in", file=sys.stderr)
    print("theme.extend.spacing in frontend/tailwind.config.js.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
