# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is the self-hoster who installed ReoVault: a homelab owner running it in Docker next to their Reolink cameras. They open the dashboard occasionally to confirm that everything is backed up, and they act when an alert (ntfy, webhook or email) says something needs attention. Mostly they glance. They configure rarely.

Phone and desktop are equally first-class. The phone is the installed PWA, often opened straight from an alert. The desktop is a browser on the LAN or over a VPN, used for settings, bulk export and longer review.

## Product Purpose

ReoVault copies every clip off each camera's SD card before the loop recording overwrites it, into an encrypted restic backup on a destination the user picks (a local disk, Nextcloud, OneDrive, S3, anything rclone reaches). The dashboard exists to answer "is every card backed up?" at a glance, to say what is waiting or at risk and when it catches up, and to let the user download any backed-up clip or day.

Success: the user trusts the backups without checking them often, and when something is wrong they learn it early enough to act.

## Positioning

- **Backup, not surveillance.** No live view, no playback, no detection of its own. Playback of what's still on the card stays in the Reolink app; ReoVault makes sure the card's clips survive the overwrite. It complements the Reolink app, an NVR or Frigate rather than replacing them.
- **Trust through honesty.** A clip is never shown as backed up before its size matched the card's and restic confirmed a snapshot. The UI states plainly what is waiting, what is at risk and what was lost.
- **Not locked in.** The backups are ordinary restic repositories: restorable with plain restic, with or without ReoVault.
- **Self-hosted, destination of your choice.** No Reolink cloud. The backup goes wherever the user points restic, local or cloud.
- **Official tooling only.** It talks to cameras only through Reolink's official `reolink-cli`, never a reverse-engineered protocol.

## Operating Context

- Deployed with Docker Compose, either behind a reverse proxy or as a direct port on the LAN/VPN. One password-protected user.
- The dashboard is served by the ReoVault daemon on port 8080 and updates live over Server-Sent Events.
- Dashboard pages: Overview (what needs attention, one tile per camera with its verdict, backlog and catch-up estimate, the destination), Footage (month calendar, day grouped by hour, detection-type filter, download a clip or a day as zip), Activity (backup runs, clips that keep failing, clips lost this week), Cameras (LAN discovery, demo cameras, on/off, rename), Settings (backup destination with a test, interval, retention, alerts; writes back into `reovault.toml`, comments kept), Login.
- Alerts fire when backups of a camera keep failing, clips are about to be overwritten before they're backed up, clips keep failing to download, the weekly restic check finds errors, or the destination answers with no backups where there should be some.

## Capabilities and Constraints

- Stack: SvelteKit SPA (Svelte 5, Tailwind v4, shadcn-svelte) in `dashboard/`, built to static files, talking only to the `/api/v1` JSON API. API types are generated from `dashboard/openapi.json`.
- CSP is `script-src 'self'` (plus the hash of the one theme boot script) and `style-src 'self'`: no inline `<script>`, no static `style="..."` in markup. Dynamic values go through SVG attributes or Svelte `style:` bindings.
- Only ASCII on screen: no typographic quotes, dashes, dots or arrows, and no drawn separators between facts. Symbols come from the icon pack (Lucide). Enforced by `tests/unit/test_dashboard_ascii.py`.
- Colors come from the brand tokens in `dashboard/src/routes/layout.css`. `tests/unit/test_design_tokens.py` checks their WCAG contrast.
- Components under `src/lib/components/ui/` are generated with `npx shadcn-svelte@1.7.0 add <name>`. Prefer adding over hand-writing.
- Downloads stream out of `restic dump`; there is no in-browser player.
- Pre-1.0: the config schema can still change.
- Verified hardware: Reolink Video Doorbell D340W and E1 Outdoor Pro, both backed up locally and to Nextcloud. See `docs/compatibility.md`.

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

1. **Never overstate safety.** A clip is backed up only once restic confirmed it, and the UI must reflect exactly that.
2. **Answer the one question first.** "Is every card backed up, and if not, what's waiting and when does it catch up?" comes before everything else on any surface.
3. **Quiet when fine, specific when not.** Normal state needs a glance. Problems name the camera and the clips, and offer the fix (back up now, check the destination).
4. **Failure is normal.** Network drops, camera restarts and interrupted downloads are expected states, shown calmly and recoverably.
5. **Local and self-contained.** No external services, CDNs or dependencies the self-hoster did not choose.

## Accessibility & Inclusion

Brand color tokens must meet WCAG contrast (enforced by test). Both themes and both phone and desktop layouts are first-class.
