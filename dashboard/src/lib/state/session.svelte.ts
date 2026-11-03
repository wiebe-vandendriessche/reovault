import { api, setCsrf, type Session } from '#lib/api/index.js';

export const session = $state({
	ready: false,
	authenticated: false,
	version: '',
	pinnedCli: '',
	/** No login exists yet: the dashboard shows the setup screen. */
	setupNeeded: false,
	/** A login email already set in the config; the setup form then skips it. */
	setupEmail: null as string | null,
	/** A newer ReoVault release, when the daily check found one. */
	updateAvailable: null as string | null,
	/** Set when the server itself refuses (e.g. no password configured). */
	blocked: null as string | null
});

export function applySession(s: Session) {
	setCsrf(s.csrf_token);
	session.authenticated = s.authenticated;
	session.version = s.version;
	session.pinnedCli = s.pinned_cli_version;
	session.setupNeeded = s.setup_needed;
	session.setupEmail = s.setup_email ?? null;
	session.updateAvailable = s.update_available ?? null;
	session.ready = true;
}

export async function loadSession() {
	try {
		applySession(await api<Session>('/session'));
		session.blocked = null;
	} catch (e) {
		session.blocked = e instanceof Error ? e.message : String(e);
		session.ready = true;
	}
}
