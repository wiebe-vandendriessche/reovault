<script lang="ts">
	import type { components } from '$lib/api/schema';
	import * as Card from '$lib/components/ui/card';
	import StatusBadge from '$lib/components/app/StatusBadge.svelte';
	import * as Chart from '$lib/components/ui/chart';
	import { Badge } from '$lib/components/ui/badge';
	import Legend from '$lib/components/app/Legend.svelte';
	import CardTimeline from './CardTimeline.svelte';
	import { ArcChart } from 'layerchart';
	import { plural, relative, span } from '$lib/format';
	import { cn } from '$lib/utils';

	type S = components['schemas'];
	type Level = 'ok' | 'warn' | 'bad' | 'idle';

	/** One camera's SD card: is everything on it archived, and if not, how
	 * long until the oldest unarchived clip is overwritten. */
	let {
		title,
		card,
		sample,
		lastRunAt,
		nextRunAt,
		running = false,
		gatewayDown = false,
		onclick,
		class: className
	}: {
		title: string;
		card: S['CardOut'];
		sample: S['StorageSampleOut'] | null;
		lastRunAt?: string | null;
		nextRunAt?: string | null;
		running?: boolean;
		gatewayDown?: boolean;
		onclick?: () => void;
		class?: string;
	} = $props();

	const verdict = $derived.by((): { level: Level; badge: string; text: string } => {
		const n = card.waiting_count;
		switch (card.state) {
			case 'not_mounted':
				return { level: 'bad', badge: 'Not mounted', text: 'The camera reports no mounted SD card.' };
			case 'at_risk':
				return {
					level: 'warn',
					badge: 'At risk',
					text: `${plural(n, 'clip')} not archived; the oldest is overwritten in about ${span(card.safe_for_seconds)}.`
				};
			case 'waiting':
				return {
					level: 'ok',
					badge: `${n} waiting`,
					text:
						card.safe_for_seconds != null
							? `${plural(n, 'clip')} waiting; safe for about ${span(card.safe_for_seconds)}.`
							: `${plural(n, 'clip')} waiting for the next run.`
				};
			case 'all_archived':
				return { level: 'ok', badge: 'All archived', text: 'Every clip on the card is archived.' };
			default:
				return { level: 'idle', badge: 'No data yet', text: 'No archive run has sampled this card yet.' };
		}
	});

	const used = $derived(card.used_fraction);
	const fill = $derived([{ key: 'used', value: Math.round((used ?? 0) * 100) }]);
	const fillColor = $derived(used == null ? 'var(--idle)' : used > 0.97 ? 'var(--warn)' : 'var(--brand)');
	const runs = $derived(
		running
			? 'Running now'
			: [lastRunAt && `Last run ${relative(lastRunAt)}`, nextRunAt && `next ${relative(nextRunAt)}`]
					.filter(Boolean)
					.join(', ')
					.replace(/^n/, 'N')
	);
	const windowDays = $derived(
		card.window_start_utc ? (Date.now() - Date.parse(card.window_start_utc)) / 86400000 : null
	);
</script>

<Card.Root
	class={cn(onclick && 'hover:border-brand-text cursor-pointer transition-colors', className)}
	{onclick}
	role={onclick ? 'link' : undefined}
	tabindex={onclick ? 0 : undefined}
	onkeydown={(e: KeyboardEvent) => onclick && e.key === 'Enter' && onclick()}
>
	<Card.Header>
		<Card.Description>SD card</Card.Description>
		<Card.Title class="text-lg">{title}</Card.Title>
		<Card.Action class="flex flex-col items-end gap-1">
			<StatusBadge level={verdict.level}>{verdict.badge}</StatusBadge>
			{#if card.lost_count}
				<Badge href="/problems" variant="destructive" onclick={(e: MouseEvent) => e.stopPropagation()}>
					{plural(card.lost_count, 'clip')} lost
				</Badge>
			{/if}
		</Card.Action>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		<div class="flex items-center gap-4">
			<div class="relative size-16 shrink-0" role="img" aria-label={used == null ? 'Card fill unknown' : `Card ${fill[0].value}% full`}>
				<Chart.Container config={{ used: { label: 'Full' } }} class="aspect-square size-16">
					<ArcChart
						data={fill}
						key="key"
						value="value"
						maxValue={100}
						innerRadius={-7}
						cornerRadius={4}
						series={[{ key: 'used', color: fillColor, data: fill }]}
						props={{ arc: { track: { fill: 'var(--muted)' } } }}
					/>
				</Chart.Container>
				<span class="absolute inset-0 grid place-items-center text-xs font-semibold tabular-nums">
					{used == null ? '?' : `${fill[0].value}%`}
				</span>
			</div>
			<div class="flex min-w-0 flex-col gap-0.5">
				<p class="text-sm font-medium">{verdict.text}</p>
				<p class="text-muted-foreground text-xs">
					{#if sample?.total_gb}{sample.total_gb.toFixed(0)} GB card{/if}{#if windowDays != null}, holds {card.window_estimated ? '~' : ''}{Math.max(1, Math.round(windowDays))} days of footage{/if}
				</p>
			</div>
		</div>
		<CardTimeline buckets={card.timeline} estimated={card.window_estimated} />
		<div class="flex flex-wrap items-center gap-x-4 gap-y-2">
			{#if card.timeline.length}
				<Legend
					items={[
						{ label: 'Archived', class: 'bg-ok' },
						{ label: 'Waiting', class: 'bg-warn' },
						{ label: 'Failed', class: 'bg-bad' }
					]}
				/>
			{/if}
			<span class="text-muted-foreground ml-auto text-xs">{runs}</span>
			{#if gatewayDown}<StatusBadge level="warn">Camera gateway unreachable</StatusBadge>{/if}
		</div>
	</Card.Content>
</Card.Root>
