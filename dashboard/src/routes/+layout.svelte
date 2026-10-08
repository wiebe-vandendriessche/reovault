<script lang="ts">
	import './layout.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { api, setUnauthorizedHandler, type Session } from '$lib/api';
	import { applySession, loadSession, session } from '$lib/state/session.svelte';
	import { devices, deviceLabel, loadDevices, selectDevice } from '$lib/state/devices.svelte';
	import { connectLive, disconnectLive, live } from '$lib/state/live.svelte';
	import { initTheme, theme, toggleTheme } from '$lib/state/theme.svelte';
	import { query } from '$lib/state/query.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import * as Sheet from '$lib/components/ui/sheet';
	import { Toaster } from '$lib/components/ui/sonner';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { Spinner } from '$lib/components/ui/spinner';
	import { cn } from '$lib/utils';
	import Activity from '@lucide/svelte/icons/activity';
	import Video from '@lucide/svelte/icons/video';
	import ListChecks from '@lucide/svelte/icons/list-checks';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import Cctv from '@lucide/svelte/icons/cctv';
	import Settings from '@lucide/svelte/icons/settings';
	import Menu from '@lucide/svelte/icons/menu';
	import Moon from '@lucide/svelte/icons/moon';
	import Sun from '@lucide/svelte/icons/sun';
	import LogOut from '@lucide/svelte/icons/log-out';
	import ChevronsUpDown from '@lucide/svelte/icons/chevrons-up-down';
	import Check from '@lucide/svelte/icons/check';

	let { children } = $props();

	const scoped = [
		{ href: '/', label: 'Health', icon: Activity, desc: "Status, coverage, and today's footage." },
		{ href: '/footage', label: 'Footage', icon: Video, desc: 'Browse and play archived clips.' },
		{ href: '/runs', label: 'Runs', icon: ListChecks, desc: 'History of every archive run.' },
		{
			href: '/problems',
			label: 'Problems',
			icon: TriangleAlert,
			desc: 'Recordings that need attention.'
		}
	];
	const fleet = [
		{ href: '/devices', label: 'Devices', icon: Cctv, desc: 'Cameras, their settings, and adding one.' },
		{ href: '/settings', label: 'Settings', icon: Settings, desc: 'Schedule, retention, alerts, system.' }
	];

	let menuOpen = $state(false);
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
			goto(`/login?next=${encodeURIComponent(here)}`, { replaceState: true });
	}

	onMount(() => {
		initTheme();
		setUnauthorizedHandler(toLogin);
		loadSession();
		if ('serviceWorker' in navigator && !import.meta.env.DEV)
			navigator.serviceWorker.register('/service-worker.js').catch(() => {});
	});

	// Gate every page but /login on a session, then start the app's data.
	$effect(() => {
		if (!session.ready || session.blocked) return;
		if (!session.authenticated) {
			if (!isLogin) toLogin();
			return;
		}
		const fromUrl = Number(page.url.searchParams.get('device')) || null;
		loadDevices(fromUrl).catch(() => {});
		connectLive();
	});

	// Re-read the camera list when the server says the fleet changed.
	$effect(() => {
		if (live.devices && session.authenticated) loadDevices().catch(() => {});
	});

	const problems = query(() => {
		void live.problems;
		const device = devices.currentId;
		if (!session.authenticated || device == null) return null;
		return api<{ count: number }>('/problems/count', { query: { device } });
	});

	async function signOut() {
		try {
			applySession(await api<Session>('/logout', { method: 'POST' }));
		} finally {
			toLogin();
		}
	}

	const current = $derived(devices.list.find((d) => d.id === devices.currentId));
</script>

