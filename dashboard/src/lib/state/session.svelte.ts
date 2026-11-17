import { api, ApiError, setCsrf, type Session } from '#lib/api/index.js';

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

/** POSTs /login or /setup with a CSRF token fetched just now: the one held may
 * be bound to a session that has since expired, or past the anonymous token's
 * 10 minutes. A CSRF refusal anyway (a race with a logout) refreshes once more. */
export async function postWithFreshCsrf(path: '/login' | '/setup', body: unknown): Promise<Session> {
	for (let attempt = 0; ; attempt++) {
		setCsrf((await api<Session>('/session')).csrf_token);
		try {
			return await api<Session>(path, { method: 'POST', body });
		} catch (e) {
			const csrf = e instanceof ApiError && e.status === 403 && e.message.includes('CSRF');
			if (!csrf || attempt > 0) throw e;
		}
	}
}
