"""Mechanizes the WCAG contrast checking that was done by hand during the
brand token rewrite. Parses the two theme blocks straight out of app.css
(not a hand-copied literal palette: if the tokens drift, this test reads
the drift) and checks a fixed table of known text/surface and fill/surface
pairs -- not a generic CSS contrast linter, just the pairs this app
actually renders.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP_CSS = (
    Path(__file__).resolve().parents[2] / "reovault" / "web" / "static" / "app.css"
).read_text()

_TOKEN_RE = re.compile(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})")


def _extract_block(text: str, start_marker: str) -> dict[str, str]:
    start = text.index(start_marker)
    # A theme block is one brace-delimited `{ ... }`; find its matching
    # close by simple depth counting (no nested braces inside a rule body
    # here, so this is exact, not a heuristic).
    open_idx = text.index("{", start)
    depth = 0
    end_idx = open_idx
    for i, ch in enumerate(text[open_idx:], start=open_idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    body = text[open_idx:end_idx]
    return {name: value.lower() for name, value in _TOKEN_RE.findall(body)}


def _dark_tokens() -> dict[str, str]:
    return _extract_block(_APP_CSS, "\n  :root {")


def _light_tokens() -> dict[str, str]:
    return _extract_block(_APP_CSS, ':root[data-theme="light"] {')


def _linearize(c: int) -> float:
    c_norm = c / 255
    return c_norm / 12.92 if c_norm <= 0.03928 else ((c_norm + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# (foreground token, background token) pairs actually rendered as body text
# somewhere in the app, per theme. Must clear WCAG AA's 4.5:1.
_TEXT_PAIRS = [
    ("text-1", "surface-0"),
    ("text-1", "surface-1"),
    ("text-1", "surface-2"),
    ("text-2", "surface-0"),
    ("text-2", "surface-1"),
    ("text-2", "surface-2"),
    ("text-3", "surface-1"),
    ("text-3", "surface-2"),  # the binding case found by hand
    ("brand-text", "surface-0"),
    ("brand-text", "surface-1"),
    ("on-brand", "brand"),  # button label on a --brand fill
    ("on-warn", "warn"),  # #offline-banner text on a --warn fill
]

# (color token, surface it sits against) pairs used as a border/ring/fill,
# never as text. Must clear the non-text minimum, 3:1.
#
# --brand itself is only ever a border/ring directly on --surface-0 (e.g.
# the topbar's active-tab underline) -- it measures under 3:1 against every
# elevated surface in dark mode (surface-1, surface-2, and brand-dim all
# fail; found by this test). Anywhere elevated (.chip.is-on, the calendar's
# current-date outline) uses --brand-text instead, which clears all of them.
_NON_TEXT_PAIRS = [
    ("brand", "surface-0"),
    ("brand-text", "surface-1"),
    ("brand-text", "surface-2"),
    ("brand-text", "brand-dim"),
    ("ok", "surface-1"),
    ("warn", "surface-1"),
    ("bad", "surface-1"),
]


def _check(tokens: dict[str, str], pairs: list[tuple[str, str]], minimum: float, theme: str):
    for fg_name, bg_name in pairs:
        fg, bg = tokens[fg_name], tokens[bg_name]
        ratio = _contrast(fg, bg)
        assert ratio >= minimum, (
            f"[{theme}] --{fg_name} ({fg}) on --{bg_name} ({bg}) is {ratio:.2f}:1, "
            f"needs {minimum}:1"
        )


def test_dark_theme_text_pairs_clear_aa():
    _check(_dark_tokens(), _TEXT_PAIRS, 4.5, "dark")


def test_light_theme_text_pairs_clear_aa():
    _check(_light_tokens(), _TEXT_PAIRS, 4.5, "light")


def test_dark_theme_non_text_pairs_clear_aa():
    _check(_dark_tokens(), _NON_TEXT_PAIRS, 3.0, "dark")


def test_light_theme_non_text_pairs_clear_aa():
    _check(_light_tokens(), _NON_TEXT_PAIRS, 3.0, "light")
