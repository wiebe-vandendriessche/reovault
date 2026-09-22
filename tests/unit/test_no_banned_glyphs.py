"""Enforces that no typed ASCII/Unicode icon-glyph character ever appears in
rendered output. Scoped to the template sources under
`reovault/web/templates/` -- the things that actually render to a user --
not `static/*.css`/`*.js`, whose comments legitimately *describe* the
characters this test bans; a CSS/JS comment never reaches the rendered page
anyway. Allowlists nothing: any new instance must be replaced with a real
icon or a CSS-rendered element (see `.sep` in app.css), not typed.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "reovault" / "web" / "templates"

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


def test_no_banned_glyph_characters_in_templates():
    offenders = []
    for path in _TEMPLATES_DIR.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text()
        for ch in _BANNED_CHARS:
            if ch in text:
                offenders.append(f"{path.relative_to(_TEMPLATES_DIR)}: character {ch!r}")
        for entity in _BANNED_ENTITIES:
            if entity in text:
                offenders.append(f"{path.relative_to(_TEMPLATES_DIR)}: entity {entity!r}")
    assert not offenders, "banned glyphs found:\n" + "\n".join(offenders)
