import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';
import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			preprocess: vitePreprocess(),
			compilerOptions: { runes: true },
			// A pure SPA: FastAPI serves index.html for every client route (see
			// reovault/web/app.py) and computes the CSP hash of its inline boot
			// script at startup.
			adapter: adapter({ fallback: 'index.html', strict: true }),
			output: { bundleStrategy: 'split' },
			// Registered by +layout.svelte in production builds only.
			serviceWorker: { register: false }
		})
	],
	server: {
		// `npm run dev` against a local `reovault daemon`: same-origin from the
		// browser's point of view, so the session cookie and CSRF checks work
		// exactly as in production.
		proxy: { '/api': { target: 'http://127.0.0.1:8080', changeOrigin: false } }
	},
	test: {
		expect: { requireAssertions: true },
		include: ['src/**/*.{test,spec}.{js,ts}'],
		environment: 'node'
	}
});
