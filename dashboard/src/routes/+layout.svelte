<script lang="ts">
	import './layout.css';
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { api, setUnauthorizedHandler, type Session } from '#lib/api/index.js';
	import { applySession, loadSession, session } from '#lib/state/session.svelte.js';
	import { connectLive, disconnectLive, live } from '#lib/state/live.svelte.js';
	import { initTheme, theme, toggleTheme } from '#lib/state/theme.svelte.js';
	import { confirmDiscard, hasUnsaved } from '#lib/state/unsaved.svelte.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Toaster } from '#lib/components/ui/sonner/index.js';
	import * as Tooltip from '#lib/components/ui/tooltip/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import { cn } from '#lib/utils.js';
	import LayoutDashboard from '@lucide/svelte/icons/layout-dashboard';
	import Video from '@lucide/svelte/icons/video';
	import ListChecks from '@lucide/svelte/icons/list-checks';
	import Cctv from '@lucide/svelte/icons/cctv';
	import Settings from '@lucide/svelte/icons/settings';
	import Moon from '@lucide/svelte/icons/moon';
	import Sun from '@lucide/svelte/icons/sun';
	import LogOut from '@lucide/svelte/icons/log-out';

	let { children } = $props();

	const nav = [
		{ href: '/', label: 'Overview', icon: LayoutDashboard },
		{ href: '/footage', label: 'Footage', icon: Video },
		{ href: '/activity', label: 'Activity', icon: ListChecks },
		{ href: '/cameras', label: 'Cameras', icon: Cctv },
		{ href: '/settings', label: 'Settings', icon: Settings }
	];

	const isLogin = $derived(page.url.pathname === '/login');

	function active(href: string) {
		const p = page.url.pathname;
		return href === '/' ? p === '/' : p === href || p.startsWith(`${href}/`);
	}

	function toLogin() {
		const here = page.url.pathname + page.url.search;
		disconnectLive();
		session.authenticated = false;
		if (page.url.pathname !== '/login')
			goto(`/login?next=${encodeURIComponent(here)}`, { replace: true });
	}

	onMount(() => {
		initTheme();
		setUnauthorizedHandler(toLogin);
		loadSession();
		if ('serviceWorker' in navigator && !import.meta.env.DEV)
			navigator.serviceWorker.register('/service-worker.js').catch(() => {});
	});

	// Gate every page but /login on a session, then open the live stream.
	$effect(() => {
		if (!session.ready || session.blocked) return;
		if (!session.authenticated) {
			if (!isLogin) toLogin();
			return;
		}
		connectLive();
	});

	// Ask before a navigation drops a form's unsaved edits.
	beforeNavigate((nav) => {
		if (!hasUnsaved()) return;
		if (nav.type === 'leave') nav.cancel(); // the browser shows its own prompt
		else if (!confirmDiscard()) nav.cancel();
	});

	async function signOut() {
		try {
			applySession(await api<Session>('/logout', { method: 'POST' }));
		} finally {
			toLogin();
		}
	}
</script>

<Toaster richColors closeButton position="bottom-right" mobileOffset={{ bottom: 'calc(4.5rem + env(safe-area-inset-bottom))' }} />

<Tooltip.Provider delayDuration={300}>
{#if !session.ready}
	<div class="grid min-h-dvh place-items-center"><Spinner class="size-6" /></div>
{:else if session.blocked}
	<main class="mx-auto grid min-h-dvh max-w-md place-items-center p-6 text-center">
		<div>
			<h1 class="text-xl font-semibold">ReoVault isn't ready yet</h1>
			<p class="text-muted-foreground mt-2 text-sm">{session.blocked}</p>
		</div>
	</main>
{:else if isLogin || !session.authenticated}
	{@render children()}
{:else}
	<div class="flex min-h-dvh flex-col pb-[calc(3.5rem+env(safe-area-inset-bottom))] lg:pb-0">
		<header class="bg-background/95 sticky top-0 z-40 border-b backdrop-blur">
			<div class="mx-auto flex h-16 max-w-[1180px] items-center gap-3 px-4 sm:px-6">
				<a href="/" class="mr-1 flex shrink-0 items-center" aria-label="ReoVault home">
					<img src="/icons/wordmark-dark.png" alt="ReoVault" class="hidden h-7 w-auto dark:block" />
					<img src="/icons/wordmark-light.png" alt="ReoVault" class="block h-7 w-auto dark:hidden" />
				</a>

				<nav class="hidden items-center gap-1 lg:flex" aria-label="Main">
					{#each nav as item (item.href)}
						<a
							href={item.href}
							aria-current={active(item.href) ? 'page' : undefined}
							class={cn(
								'text-muted-foreground hover:text-foreground hover:bg-muted flex h-9 items-center gap-2 rounded-md px-3 text-sm font-medium transition-colors',
								active(item.href) && 'bg-muted text-foreground'
							)}
						>
							<item.icon class="size-4" />
							{item.label}
						</a>
					{/each}
				</nav>

				<!-- Right: pages about the whole system, then the account menu. -->
				<div class="ml-auto flex shrink-0 items-center gap-1">
					<Button
						variant="ghost"
						size="icon"
						onclick={toggleTheme}
						aria-label={theme.value === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
					>
						{#if theme.value === 'dark'}<Sun />{:else}<Moon />{/if}
					</Button>
					<Button variant="ghost" size="icon" onclick={signOut} aria-label="Sign out">
						<LogOut />
					</Button>
				</div>
			</div>
			{#if live.everConnected && !live.connected}
				<p class="bg-warn text-on-warn px-4 py-1 text-center text-xs font-medium" role="status">
					Live updates paused, reconnecting. What you see may be out of date.
				</p>
			{/if}
		</header>

		<main class="mx-auto w-full max-w-[1180px] flex-1 px-4 py-6 sm:px-6 sm:py-8">
			{@render children()}
		</main>

		<footer
			class="text-subtle flex flex-wrap justify-center gap-x-4 gap-y-1 border-t px-4 py-4 text-xs sm:px-6"
		>
			<span>ReoVault {session.version}</span>
			<span>reolink-cli {session.pinnedCli}</span>
			<a href="https://wiebe-vandendriessche.github.io/reovault/" class="hover:text-foreground underline-offset-4 hover:underline" target="_blank" rel="noreferrer">Docs</a>
		</footer>

		<!-- Phones: the same five pages in a bottom bar. -->
		<nav
			class="bg-background/95 fixed inset-x-0 bottom-0 z-40 grid grid-cols-5 border-t pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden"
			aria-label="Main"
		>
			{#each nav as item (item.href)}
				<a
					href={item.href}
					aria-current={active(item.href) ? 'page' : undefined}
					class={cn(
						'text-muted-foreground flex min-h-14 flex-col items-center justify-center gap-1 text-[0.6875rem] font-medium',
						active(item.href) && 'text-foreground'
					)}
				>
					<item.icon class="size-5" />
					{item.label}
				</a>
			{/each}
		</nav>
	</div>
{/if}
</Tooltip.Provider>
