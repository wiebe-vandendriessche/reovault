import { API } from '#lib/api/index.js';

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
	connected: false,
	/** Set on the first connect, so a page load doesn't flash "reconnecting". */
	everConnected: false
});

let source: EventSource | null = null;
let onAlertCb: (() => void) | undefined;
let retryTimer: ReturnType<typeof setTimeout> | undefined;
let backoff = 1000;

function bumpAll() {
	live.activity++;
	live.problems++;
	live.devices++;
	live.alert++;
	live.settings++;
}

function bumpActivity() {
	live.activity++;
	live.problems++;
}

export function connectLive(onAlert?: () => void) {
	if (onAlert) onAlertCb = onAlert;
	if (source || retryTimer) return;
	const es = new EventSource(`${API}/events`);
	source = es;
	es.addEventListener('hello', () => {
		backoff = 1000;
		live.connected = true;
		live.everConnected = true;
		bumpAll();
	});
	es.addEventListener('resync', bumpAll);
	es.addEventListener('activity', bumpActivity);
	// Destination views (overview, settings) refetch with activity.
	es.addEventListener('destinations', bumpActivity);
	es.addEventListener('problems', () => live.problems++);
	es.addEventListener('devices', () => live.devices++);
	es.addEventListener('settings', (e) => {
		live.settings++;
		try {
			if (JSON.parse(e.data).origin === 'file') live.fileEdits++;
		} catch {
			/* older server: no origin */
		}
	});
	es.addEventListener('alert', () => {
		live.alert++;
		onAlertCb?.();
	});
	es.onerror = () => {
		live.connected = false;
		// A network drop reconnects on its own (`retry: 5000`), but a non-200
		// (a proxy's 502 while the container restarts) closes the stream for good.
		if (es.readyState !== EventSource.CLOSED || source !== es) return;
		source = null;
		retryTimer = setTimeout(() => {
			retryTimer = undefined;
			connectLive();
		}, backoff);
		backoff = Math.min(backoff * 2, 30_000);
	};
}

export function disconnectLive() {
	clearTimeout(retryTimer);
	retryTimer = undefined;
	backoff = 1000;
	source?.close();
	source = null;
	live.connected = false;
	live.everConnected = false;
}
