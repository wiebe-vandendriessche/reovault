<script lang="ts">
	import './layout.css';
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount, untrack } from 'svelte';
	import { api, setUnauthorizedHandler, type Session } from '$lib/api';
	import { applySession, loadSession, session } from '$lib/state/session.svelte';
	import { devices, deviceLabel, loadDevices, selectDevice } from '$lib/state/devices.svelte';
	import { connectLive, disconnectLive, live } from '$lib/state/live.svelte';
	import { initTheme, theme, toggleTheme } from '$lib/state/theme.svelte';
	import { confirmDiscard, hasUnsaved } from '$lib/state/unsaved.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import * as Sheet from '$lib/components/ui/sheet';
	import { Toaster } from '$lib/components/ui/sonner';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { Spinner } from '$lib/components/ui/spinner';
	import { cn } from '$lib/utils';
	import Activity from '@lucide/svelte/icons/activity';
	import LayoutDashboard from '@lucide/svelte/icons/layout-dashboard';
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
		{ href: '/health', label: 'Health', icon: Activity, desc: "This camera's SD card, runs, and today's footage." },
		{ href: '/footage', label: 'Footage', icon: Video, desc: 'Browse and play archived clips.' },
		{ href: '/runs', label: 'Runs', icon: ListChecks, desc: 'History of every archive run.' },
		{
			href: '/problems',
			label: 'Problems',
			icon: TriangleAlert,
			desc: 'Recordings that need attention.'
		}
	];
	const home = { href: '/', label: 'Overview', icon: LayoutDashboard, desc: 'The archive disk and every SD card.' };
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
		const fromUrl = untrack(() => urlDevice());
		loadDevices(fromUrl).catch(() => {});
		connectLive();
	});

	// Re-read the camera list (and its per-camera problem counts) when the
	// server says the fleet or its problems changed.
	$effect(() => {
		if ((live.devices || live.problems) && session.authenticated) loadDevices().catch(() => {});
	});

	// The camera-scoped pages carry the camera in the URL, so a link (from
	// an Overview tile, an alert, a second tab) opens the right one. The URL
	// wins; a scoped page opened without one gets the current camera added.
	const urlDevice = () => Number(page.url.searchParams.get('device')) || null;
	const onScoped = $derived(scoped.some((s) => active(s.href)));
	$effect(() => {
		const id = urlDevice();
		if (id != null && id !== untrack(() => devices.currentId) && devices.list.some((d) => d.id === id))
			selectDevice(id);
	});
	$effect(() => {
		const id = devices.currentId;
		if (!onScoped || id == null || untrack(urlDevice) != null) return;
		const url = new URL(page.url);
		url.searchParams.set('device', String(id));
		goto(url, { replaceState: true, noScroll: true, keepFocus: true });
	});

	function pickDevice(id: number) {
		if (!onScoped) return selectDevice(id);
		const url = new URL(page.url);
		url.searchParams.set('device', String(id));
		goto(url, { noScroll: true, keepFocus: true });
	}

	const scopedHref = (href: string) =>
		devices.currentId == null ? href : `${href}?device=${devices.currentId}`;

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

	const current = $derived(devices.list.find((d) => d.id === devices.currentId));
	const fleetProblems = $derived(devices.list.reduce((n, d) => n + d.problems, 0));
	const otherProblems = $derived(fleetProblems - (current?.problems ?? 0));
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

				<!-- Left: one group for the current camera and its pages. -->
				<div class="hidden min-w-0 items-center gap-0.5 rounded-lg border p-0.5 lg:flex">
					{#if devices.list.length > 1 && current}
						<DropdownMenu.Root>
							<DropdownMenu.Trigger>
								{#snippet child({ props })}
									<Button
										{...props}
										variant="ghost"
										size="sm"
										class="h-8 max-w-44 min-w-0 shrink gap-2"
										aria-label="Camera: {deviceLabel(current)}{otherProblems ? `, ${otherProblems} problems on other cameras` : ''}"
									>
										<Cctv class="size-4" />
										<span class="truncate">{deviceLabel(current)}</span>
										{#if otherProblems}
											<span class="bg-bad size-2 shrink-0 rounded-full" aria-hidden="true"></span>
										{/if}
										<ChevronsUpDown class="text-muted-foreground size-3.5" />
									</Button>
								{/snippet}
							</DropdownMenu.Trigger>
							<DropdownMenu.Content align="start" class="w-60">
								<DropdownMenu.Label>Camera</DropdownMenu.Label>
								{#each devices.list as d (d.id)}
									<DropdownMenu.Item onSelect={() => pickDevice(d.id)}>
										<Check class={cn('size-4', d.id !== devices.currentId && 'invisible')} />
										<span class="truncate">{deviceLabel(d)}</span>
										{#if d.problems}
											<span class="text-bad ml-auto text-xs tabular-nums" aria-label="{d.problems} problems">{d.problems}</span>
										{/if}
									</DropdownMenu.Item>
								{/each}
							</DropdownMenu.Content>
						</DropdownMenu.Root>
					{:else if current}
						<span class="text-muted-foreground flex h-8 max-w-44 min-w-0 items-center gap-2 px-2.5 text-sm font-medium">
							<Cctv class="size-4 shrink-0" />
							<span class="truncate">{deviceLabel(current)}</span>
						</span>
					{/if}
					<div class="bg-border mx-0.5 h-5 w-px shrink-0" aria-hidden="true"></div>
					<nav class="flex items-center gap-0.5" aria-label="Camera">
						{#each scoped as item (item.href)}
							<a
								href={scopedHref(item.href)}
								aria-current={active(item.href) ? 'page' : undefined}
								class={cn(
									'text-muted-foreground hover:text-foreground hover:bg-muted flex h-8 shrink-0 items-center gap-2 rounded-md px-2.5 text-sm font-medium transition-colors',
									active(item.href) && 'bg-muted text-foreground'
								)}
							>
								<item.icon class="size-4" />
								{item.label}
								{#if item.href === '/problems' && current?.problems}
									<span class="bg-bad text-background rounded-full px-1.5 text-xs font-semibold tabular-nums">
										{current.problems}
									</span>
								{/if}
							</a>
						{/each}
					</nav>
				</div>

				<!-- Right: pages about the whole system, then the account menu. -->
				<div class="ml-auto flex shrink-0 items-center gap-1">
					<nav class="hidden items-center gap-1 lg:flex" aria-label="System">
						{#each [home, ...fleet] as item (item.href)}
							<Tooltip.Root>
								<Tooltip.Trigger>
									{#snippet child({ props })}
										<a
											{...props}
											href={item.href}
											aria-current={active(item.href) ? 'page' : undefined}
											aria-label={item.label}
											class={cn(
												'text-muted-foreground hover:text-foreground hover:bg-muted flex h-9 items-center gap-2 rounded-md px-2.5 text-sm font-medium transition-colors xl:px-3',
												active(item.href) && 'bg-muted text-foreground'
											)}
										>
											<item.icon class="size-4" />
											<span class="hidden xl:inline">{item.label}</span>
										</a>
									{/snippet}
								</Tooltip.Trigger>
								<Tooltip.Content class="xl:hidden">{item.label}</Tooltip.Content>
							</Tooltip.Root>
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

		<!-- Phones: fleet-wide pages in the bar, the camera's pages under Menu. -->
		<nav
			class="bg-background/95 fixed inset-x-0 bottom-0 z-40 grid grid-cols-4 border-t pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden"
			aria-label="Main"
		>
			{#each [home, ...fleet] as item (item.href)}
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
			<Sheet.Root bind:open={menuOpen}>
				<Sheet.Trigger>
					{#snippet child({ props })}
						<button
							{...props}
							class={cn(
								'text-muted-foreground relative flex min-h-14 flex-col items-center justify-center gap-1 text-[0.6875rem] font-medium',
								onScoped && 'text-foreground'
							)}
							aria-label={fleetProblems ? `Menu, ${fleetProblems} problems` : 'Menu'}
						>
							<Menu class="size-5" />
							Menu
							{#if fleetProblems}
								<span class="bg-bad absolute top-2 left-1/2 ml-2 size-2 rounded-full" aria-hidden="true"></span>
							{/if}
						</button>
					{/snippet}
				</Sheet.Trigger>
				<Sheet.Content side="right" class="w-80 overflow-y-auto">
					<Sheet.Header>
						<Sheet.Title>Camera</Sheet.Title>
					</Sheet.Header>
					{#if devices.list.length > 1}
						<div class="flex flex-col gap-1 px-4" role="radiogroup" aria-label="Camera">
							{#each devices.list as d (d.id)}
								<button
									role="radio"
									aria-checked={d.id === devices.currentId}
									onclick={() => {
										pickDevice(d.id);
										if (onScoped) menuOpen = false;
									}}
									class={cn(
										'hover:bg-muted flex min-h-11 items-center gap-3 rounded-md px-3 text-left text-sm font-medium',
										d.id === devices.currentId && 'bg-muted'
									)}
								>
									<Check class={cn('size-4 shrink-0', d.id !== devices.currentId && 'invisible')} />
									<span class="truncate">{deviceLabel(d)}</span>
									{#if d.problems}
										<span class="text-bad ml-auto text-xs font-semibold tabular-nums" aria-label="{d.problems} problems">{d.problems}</span>
									{/if}
								</button>
							{/each}
						</div>
						<div class="bg-border mx-4 my-2 h-px" aria-hidden="true"></div>
					{:else if current}
						<p class="text-muted-foreground flex items-center gap-2 px-7 pb-2 text-sm">
							<Cctv class="size-4" />
							<span class="truncate">{deviceLabel(current)}</span>
						</p>
					{/if}
					<nav class="flex flex-col gap-1 px-4" aria-label="Camera pages">
						{#each scoped as item (item.href)}
							<a
								href={scopedHref(item.href)}
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
								{#if item.href === '/problems' && current?.problems}
									<span class="text-bad ml-auto text-xs font-semibold tabular-nums">{current.problems}</span>
								{/if}
							</a>
						{/each}
					</nav>
				</Sheet.Content>
			</Sheet.Root>
		</nav>
	</div>
{/if}
</Tooltip.Provider>
