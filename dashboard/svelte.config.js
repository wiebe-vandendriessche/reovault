import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: { runes: true },
	kit: {
		// A pure SPA: FastAPI serves index.html for every client route (see
		// reovault/web/app.py) and computes the CSP hash of its inline boot
		// script at startup.
		adapter: adapter({ fallback: 'index.html', strict: true }),
		output: { bundleStrategy: 'split' },
		// Registered by +layout.svelte in production builds only.
		serviceWorker: { register: false }
	}
};

export default config;
