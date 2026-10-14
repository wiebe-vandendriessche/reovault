<script lang="ts">
	import { api } from '$lib/api';
	import type { components } from '$lib/api/schema';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Chart from '$lib/components/ui/chart';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { BarChart } from 'layerchart';
	import { scaleBand } from 'd3-scale';
	import { plural, shortDay } from '$lib/format';

	type TypeStats = components['schemas']['TypeStatsOut'];

	let { device }: { device: number } = $props();

	const stats = query(() => {
		void live.activity;
		return api<TypeStats>('/stats/types', { query: { device, days: 30 } });
	});

	// Stable colors for the types Reolink reports most; anything else cycles.
	const known: Record<string, string> = {
		people: 'var(--chart-1)',
		vehicle: 'var(--chart-2)',
		md: 'var(--chart-3)',
		visitor: 'var(--chart-4)',
		dog_cat: 'var(--chart-5)',
		other: 'var(--idle)'
	};
	const cycle = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)'];
	const colorOf = (t: string, i: number) => known[t] ?? cycle[i % cycle.length];

	const data = $derived(
		(stats.data?.days ?? []).map((d) => ({
			day: d.day,
			...Object.fromEntries((stats.data?.types ?? []).map((t) => [t, d.counts[t] ?? 0]))
		}))
	);
	const series = $derived(
		(stats.data?.types ?? []).map((t, i) => ({ key: t, label: t, color: colorOf(t, i) }))
	);
	const total = $derived(
		(stats.data?.days ?? []).reduce((n, d) => n + Object.values(d.counts).reduce((a, b) => a + b, 0), 0)
	);
	const config = $derived(
		Object.fromEntries(series.map((s) => [s.key, { label: s.label }])) satisfies Chart.ChartConfig
	);
	const tick = (day: string) => {
		const i = data.findIndex((d) => d.day === day);
		return i % 5 === 0 || i === data.length - 1 ? shortDay(day) : '';
	};
</script>

<Card.Root class="md:col-span-2">
	<Card.Header>
		<Card.Description>Detections, last 30 days</Card.Description>
		<Card.Title class="text-3xl tabular-nums">{stats.data ? plural(total, 'clip') : ''}</Card.Title>
	</Card.Header>
	<Card.Content>
		{#if !stats.data}
			<Skeleton class="h-48 w-full" />
		{:else if total === 0}
			<p class="text-muted-foreground text-sm">No clips in the last 30 days.</p>
		{:else}
			<Chart.Container {config} class="aspect-auto h-48 w-full">
				<BarChart
					{data}
					x="day"
					xScale={scaleBand().padding(0.2)}
					axis="x"
					rule={false}
					grid={false}
					{series}
					seriesLayout="stack"
					legend
					props={{ bars: { stroke: 'none' }, xAxis: { format: tick } }}
				>
					{#snippet tooltip()}
						<Chart.Tooltip labelFormatter={(d: string) => shortDay(d)} />
					{/snippet}
				</BarChart>
			</Chart.Container>
		{/if}
	</Card.Content>
</Card.Root>
