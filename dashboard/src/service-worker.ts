/// <reference types="@sveltejs/kit" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />
/**
 * App-shell service worker. Caches the built assets and the SPA shell so the
 * dashboard opens instantly and offline. It never touches `/api/*`: every
 * recording, run and setting comes from the network, so nothing a user saw
 * can be served to anyone else, or after logout, from this cache. The shell
 * itself holds no user data, which is what makes caching it safe.
 */
import { build, files, version } from '$service-worker';

const sw = self as unknown as ServiceWorkerGlobalScope;
const CACHE = `reovault-${version}`;
const SHELL = '/index.html';
const PRECACHE = [...build, ...files, SHELL];

sw.addEventListener('install', (event) => {
	event.waitUntil(
		caches
			.open(CACHE)
			.then((cache) => cache.addAll(PRECACHE))
			.then(() => sw.skipWaiting())
	);
});

sw.addEventListener('activate', (event) => {
	event.waitUntil(
		caches
			.keys()
			.then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
			.then(() => sw.clients.claim())
	);
});

sw.addEventListener('fetch', (event) => {
	const req = event.request;
	if (req.method !== 'GET') return;
	const url = new URL(req.url);
	if (url.origin !== sw.location.origin || url.pathname.startsWith('/api/')) return;

	// Navigations: network first (a fresh deploy wins), cached shell offline.
	if (req.mode === 'navigate') {
		event.respondWith(
			fetch(req).catch(async () => (await caches.match(SHELL)) ?? Response.error())
		);
		return;
	}

	// Built and static assets: cache first; names are content-hashed.
	if (PRECACHE.includes(url.pathname)) {
		event.respondWith(caches.match(req).then((hit) => hit ?? fetch(req)));
	}
});
