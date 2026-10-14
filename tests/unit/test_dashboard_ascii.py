"""Everything the dashboard can put on screen is plain ASCII: no typographic
quotes, dashes, dots or arrows typed into markup, and no CSS-drawn dot
separators either. Symbols come from the icon pack only. Covers the
dashboard sources (vendored shadcn components included, since they render
too) and the Python modules whose strings reach the UI through the API."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD_SRC = _ROOT / "dashboard" / "src"
_API_STRING_SOURCES = [
    *sorted((_ROOT / "reovault" / "web").rglob("*.py")),
    _ROOT / "reovault" / "notify.py",
    _ROOT / "reovault" / "health.py",
]


def _non_ascii(path: Path) -> list[str]:
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for ch in line:
            if ord(ch) > 0x7F:
                hits.append(f"{path.relative_to(_ROOT)}:{lineno}: U+{ord(ch):04X} {ch!r}")
    return hits


def test_dashboard_sources_are_ascii():
    offenders = []
    for path in _DASHBOARD_SRC.rglob("*"):
        if path.suffix not in (".svelte", ".ts", ".html", ".css") or path.name == "schema.d.ts":
            continue
        offenders += _non_ascii(path)
    assert not offenders, "non-ASCII in dashboard sources:\n" + "\n".join(offenders)


def test_no_css_drawn_separators():
    offenders = [
        str(p.relative_to(_ROOT))
        for p in _DASHBOARD_SRC.rglob("*.svelte")
        if 'class="sep"' in p.read_text(encoding="utf-8")
    ]
    assert not offenders, "use spacing between facts, not a drawn separator: " + ", ".join(
        offenders
    )


def test_api_strings_are_ascii():
    offenders = [hit for path in _API_STRING_SOURCES for hit in _non_ascii(path)]
    assert not offenders, "non-ASCII in API/alert strings:\n" + "\n".join(offenders)
