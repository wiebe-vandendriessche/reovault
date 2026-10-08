import { API } from '$lib/api';

/** The one SSE stream. Each counter bumps when the server hints that data
 * of that kind changed; pages read the counter inside a `query()` so they
 * refetch. `hello` (every (re)connect) and `resync` bump everything, which
 * is what makes a dropped connection harmless. */
export const live = $state({
	activity: 0,
	problems: 0,
	devices: 0,
	alert: 0,
	/** reovault.toml changed (dashboard save or a hand edit of the file). */
	settings: 0,
	/** Bumped only for edits made to reovault.toml outside the dashboard. */
	fileEdits: 0,
	connected: false
});

let source: EventSource | null = null;

function bumpAll() {
	live.activity++;
	live.problems++;
	live.devices++;
	live.alert++;
	live.settings++;
}

export function connectLive(onAlert?: () => void) {
	if (source) return;
	source = new EventSource(`${API}/events`);
	source.addEventListener('hello', () => {
		live.connected = true;
		bumpAll();
	});
	source.addEventListener('resync', bumpAll);
	source.addEventListener('activity', () => {
		live.activity++;
		live.problems++;
	});
	source.addEventListener('problems', () => live.problems++);
	source.addEventListener('devices', () => live.devices++);
	source.addEventListener('settings', (e) => {
		live.settings++;
		try {
			if (JSON.parse(e.data).origin === 'file') live.fileEdits++;
		} catch {
			/* older server: no origin */
		}
	});
	source.addEventListener('alert', () => {
		live.alert++;
		onAlert?.();
	});
	source.onerror = () => {
		// EventSource reconnects on its own (server sends `retry: 5000`).
		live.connected = false;
	};
}

export function disconnectLive() {
	source?.close();
	source = null;
	live.connected = false;
}
