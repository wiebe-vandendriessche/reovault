"""Regression guards for the dashboard design sweep: icon stroke/dots, the
Montserrat @font-face format, card-title casing, and the wordmark files.
Each of these was a real, reported defect with a specific root cause; this
file makes sure none of them come back silently. Parses app.css as text,
same style as test_design_tokens.py, rather than rendering it.
"""

from __future__ import annotations

from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "reovault" / "web" / "static"
_APP_CSS = (_STATIC_DIR / "app.css").read_text()
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "reovault" / "web" / "templates"


def _rule_body(css: str, selector: str) -> str:
    start = css.index(selector)
    open_idx = css.index("{", start)
    depth = 0
    end_idx = open_idx
    for i, ch in enumerate(css[open_idx:], start=open_idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    return css[open_idx:end_idx]


def test_icon_declares_stroke_width_and_round_caps():
    """Lucide draws every glyph at stroke-width:2 with round caps/joins; the
    sprite build strips those presentation attributes so CSS controls them.
    Without this, every icon renders at half weight and the lower "dot" on
    circle-help/triangle-alert/circle-alert is an invisible zero-length
    segment (see icons.py's docstring)."""
    body = _rule_body(_APP_CSS, "\n  .icon {")
    assert "stroke-width: 2" in body
    assert "stroke-linecap: round" in body
    assert "stroke-linejoin: round" in body


def test_font_face_uses_a_recognized_format_and_full_weight_range():
    """`format("woff2-variations")` is a legacy hint modern browsers don't
    recognize; an unrecognized format makes the whole `src` get skipped and
    Montserrat silently falls back to Arial. The font's real `wght` axis is
    100-900 (verified with fonttools), so the declared range must match it,
    not a narrower guess."""
    body = _rule_body(_APP_CSS, "@font-face {")
    assert 'format("woff2")' in body
    assert "woff2-variations" not in body
    assert "font-weight: 100 900" in body


def test_card_label_is_not_forced_uppercase():
    body = _rule_body(_APP_CSS, "\n  .card-label {")
    assert "text-transform" not in body


def test_light_wordmark_is_not_the_transparent_logo():
    """static/icons/wordmark-light.png must never be byte-identical to
    img/reovault.png, the transparent wordmark the navbar must not use (its
    background isn't opaque, and .topbar's background assumes it is)."""
    root = _STATIC_DIR.parents[2]
    transparent = (root / "img" / "reovault.png").read_bytes()
    wordmark_light = (_STATIC_DIR / "icons" / "wordmark-light.png").read_bytes()
    assert wordmark_light != transparent


def test_wordmark_visibility_reacts_to_os_light_mode_not_only_data_theme():
    """Regression: the wordmark swap used to be gated only on an explicit
    `data-theme="light"` attribute that nothing in this app ever sets (no
    JS writes it anywhere) -- unlike the color tokens a few lines above it
    in the same file, which correctly react to
    `@media (prefers-color-scheme: light)`. A light-OS visitor got the dark
    wordmark, baked-in black background and all, on a white topbar."""
    after_auth_logo = _APP_CSS[_APP_CSS.index(".auth-logo") :]
    # The trailing `{` distinguishes the real rule from app.css's own
    # explanatory comment just above it, which also names the media query
    # in backticks with no brace after it.
    open_idx = after_auth_logo.index("{", after_auth_logo.index("(prefers-color-scheme: light) {"))
    depth, end_idx = 0, open_idx
    for i, ch in enumerate(after_auth_logo[open_idx:], start=open_idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    body = after_auth_logo[open_idx:end_idx]
    assert "only-light" in body
    assert "display: block" in body


def test_no_inline_style_attributes_in_templates():
    """This app's CSP is `style-src 'self'`, deliberately with no
    `unsafe-inline`: in a real CSP-enforcing browser, every `style="..."`
    attribute in the entire dashboard is silently discarded, which is why
    every dynamic bar (coverage margin, SD card, the growth sparkline, the
    calendar heatmap) rendered at zero width/height/opacity, and why every
    spacing fix that used an inline `style="margin...:var(...)"` never
    actually took effect. A per-request dynamic value belongs in a real CSS
    class, an SVG presentation attribute (width/x/height, not `style=`), or
    a plain HTML attribute matched with a CSS attribute selector -- never in
    a `style` attribute, static or templated."""
    offenders = []
    for path in _TEMPLATES_DIR.rglob("*"):
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "style=" in line:
                offenders.append(f"{path.relative_to(_TEMPLATES_DIR)}:{lineno}: {line.strip()}")
    assert not offenders, "inline style attributes found (blocked by CSP):\n" + "\n".join(offenders)
