"""The `icon()` Jinja global: renders one `<svg><use></svg>` against the
vendored Lucide sprite at `/static/icons.svg`. A plain function registered
as a template global rather than a macro, so every template gets it for
free with no per-file `{% import %}`.

`.icon { width: 1em; height: 1em; stroke: currentColor; fill: none; }` in
app.css is what makes the glyph inherit its context's color and size; this
module only ever emits the `<use>` reference, never a color or a size.
"""

from __future__ import annotations

from markupsafe import Markup, escape


def icon_tag(name: str, asset_version: str, *, label: str | None = None, cls: str = "") -> Markup:
    """`label=None` (the default, for a purely decorative icon sitting next
    to visible text) renders `aria-hidden="true"`. Passing `label` makes the
    icon itself the accessible name (`role="img" aria-label=...`), for an
    icon-only control with no visible text anywhere near it."""
    classes = f"icon {cls}".strip()
    href = f"/static/icons.svg?v={asset_version}#{name}"
    if label:
        return Markup(
            '<svg class="{}" role="img" aria-label="{}"><use href="{}"></use></svg>'
        ).format(escape(classes), escape(label), escape(href))
    return Markup('<svg class="{}" aria-hidden="true"><use href="{}"></use></svg>').format(
        escape(classes), escape(href)
    )
