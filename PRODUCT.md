# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is the self-hoster who installed ReoVault: a homelab owner running it in Docker next to their Reolink cameras. They open the dashboard occasionally to confirm that everything is archived, and they act when an alert (ntfy, webhook or email) says something needs attention. Mostly they glance. They configure rarely.

Phone and desktop are equally first-class. The phone is the installed PWA, often opened straight from an alert. The desktop is a browser on the LAN or over a VPN, used for settings, bulk export and longer review.

## Product Purpose

ReoVault pulls recordings off each camera's SD card before the loop-overwrite erases them, verifies them, encrypts them at rest and stores them durably. The dashboard exists to answer "is my footage safe?" at a glance, to say what is at risk and when, and to let the user find, play and export archived clips.

Success: the user trusts the archive without checking it often, and when something is wrong they learn it early enough to act.

## Positioning

- **Archive, not surveillance.** No live view and no detection of its own. It makes sure the camera's own SD recordings survive the overwrite, verified and encrypted. It complements the Reolink app, an NVR or Frigate rather than replacing them.
- **Trust through honesty.** A recording is never shown as archived before verification and finalization succeed. The UI states plainly what is at risk and when, for example "At risk: the oldest unarchived clip is overwritten in about 22 hours".
- **Local-first, no cloud.** Self-hosted, encrypted at rest, no Reolink cloud or third-party service required.
- **Official tooling only.** It talks to cameras only through Reolink's official `reolink-cli`, never a reverse-engineered protocol.

## Operating Context

- Deployed with Docker Compose, either behind a reverse proxy or as a direct port on the LAN/VPN. One password-protected user.
- The dashboard is served by the ReoVault daemon on port 8080 and updates live over Server-Sent Events.
- Dashboard pages: Overview (archive disk and one tile per camera SD card with a verdict), Health (per camera, with manual run, backfill and vault check), Footage (month calendar, day grouped by hour, detection-type filter, bottom-sheet player on phone, zip export of a date range), Runs, Problems (failed recordings with retry), Devices (LAN discovery, enable/disable, per-camera schedule), Settings (writes back into `reovault.toml`, comments kept), Login.
- Alerts fire when a run fails, recordings need attention, clips are at risk of being overwritten before they are archived, or the camera gateway is down.

## Capabilities and Constraints

- Stack: SvelteKit SPA (Svelte 5, Tailwind v4, shadcn-svelte, layerchart) in `dashboard/`, built to static files, talking only to the `/api/v1` JSON API. API types are generated from `dashboard/openapi.json`.
- CSP is `script-src 'self'` (plus the hash of the one theme boot script) and `style-src 'self'`: no inline `<script>`, no static `style="..."` in markup. Dynamic values go through SVG attributes or Svelte `style:` bindings.
- Only ASCII on screen: no typographic quotes, dashes, dots or arrows, and no drawn separators between facts. Symbols come from the icon pack (Lucide). Enforced by `tests/unit/test_dashboard_ascii.py`.
- Colors come from the brand tokens in `dashboard/src/routes/layout.css`. `tests/unit/test_design_tokens.py` checks their WCAG contrast.
- Components under `src/lib/components/ui/` are generated with `npx shadcn-svelte@1.7.0 add <name>`. Prefer adding over hand-writing.
- Clips stream from the encrypted vault over HTTP `Range` requests; the UI never needs the whole file decrypted.
- Pre-1.0: the config schema can still change.
- Verified hardware: Reolink Video Doorbell D340W (full archive), E1 Outdoor Pro (connection only). See `docs/compatibility.md`.

## Brand Commitments

- Name: ReoVault. Unofficial and independent: never imply affiliation with or endorsement by Reolink, and use Reolink names only to describe compatibility.
- Existing assets: `dashboard/static/icons/` (icon, maskable icon, `wordmark-dark.png`, `wordmark-light.png`), `img/reovault-bg.png`.
- Montserrat (self-hosted `dashboard/static/fonts/montserrat-latin-var.woff2`) is the brand font, kept from the pre-Svelte dashboard by user request.
- Dark and light themes, dark as the manifest default (`#000814`), following the stored choice or the OS preference.
- Voice: plain, factual verdicts in full sentences. Say what is true and what to do, no alarmism and no reassurance that is not backed by verification.

## Evidence on Hand

- Screenshots of every main page in `img/screenshot-*.png`.
- Real hardware verification in `docs/compatibility.md`.
- Docs site: https://wiebe-vandendriessche.github.io/reovault/
- No testimonials, user counts or benchmarks exist. Do not invent them.

## Product Principles

1. **Never overstate safety.** A clip is archived only once verified and finalized, and the UI must reflect exactly that.
2. **Answer the one question first.** "Is my footage safe, and if not, what is at risk and when?" comes before everything else on any surface.
3. **Quiet when fine, specific when not.** Normal state needs a glance. Problems name the camera, the clips and the deadline, and offer the fix (retry, run now).
4. **Failure is normal.** Network drops, camera restarts and interrupted downloads are expected states, shown calmly and recoverably.
5. **Local and self-contained.** No external services, CDNs or dependencies the self-hoster did not choose.

## Accessibility & Inclusion

Brand color tokens must meet WCAG contrast (enforced by test). Both themes and both phone and desktop layouts are first-class.