<Toaster richColors closeButton position="bottom-right" />

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
	<div class="flex min-h-dvh flex-col">
		<header class="bg-background/95 sticky top-0 z-40 border-b backdrop-blur">
			<div class="mx-auto flex h-16 max-w-[1180px] items-center gap-3 px-4 sm:px-6">
				<a href="/" class="mr-2 flex shrink-0 items-center" aria-label="ReoVault home">
					<img src="/icons/wordmark-dark.png" alt="ReoVault" class="hidden h-7 w-auto dark:block" />
					<img src="/icons/wordmark-light.png" alt="ReoVault" class="block h-7 w-auto dark:hidden" />
				</a>

				<nav class="hidden items-center gap-1 lg:flex" aria-label="Camera">
					{#each scoped as item (item.href)}
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
							{#if item.href === '/problems' && problems.data?.count}
								<span class="bg-bad text-background rounded-full px-1.5 text-xs font-semibold tabular-nums">
									{problems.data.count}
								</span>
							{/if}
						</a>
					{/each}
				</nav>

				{#if devices.list.length > 1 && current}
					<DropdownMenu.Root>
						<DropdownMenu.Trigger>
							{#snippet child({ props })}
								<Button {...props} variant="outline" size="sm" class="max-w-48 gap-2">
									<Cctv class="size-4" />
									<span class="truncate">{deviceLabel(current)}</span>
									<ChevronsUpDown class="text-muted-foreground size-3.5" />
								</Button>
							{/snippet}
						</DropdownMenu.Trigger>
						<DropdownMenu.Content align="start" class="w-60">
							<DropdownMenu.Label>Camera</DropdownMenu.Label>
							{#each devices.list as d (d.id)}
								<DropdownMenu.Item onSelect={() => selectDevice(d.id)}>
									<Check class={cn('size-4', d.id !== devices.currentId && 'invisible')} />
									<span class="truncate">{deviceLabel(d)}</span>
									{#if d.problems}
										<span class="text-bad ml-auto text-xs tabular-nums">{d.problems}</span>
									{/if}
								</DropdownMenu.Item>
							{/each}
						</DropdownMenu.Content>
					</DropdownMenu.Root>
				{/if}

				<div class="ml-auto flex items-center gap-1">
					<nav class="hidden items-center gap-1 lg:flex" aria-label="Fleet">
						{#each fleet as item (item.href)}
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
					<Button
						variant="ghost"
						size="icon"
						onclick={toggleTheme}
						aria-label={theme.value === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
					>
						{#if theme.value === 'dark'}<Sun />{:else}<Moon />{/if}
					</Button>
					<Button variant="ghost" size="icon" class="hidden lg:inline-flex" onclick={signOut} aria-label="Sign out">
						<LogOut />
					</Button>

					<Sheet.Root bind:open={menuOpen}>
						<Sheet.Trigger>
							{#snippet child({ props })}
								<Button {...props} variant="ghost" size="icon" class="lg:hidden" aria-label="Menu">
									<Menu />
								</Button>
							{/snippet}
						</Sheet.Trigger>
						<Sheet.Content side="right" class="w-80">
							<Sheet.Header>
								<Sheet.Title>Menu</Sheet.Title>
							</Sheet.Header>
							<nav class="flex flex-col gap-1 px-4">
								{#each [...scoped, ...fleet] as item (item.href)}
									<a
										href={item.href}
										onclick={() => (menuOpen = false)}
										aria-current={active(item.href) ? 'page' : undefined}
										class={cn(
											'hover:bg-muted flex min-h-11 items-start gap-3 rounded-md p-3',
											active(item.href) && 'bg-muted'
										)}
									>
										<item.icon class="mt-0.5 size-4 shrink-0" />
										<span class="flex flex-col">
											<span class="text-sm font-medium">{item.label}</span>
											<span class="text-subtle text-xs">{item.desc}</span>
										</span>
										{#if item.href === '/problems' && problems.data?.count}
											<span class="text-bad ml-auto text-xs font-semibold tabular-nums">
												{problems.data.count}
											</span>
										{/if}
									</a>
								{/each}
								<button
									class="hover:bg-muted mt-2 flex min-h-11 items-center gap-3 rounded-md p-3 text-left text-sm font-medium"
									onclick={signOut}
								>
									<LogOut class="size-4" /> Sign out
								</button>
							</nav>
						</Sheet.Content>
					</Sheet.Root>
				</div>
			</div>
		</header>

		<main class="mx-auto w-full max-w-[1180px] flex-1 px-4 py-6 sm:px-6 sm:py-8">
			{@render children()}
		</main>

		<footer
			class="text-subtle flex flex-wrap justify-center gap-x-4 gap-y-1 border-t px-4 py-4 text-xs sm:px-6"
		>
			<span>ReoVault {session.version}</span>
			<span>reolink-cli {session.pinnedCli}</span>
			{#if !live.connected}
				<span class="text-warn">Live updates reconnecting</span>
			{/if}
		</footer>
	</div>
{/if}
</Tooltip.Provider>
