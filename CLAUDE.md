# ReoVault

ReoVault is a local-first, self-hosted application for securely archiving recordings from Reolink cameras.

## Current direction

* Reolink camera recordings are stored on the camera's local storage.
* ReoVault periodically retrieves recordings and archives them locally.
* [reolink-cli](https://github.com/reolink/reolink-cli) is the initial Reolink integration.
* A web dashboard is planned.
* Reliability, deduplication, and encrypted storage are important requirements.

## Engineering principles

* Keep the architecture simple and modular.
* Prefer existing, well-maintained tools and libraries over reimplementing functionality.
* Keep camera-specific logic isolated behind an abstraction.
* Treat network failures, interrupted downloads, duplicate recordings, and restarts as normal conditions.
* Never lose successfully archived data because a later operation fails.
* Do not store secrets in source control.
* Prefer clear, boring implementations over unnecessary complexity.

## Development

Before making significant implementation decisions, inspect the existing repository and relevant upstream documentation.

Do not introduce large frameworks or infrastructure without a clear need.

Keep changes focused and easy to review.
