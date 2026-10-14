import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, apiUrl, ApiError, setCsrf, setUnauthorizedHandler } from './index';

function respond(status: number, body: unknown) {
	return vi.fn(async () =>
		new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
	);
}

afterEach(() => vi.unstubAllGlobals());

describe('apiUrl', () => {
	it('drops undefined and null query values', () => {
		expect(apiUrl('/day', { device: 2, date_: undefined, types: null })).toBe('/api/v1/day?device=2');
		expect(apiUrl('/health')).toBe('/api/v1/health');
	});
});

describe('api', () => {
	it('sends the CSRF token on mutations only', async () => {
		const fetchMock = respond(200, {});
		vi.stubGlobal('fetch', fetchMock);
		setCsrf('tok');
		await api('/health');
		await api('/actions/run', { method: 'POST' });
		const headers = (i: number) => (fetchMock.mock.calls[i] as unknown as [string, RequestInit])[1].headers as Record<string, string>;
		expect(headers(0)['X-CSRF-Token']).toBeUndefined();
		expect(headers(1)['X-CSRF-Token']).toBe('tok');
	});

	it('turns a FastAPI string detail into the error message', async () => {
		vi.stubGlobal('fetch', respond(409, { detail: 'Another export is already running.' }));
		await expect(api('/export')).rejects.toEqual(new ApiError(409, 'Another export is already running.'));
	});

	it('turns validation errors into one readable sentence', async () => {
		vi.stubGlobal(
			'fetch',
			respond(422, { detail: [{ loc: ['body', 'archive_time'], msg: 'times are HH:MM' }] })
		);
		await expect(api('/schedule', { method: 'PUT', body: {} })).rejects.toThrow('archive_time: times are HH:MM');
	});

	it('calls the unauthorized handler on 401, except for the login call itself', async () => {
		const handler = vi.fn();
		setUnauthorizedHandler(handler);
		vi.stubGlobal('fetch', respond(401, { detail: 'unauthorized' }));
		await expect(api('/health')).rejects.toBeInstanceOf(ApiError);
		expect(handler).toHaveBeenCalledTimes(1);
		await expect(api('/login', { method: 'POST', body: {} })).rejects.toBeInstanceOf(ApiError);
		expect(handler).toHaveBeenCalledTimes(1);
	});

	it('reports a network failure plainly', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('fetch failed'))));
		await expect(api('/health')).rejects.toThrow("Can't reach ReoVault");
	});
});
