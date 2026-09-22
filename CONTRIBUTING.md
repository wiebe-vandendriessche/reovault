# Contributing to ReoVault

Thanks for considering a contribution. ReoVault is a small, focused project;
the notes below should be enough to get a change reviewed and merged
smoothly.

## Development setup

```bash
uv sync --dev
uv run pytest                            # unit + integration tests (camera-free, fake provider)
uv run ruff check . && uv run ruff format --check . && uv run mypy reovault
```

Camera-touching work (`reovault probe`/`run` against real hardware, capturing
fixtures, contract tests) needs `reolink-cli` installed and a real camera on
the LAN; those tests are marked `contract`/`slow` and excluded by default
(`uv run pytest -m "contract or slow"` to include them).

## Before implementing Reolink-related behavior

Read [`CLAUDE.md`](CLAUDE.md)'s "Research before implementation" section.
Do not assume protocol behavior, command availability, recording formats, or
camera capabilities from memory; verify against the upstream `reolink-cli`
docs/changelog or against real device behavior, and say which one a claim is
based on.

## Making a change

1. Open an issue first for anything non-trivial (new feature, behavior
   change) so the approach can be discussed before code is written.
2. Keep changes focused. A bug fix doesn't need to also refactor nearby
   code. Smaller PRs review faster.
3. Add or update tests for the behavior you're changing. Non-trivial logic
   (a branch, a parser, a security-relevant path) needs a test that would
   fail if the logic broke.
4. Run the checks above before opening a PR; CI enforces the same ones.
5. Open a PR against `main`. Fill in the PR template. It's short.

All changes land via pull request; direct pushes to `main` are disabled.

## Versioning and releases

Releases are tag-triggered (`vX.Y.Z`) and built/signed/published by CI (see
[`.github/workflows/release.yml`](.github/workflows/release.yml)). Version
bumps happen in a normal PR that updates `version` in `pyproject.toml`;
maintainers tag a release from `main` once a bump is merged.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Please don't open a public issue for a
vulnerability.
