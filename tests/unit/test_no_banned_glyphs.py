"""Enforces that no typed ASCII/Unicode icon-glyph character ever appears in
rendered output. Scoped to the dashboard's own Svelte/TS sources under
`dashboard/src/` (the generated shadcn components in `components/ui/` are
vendored upstream code and excluded), not CSS, whose comments legitimately
*describe* the characters this test bans. Allowlists nothing: any new
instance must be replaced with a real icon or a CSS-rendered element (see
`.sep` in layout.css), not typed.
"""

from __future__ import annotations

from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "src"
_VENDORED = _SRC_DIR / "lib" / "components" / "ui"

_BANNED_CHARS = "—–·→←‹›«»×…▸▾►▪•"
_BANNED_ENTITIES = (
    "&mdash;",
    "&ndash;",
    "&middot;",
    "&rarr;",
    "&larr;",
    "&lsaquo;",
    "&rsaquo;",
    "&times;",
    "&hellip;",
)


def test_no_banned_glyph_characters_in_dashboard_sources():
    offenders = []
    for path in _SRC_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in (".svelte", ".ts", ".html"):
            continue
        if path.is_relative_to(_VENDORED) or path.name.endswith(".d.ts"):
            continue
        text = path.read_text()
        for ch in _BANNED_CHARS:
            if ch in text:
                offenders.append(f"{path.relative_to(_SRC_DIR)}: character {ch!r}")
        for entity in _BANNED_ENTITIES:
            if entity in text:
                offenders.append(f"{path.relative_to(_SRC_DIR)}: entity {entity!r}")
    assert not offenders, "banned glyphs found:\n" + "\n".join(offenders)
