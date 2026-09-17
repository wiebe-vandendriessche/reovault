# ReoVault

ReoVault is a local-first, self-hosted application for securely archiving recordings from Reolink cameras.

## Current direction

* Project name: **ReoVault**
* Initial camera integration: **`reolink-cli`**
* Recordings remain on the camera's local storage and are periodically archived by ReoVault.
* A web dashboard is part of the project.
* Reliability, deduplication, integrity verification, and encrypted storage are core requirements.
* Keep camera-specific functionality isolated behind a provider/adapter boundary.

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
* Reolink Video Doorbell D350W product/specification documentation

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
* Never re-download or overwrite successfully archived recordings unnecessarily.
* Never mark a recording as archived before verification and finalization succeed.
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
