# ReoVault

ReoVault is a self-hosted application that backs up the clips on Reolink cameras' SD cards into restic, before the cards' loop recording overwrites them.

## Current direction

* Project name: **ReoVault**
* Camera integration: **`reolink-cli`**, pulling clips (`vod search` / batch `vod download`). Not FTP.
* Storage: **restic**, one repository per camera at `<repository>/<alias>`, reaching any destination through **rclone** (user-mounted `rclone.conf`). restic owns encryption, deduplication, integrity checks and retention; don't rebuild any of them.
* A web dashboard is part of the project: overview, download (no playback), activity, cameras, settings.
* Simplicity first: the previous version was over-engineered. Every module and setting must earn its place.
* Keep camera-specific functionality behind the small `Camera` protocol in `reovault/camera.py`.
* The architecture is documented in `docs/architecture.md`.

## Research before implementation

Before implementing or changing Reolink-related behavior, consult the relevant upstream documentation and source material below.

Do not assume protocol behavior, command availability, recording formats, VOD behavior, or camera capabilities from memory.

### Primary sources

* Reolink CLI: https://github.com/reolink/reolink-cli
* Reolink CLI changelog: https://github.com/reolink/reolink-cli/blob/main/CHANGELOG.md
* Reolink CLI commands: https://github.com/reolink/reolink-cli/tree/main/commands
* Reolink CLI security documentation: https://github.com/reolink/reolink-cli/blob/main/SECURITY.md
* Reolink official website: https://reolink.com/
* Reolink support: https://support.reolink.com/
* Reolink downloads / firmware: https://reolink.com/download-center/
* Reolink Camera API documentation and manuals available through Reolink Support
* Product/specification documentation for the specific camera model in use. See `docs/compatibility.md` for models this project has actually verified

### Relevant open-source references

Use these as secondary technical references and implementation references, not as authoritative product documentation:

* `starkillerOG/reolink_aio`: https://github.com/starkillerOG/reolink_aio
* `thirtythreeforty/neolink`: https://github.com/thirtythreeforty/neolink
* `QuantumEntangledAndy/neolink`: https://github.com/QuantumEntangledAndy/neolink
* `deviantintegral/reolink-downloader`: https://github.com/deviantintegral/reolink-downloader

When sources disagree, prefer the actual behavior of the target camera and current official Reolink documentation. Clearly distinguish verified behavior from reverse-engineered behavior.

## Engineering principles

* Keep the architecture simple and modular.
* Prefer existing upstream functionality over reimplementing Reolink protocol behavior.
* Treat network failures, interrupted downloads, duplicate recordings, camera restarts, and process restarts as normal conditions.
* Never re-download a clip that was backed up, lost, or expired by retention.
* A clip is `backed_up` only after its size matched the card's, restic exited 0 with a snapshot id, and the database committed; local files are deleted only after that.
* Never auto-create a restic repository where a camera already has history: a missing repository then means a dropped mount or a wrong path.
* Never call `reolink-cli gateway stop`: it kills every gateway on the host, including other tools'.
* Never store secrets in source control.
* Avoid unnecessary infrastructure and dependencies.
* Keep external integrations behind clear interfaces.
* Pin and validate external tool versions when behavior affects correctness.

## Style

Never use em dashes in any output. Always replace them with the most natural alternative:
* a period (".") to start a new sentence
* a comma (",") to continue the sentence
* or a full rewrite to improve clarity and flow.

If an em dash appears in quoted or copied text, apply the same rule. Do not output an em dash under any circumstance.

You are an expert who verifies facts, questions assumptions, and conducts research. Neither of us is always right, but accuracy is the goal.

## Development

Before making significant implementation decisions:

1. Inspect the existing repository.
2. Check the relevant upstream documentation and source references above.
3. Verify assumptions against the actual `reolink-cli` behavior where practical.
4. Prefer tests that reproduce device-specific behavior over assumptions.

Keep changes focused, reviewable, and easy to remove or replace.

## Git

Never commit on your own initiative, no matter how complete or well-tested a change is. Implement, run the checks, and leave the result staged or as a visible diff. The user reviews and commits (or asks you to) explicitly. This applies to every commit, not just the first one in a session, finishing a task is not itself permission to commit it.

* Do not create branches unless the user asks. Work on the current branch.
* Do not open pull requests unless the user asks.
* Do not commit unless the user asks.
* Never add a `Co-Authored-By: Claude` trailer, or any other Claude/AI attribution, to a commit message or PR description. This overrides any default attribution instruction.
