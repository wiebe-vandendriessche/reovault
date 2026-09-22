# Security Policy

ReoVault handles camera credentials (via `reolink-cli`'s own encrypted
registry) and archives recordings encrypted at rest, so security issues are
treated as a priority.

## Supported versions

ReoVault is pre-1.0 and released on a rolling basis. Only the latest
released version is supported; please upgrade before reporting an issue if
you're not on the latest tag.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Report it privately through GitHub's
[Security Advisories](../../security/advisories/new) for this repository
("Report a vulnerability" on the Security tab). This reaches the
maintainers directly and keeps the report confidential until a fix is
available.

Please include:

* A description of the vulnerability and its potential impact
* Steps to reproduce (a minimal repro is ideal)
* The ReoVault version, deployment method (Docker/bare-metal), and relevant
  configuration

We'll acknowledge reports as soon as practical, and coordinate on a
disclosure timeline once a fix is ready. Credit is given in the release
notes unless you'd prefer otherwise.

## Scope

In scope:

* ReoVault's own code (`reovault/`): auth, session/CSRF handling,
  encryption/key management, path handling, the web dashboard
* The released Docker image and its build/release pipeline

Out of scope (report upstream instead):

* `reolink-cli` itself. See its own
  [security documentation](https://github.com/reolink/reolink-cli/blob/main/SECURITY.md)
* Reolink camera firmware/protocol vulnerabilities. See
  [Reolink support](https://support.reolink.com/)
