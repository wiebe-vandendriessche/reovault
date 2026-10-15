<script lang="ts">
	import { api, type Overview } from '$lib/api';
	import { devices, deviceLabel } from '$lib/state/devices.svelte';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import NoDevice from '$lib/components/app/NoDevice.svelte';
	import ArchiveCard from '$lib/components/overview/ArchiveCard.svelte';
	import SdCardTile from '$lib/components/overview/SdCardTile.svelte';
	import { plural } from '$lib/format';
	import { cn } from '$lib/utils';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';

	const overview = query(() => {
		void live.activity;
		void live.problems;
		void live.devices;
		return api<Overview>('/overview');
	});

	// Worst card first, so the one that needs you is at the top.
	const rank: Record<string, number> = { not_mounted: 0, at_risk: 1, waiting: 3, all_archived: 4, unknown: 5 };
	const cards = $derived(
		[...(overview.data?.cards ?? [])].sort((a, b) => {
			const r = (c: typeof a) =>
				c.gateway_down || c.problems_count || c.card.lost_count ? Math.min(rank[c.card.state], 2) : rank[c.card.state];
			return r(a) - r(b) || (a.card.safe_for_seconds ?? Infinity) - (b.card.safe_for_seconds ?? Infinity);
		})
	);
	const allClear = $derived.by(() => {
		const n = cards.length;
		const archived = cards.filter((c) => c.card.state === 'all_archived').length;
		if (archived === n) return `Every clip on ${n === 1 ? 'the SD card' : `all ${n} SD cards`} is archived.`;
		const waiting = cards.reduce((sum, c) => sum + c.card.waiting_count, 0);
		const unknown = cards.filter((c) => c.card.state === 'unknown').length;
		return [
			`${archived} of ${n} cards fully archived`,
			waiting && `${plural(waiting, 'clip')} waiting, safe until the next run`,
			unknown && `${unknown} not sampled by a run yet`
		]
			.filter(Boolean)
			.join('; ') + '.';
	});
</script>

<svelte:head><title>Overview | ReoVault</title></svelte:head>

<PageHeader title="Overview" description="Is every SD card's footage safe, and is there room for more." />

{#if devices.loaded && devices.list.length === 0}
	<NoDevice />
{:else if overview.error}
	<ErrorAlert message={overview.error} />
{:else if !overview.data}
	<div class="flex flex-col gap-4">
		<Skeleton class="h-24 rounded-xl" />
		<div class="grid gap-4 md:grid-cols-2">
			{#each [0, 1] as i (i)}<Skeleton class="h-56 rounded-xl" />{/each}
		</div>
	</div>
{:else}
	{@const o = overview.data}
	<div class="flex flex-col gap-8">
		<section aria-labelledby="verdict-heading">
			{#if o.issues.length === 0}
				<div class="flex items-start gap-3">
					<ShieldCheck class="text-ok mt-0.5 size-6 shrink-0" aria-hidden="true" />
					<div>
						<h2 id="verdict-heading" class="text-xl font-semibold">Nothing needs attention</h2>
						<p class="text-muted-foreground mt-1 text-sm">{allClear}</p>
					</div>
				</div>
			{:else}
				<Card.Root class={o.issues.some((i) => i.level === 'bad') ? 'border-bad/40' : 'border-warn/40'}>
					<Card.Header>
						<Card.Title id="verdict-heading" class="text-xl">
							{o.issues.length === 1 ? '1 thing needs attention' : `${o.issues.length} things need attention`}
						</Card.Title>
						<Card.Description>Most urgent first.</Card.Description>
					</Card.Header>
					<Card.Content>
						<ul class="flex flex-col gap-4">
							{#each o.issues as issue, i (i)}
								{@const Icon = issue.level === 'bad' ? CircleAlert : TriangleAlert}
								<li class="flex flex-wrap items-center gap-x-3 gap-y-2">
									<Icon class={cn('size-4 shrink-0', issue.level === 'bad' ? 'text-bad' : 'text-warn')} aria-hidden="true" />
									<span class="min-w-0 flex-1 text-sm font-medium">
										<span class="sr-only">{issue.level === 'bad' ? 'Problem:' : 'Warning:'}</span>
										{issue.text}
									</span>
									<Button
										size="sm"
										variant="outline"
										href={issue.link === 'problems' ? `/problems?device=${issue.device_id}` : `/health?device=${issue.device_id}`}
										aria-label="{issue.link === 'problems' ? 'Review problems' : 'Open health'}: {issue.text}"
									>
										{issue.link === 'problems' ? 'Review' : 'Open'}
									</Button>
								</li>
							{/each}
						</ul>
					</Card.Content>
				</Card.Root>
			{/if}
		</section>

		<section class="flex flex-col gap-3" aria-labelledby="cards-heading">
			<h2 id="cards-heading" class="text-base font-semibold">SD cards</h2>
			<div class="grid gap-4 md:grid-cols-2">
				{#each cards as c (c.device_id)}
					<SdCardTile
						title={deviceLabel(c)}
						card={c.card}
						sample={c.sample}
						lastRunAt={c.last_run?.finished_at}
						nextRunAt={c.next_archive_utc}
						running={c.running}
						gatewayDown={c.gateway_down}
						deviceId={c.device_id}
						href="/health?device={c.device_id}"
					/>
				{/each}
			</div>
		</section>

		<ArchiveCard vault={o.vault} />
	</div>
{/if}
