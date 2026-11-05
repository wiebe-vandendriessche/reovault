<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type Camera, type Overview } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as NativeSelect from '#lib/components/ui/native-select/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import NoCamera from '#lib/components/app/NoCamera.svelte';
	import CameraTile from '#lib/components/overview/CameraTile.svelte';
	import DestinationCard from '#lib/components/overview/DestinationCard.svelte';
	import Info from '@lucide/svelte/icons/info';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';

	// How far back the cameras' coverage looks: since the SD card starts (the
	// default), a number of days, or everything. In the URL, like Footage's filters.
	const span = $derived(page.url.searchParams.get('span') ?? 'card');

	const overview = query(() => {
		void live.activity;
		void live.settings;
		void live.alert;
		return api<Overview>('/overview', { query: { span } });
	});

	// Offered only as far back as there is anything backed up.
	const PRESETS: [number, string][] = [
		[7, 'Last week'],
		[30, 'Last month'],
		[90, 'Last 3 months'],
		[180, 'Last 6 months'],
		[365, 'Last year'],
		[730, 'Last 2 years']
	];
	const spans = $derived.by(() => {
		const oldest = overview.data?.oldest_backup;
		const history = oldest ? (Date.now() - Date.parse(oldest)) / 86400000 : 0;
		const since = oldest
			? new Date(oldest).toLocaleDateString('en-GB', { month: 'short', year: 'numeric' })
			: '';
		const out = [
			{ value: 'card', label: 'Since the SD card starts' },
			...PRESETS.filter(([d]) => d < history).map(([d, label]) => ({ value: String(d), label })),
			{ value: 'all', label: since ? `Everything (since ${since})` : 'Everything' }
		];
		if (!out.some((o) => o.value === span)) out.splice(1, 0, { value: span, label: `Last ${span} days` });
		return out;
	});

	const LEGEND = [
		{ label: 'Backed up', class: 'fill-ok' },
		{ label: 'Waiting', class: 'fill-brand' },
		{ label: 'Keeps failing', class: 'fill-bad' },
		{ label: 'Lost', class: 'fill-warn' }
	];

	// Worst first, so the camera that needs you is at the top.
	const rank: Record<Camera['verdict'], number> = {
		offline: 0,
		not_mounted: 1,
		failing: 2,
		at_risk: 3,
		catching_up: 4,
		unknown: 5,
		up_to_date: 6
	};
	const cameras = $derived(
		[...(overview.data?.cameras ?? [])].sort((a, b) => rank[a.verdict] - rank[b.verdict])
	);

	function replace(c: Camera) {
		if (!overview.data) return;
		// Run/stop answers without coverage: keep what the card already shows.
		overview.data.cameras = overview.data.cameras.map((x) =>
			x.id === c.id ? { ...c, coverage: c.coverage ?? x.coverage } : x
		);
	}
</script>

<svelte:head><title>Overview | ReoVault</title></svelte:head>

<PageHeader title="Overview" description="Is every SD card backed up, and where do the backups go." />

{#if overview.error}
	<ErrorAlert message={overview.error} />
{:else if !overview.data}
	<div class="flex flex-col gap-4">
		<Skeleton class="h-24 rounded-xl" />
		<div class="grid gap-4 md:grid-cols-2">
			{#each [0, 1] as i (i)}<Skeleton class="h-56 rounded-xl" />{/each}
		</div>
	</div>
{:else if overview.data.cameras.length === 0}
	<NoCamera />
{:else}
	{@const o = overview.data}
	<div class="flex flex-col gap-8">
		<section aria-labelledby="verdict-heading">
			{#if o.issues.length === 0}
				<div class="flex items-start gap-3">
					<ShieldCheck class="text-ok mt-0.5 size-6 shrink-0" aria-hidden="true" />
					<div>
						<h2 id="verdict-heading" class="text-xl font-semibold">Nothing needs attention</h2>
						<p class="text-muted-foreground mt-1 text-sm">
							{cameras.every((c) => c.verdict === 'up_to_date')
								? 'Every clip on every SD card is backed up.'
								: 'Backups are running normally.'}
						</p>
					</div>
				</div>
			{:else}
				<Card.Root class="border-warn/40">
					<Card.Header>
						<Card.Title id="verdict-heading" class="text-xl">
							{o.issues.length === 1 ? '1 thing needs attention' : `${o.issues.length} things need attention`}
						</Card.Title>
					</Card.Header>
					<Card.Content>
						<ul class="flex flex-col gap-4">
							{#each o.issues as issue, i (i)}
								<li class="flex items-start gap-3">
									<TriangleAlert class="text-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
									<div class="min-w-0">
										<p class="text-sm font-medium">{issue.title}</p>
										<p class="text-muted-foreground text-sm">{issue.message}</p>
									</div>
								</li>
							{/each}
						</ul>
					</Card.Content>
				</Card.Root>
			{/if}
		</section>

		{#if o.notices.length}
			<ul class="flex flex-col gap-2">
				{#each o.notices as n, i (i)}
					<li class="bg-muted/50 flex items-start gap-3 rounded-lg border px-4 py-3">
						<Info class="text-subtle mt-0.5 size-4 shrink-0" aria-hidden="true" />
						<div class="min-w-0 text-sm">
							<p class="font-medium">{n.title}</p>
							<p class="text-muted-foreground">{n.message}</p>
						</div>
					</li>
				{/each}
			</ul>
		{/if}

		<section class="flex flex-col gap-3" aria-labelledby="cameras-heading">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<h2 id="cameras-heading" class="text-base font-semibold">Cameras</h2>
				<NativeSelect.Root
					value={span}
					onchange={(e: Event) =>
						goto(`?span=${(e.currentTarget as HTMLSelectElement).value}`, { replace: true, reset: false })}
					aria-label="How far back"
					class="min-w-52"
				>
					{#each spans as o (o.value)}
						<NativeSelect.Option value={o.value}>{o.label}</NativeSelect.Option>
					{/each}
				</NativeSelect.Root>
			</div>
			<div class="grid gap-4 md:grid-cols-2">
				{#each cameras as cam (cam.id)}
					<CameraTile {cam} onchange={replace} />
				{/each}
			</div>
			<ul class="text-subtle flex flex-wrap gap-x-4 gap-y-1 text-xs" aria-label="Colours">
				{#each LEGEND as l (l.label)}
					<li class="flex items-center gap-1.5">
						<svg class="size-2.5" aria-hidden="true"><circle cx="50%" cy="50%" r="50%" class={l.class} /></svg>
						{l.label}
					</li>
				{/each}
				<li class="flex items-center gap-1.5">
					<svg class="h-3 w-0.5" aria-hidden="true"><rect width="100%" height="100%" class="fill-foreground" /></svg>
					Where the SD card starts
				</li>
			</ul>
		</section>

		<section class="flex flex-col gap-3" aria-labelledby="dest-heading">
			<div class="flex items-center justify-between">
				<h2 id="dest-heading" class="text-base font-semibold">Destinations</h2>
				<Button size="sm" variant="ghost" href="/settings?tab=destinations">Manage</Button>
			</div>
			<div class="grid gap-4 md:grid-cols-2">
				{#each o.destinations as d (d.name)}<DestinationCard destination={d} />{/each}
			</div>
			{#if o.destinations.length === 0}
				<p class="text-muted-foreground text-sm">
					No destination yet. <a class="underline" href="/settings?tab=destinations">Connect one</a>.
				</p>
			{/if}
		</section>
	</div>
{/if}
