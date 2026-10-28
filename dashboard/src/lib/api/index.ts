/** Thin typed client for `/api/v1`. Types come from the server's OpenAPI
 * schema (`npm run gen:api`), so a renamed field is a compile error here,
 * not a blank cell in production. */
import type { components } from './schema';

type S = components['schemas'];
export type Session = S['SessionOut'];
export type Overview = S['OverviewOut'];
export type Camera = S['CameraOut'];
export type Run = S['RunOut'];
export type Clip = S['ClipOut'];
export type Calendar = S['CalendarOut'];
export type Day = S['DayOut'];
export type Activity = S['ActivityOut'];
export type Discovered = S['DiscoveredOut'];
export type AddCamera = S['AddCameraIn'];
export type EditCamera = S['EditCameraIn'];
export type Alerts = S['AlertsOut'];
export type AlertRules = S['AlertRules'];
export type TestResult = S['TestResultOut'];
export type Config = S['ConfigOut'];
export type ConfigField = S['FieldOut'];
export type ConfigSection = S['SectionOut'];
export type DestinationCheck = S['DestinationCameraOut'];
export type RetentionPreview = S['RetentionPreviewOut'];
export type Verdict = Camera['verdict'];

export const API = '/api/v1';

export class ApiError extends Error {
	constructor(
		readonly status: number,
		message: string
	) {
		super(message);
	}
}

let csrfToken = '';
let onUnauthorized: () => void = () => {};

export function setCsrf(token: string) {
	csrfToken = token;
}

export function setUnauthorizedHandler(fn: () => void) {
	onUnauthorized = fn;
}

type Query = Record<string, string | number | boolean | null | undefined>;

export function apiUrl(path: string, query: Query = {}): string {
	const params = new URLSearchParams();
	for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) params.set(k, String(v));
	const qs = params.toString();
	return `${API}${path}${qs ? `?${qs}` : ''}`;
}

/** FastAPI's `detail` is a string for HTTPException and a list for request
 * validation errors; both become one readable sentence. */
function detailOf(body: unknown, status: number): string {
	const detail = (body as { detail?: unknown } | null)?.detail;
	if (typeof detail === 'string') return detail;
	if (Array.isArray(detail))
		return detail
			.map((d: { loc?: unknown[]; msg?: string }) => {
				const field = d.loc?.filter((p) => p !== 'body').join('.');
				return field ? `${field}: ${d.msg}` : d.msg;
			})
			.join('; ');
	return `Request failed (${status}).`;
}

export async function api<T>(
	path: string,
	opts: { method?: 'GET' | 'POST' | 'PUT' | 'DELETE'; query?: Query; body?: unknown } = {}
): Promise<T> {
	const method = opts.method ?? 'GET';
	const headers: Record<string, string> = { Accept: 'application/json' };
	if (method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
	if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
	let res: Response;
	try {
		res = await fetch(apiUrl(path, opts.query), {
			method,
			headers,
			body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
			credentials: 'same-origin'
		});
	} catch {
		throw new ApiError(0, "Can't reach ReoVault. Check your connection.");
	}
	const body = res.headers.get('content-type')?.includes('json') ? await res.json() : null;
	if (res.status === 401 && path !== '/login') onUnauthorized();
	if (!res.ok) throw new ApiError(res.status, detailOf(body, res.status));
	return body as T;
}
