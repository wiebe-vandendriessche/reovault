import { api, setCsrf, type Session } from '$lib/api';

export const session = $state({
	ready: false,
	authenticated: false,
	version: '',
	pinnedCli: '',
	/** Set when the server itself refuses (e.g. no password configured). */
	blocked: null as string | null
});

export function applySession(s: Session) {
	setCsrf(s.csrf_token);
	session.authenticated = s.authenticated;
	session.version = s.version;
	session.pinnedCli = s.pinned_cli_version;
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
