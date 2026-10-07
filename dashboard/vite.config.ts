import { defineConfig } from 'vitest/config';
import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
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
