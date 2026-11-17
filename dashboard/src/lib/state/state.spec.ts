import { afterEach, describe, expect, it, vi } from 'vitest';
import { connectLive, disconnectLive, live } from './live.svelte';
import { postWithFreshCsrf } from './session.svelte';

function json(status: number, body: unknown) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

afterEach(() => {
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('postWithFreshCsrf', () => {
	it('fetches a token first, and refreshes once more on a CSRF refusal', async () => {
		let n = 0;
		const fetch = vi.fn(async (url: string, init?: RequestInit) => {
			if (url.endsWith('/session')) return json(200, { csrf_token: `t${++n}` });
			const token = (init?.headers as Record<string, string>)['X-CSRF-Token'];
			return token === 't2'
				? json(200, { authenticated: true })
				: json(403, { detail: 'missing or invalid CSRF token' });
		});
		vi.stubGlobal('fetch', fetch);
		await expect(postWithFreshCsrf('/login', {})).resolves.toEqual({ authenticated: true });
		expect(fetch).toHaveBeenCalledTimes(4);
	});
});

describe('connectLive', () => {
	it('reconnects with backoff after the server closes the stream for good', () => {
		vi.useFakeTimers();
		const made: FakeSource[] = [];
		class FakeSource {
			static CLOSED = 2;
			readyState = 0;
			onerror: (() => void) | null = null;
			listeners: Record<string, () => void> = {};
			constructor() {
				made.push(this);
			}
			addEventListener(type: string, fn: () => void) {
				this.listeners[type] = fn;
			}
			close() {}
		}
		vi.stubGlobal('EventSource', FakeSource);
		connectLive();
		made[0].readyState = FakeSource.CLOSED; // a proxy's 502
		made[0].onerror?.();
		vi.advanceTimersByTime(1000);
		expect(made).toHaveLength(2);
		const before = live.activity;
		made[1].listeners.destinations();
		expect(live.activity).toBe(before + 1);
		disconnectLive();
	});
});
