# Security

## Reporting a vulnerability

See [`SECURITY.md`](https://github.com/wiebe-vandendriessche/reovault/blob/main/SECURITY.md)
in the repository root. Please don't open a public issue for a
vulnerability. Use GitHub's private
[Security Advisories](https://github.com/wiebe-vandendriessche/reovault/security/advisories/new)
instead.

## Encryption model

Backups are restic repositories, one per camera, encrypted by restic
(AES-256 in counter mode with Poly1305 authentication; see restic's
[design document](https://restic.readthedocs.io/en/stable/100_references.html#design)).
The destination, whether a local disk or a cloud account, only ever sees
encrypted data. The password comes from the `restic_password` Docker secret
and is never stored in `reovault.toml`, the database or a log.

**There is no recovery path without the restic password.** Keep a copy in
your password manager, separate from the backups. With it, anyone can restore
the footage using plain restic: see
[Restore without ReoVault](restore-without-reovault.md).

The batch waiting for upload on the server's local disk is plaintext video,
like the SD card it came from, and is deleted as soon as restic confirms the
upload.

## Credentials

ReoVault never stores or logs the camera's password. Camera credentials are
managed entirely by `reolink-cli`'s own encrypted registry
(`aliases.toml`, AES-GCM, 0600). When a camera is added through the
dashboard, the password passes through the ReoVault process exactly once,
on stdin, on its way into that registry. It's never placed on a command
line, persisted, or echoed.

The dashboard's own login is separate: a single-user password (Argon2id
hashed) plus an optional email field used only as a password-manager
autofill hint, not an authorization boundary.

Alert channel secrets (an ntfy token, a webhook URL, an SMTP password) are
read from files at send time and never stored in the database. A failed
send logs only the error, never the URL, since a Slack or Discord webhook
URL is itself the credential.

`rclone.conf` holds your cloud logins (OAuth tokens, WebDAV passwords). It
lives in `config/rclone/`, readable only inside the container. Error text
from restic or rclone is scrubbed of `user:password@` in URLs before it
reaches the dashboard or an alert.

## Dashboard and API

* **Settings write the config file.** A logged-in user can change anything
  in `reovault.toml` that isn't infrastructure or env-set, and the change is
  written to the file. Storage paths, the tool binaries and the
  web bind, cookie, origin and proxy settings are never writable from the
  browser, so a stolen session can't swap the tools or loosen the login
  protections. Secrets are never written either: alert channels hold *paths*
  to secret files, and the restic password and camera passwords are not
  config fields. A logged-in user *can* change the backup destination, so
  treat the dashboard login like the keys to your footage. Pin anything you want out of the dashboard's reach with an
  env var.
* **Sessions:** a signed, `HttpOnly` cookie. Logout revokes sessions
  server-side by bumping a session epoch, so a copied cookie stops working
  too (single user, so every open session is signed out at once).
* **CSRF:** every mutating `/api/v1` request needs an `X-CSRF-Token` bound
  to the session, plus a matching `Origin` when the browser sends one.
* **Content Security Policy:** `script-src 'self'` plus the SHA-256 of the
  one inline boot script, which the server computes from the `index.html`
  it actually serves; `style-src 'self'` with no `'unsafe-inline'`. Dynamic
  styles go through the CSSOM, which CSP permits. The browser console
  reports one refused inline style at startup: SvelteKit's screen-reader
  route announcer hard-codes a `style` attribute, and the stylesheet
  supplies the same rules instead, so nothing is lost.
* **Request size:** bodies over 64 KB get 413, and chunked request bodies
  (which carry no length to check) get 411.
* **`/healthz`** is public and status-only, for the Docker healthcheck.
* **Downloads** stream out of `restic dump`, one day-zip at a time, with no
  temporary files.

## Deployment modes

Binding `127.0.0.1` inside a container makes the dashboard unreachable
either way. Neither a published port nor a reverse proxy connects over
container loopback. Bind `0.0.0.0` (the image's default) regardless of
which mode you pick:

* **Reverse proxy**: rely on **not publishing a port** plus the proxy for
  isolation. Set `forwarded_allow_ips` to the proxy's own address (never
  `"*"`, unless the port is genuinely unreachable except through the proxy)
  or `X-Forwarded-Proto` is silently ignored and the session cookie's
  `Secure` flag ends up wrong.
* **Direct port publish**: set `cookie_secure = false`, since plain HTTP
  has no certificate and the browser drops a `Secure` cookie sent over an
  insecure connection (login otherwise loops back to `/login` with no
  error). Restricting who can reach the port (firewall, VPN, trusted LAN)
  is then your responsibility, same as for any other self-hosted app.

See [Configuration](configuration.md#web).

## Supply chain (released images)

Released Docker images (`ghcr.io/wiebe-vandendriessche/reovault`) are:

* Scanned for known vulnerabilities (Grype) before publishing, gated on
  critical-severity, fixable CVEs.
* Built with the dashboard's npm dependencies installed by `npm ci
  --ignore-scripts` from a committed lockfile, in a build stage only: the
  runtime image carries the static output and no Node.
* Shipped with a CycloneDX SBOM, attached to both the GitHub Release and the
  image itself (via a Sigstore/cosign attestation).
* Signed keylessly with [cosign](https://github.com/sigstore/cosign) using
  the release workflow's GitHub Actions OIDC identity. Verify with:

```bash
cosign verify \
  --certificate-identity-regexp "https://github.com/wiebe-vandendriessche/reovault/.github/workflows/release.yml@.*" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/wiebe-vandendriessche/reovault:<version>
```
