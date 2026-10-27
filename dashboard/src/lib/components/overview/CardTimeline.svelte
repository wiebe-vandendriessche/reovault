<script lang="ts">
	import type { components } from '#lib/api/schema.js';
	import * as Chart from '#lib/components/ui/chart/index.js';
	import { BarChart } from 'layerchart';
	import { scaleBand } from 'd3-scale';

	type Bucket = components['schemas']['TimelineBucketOut'];

	/** What the SD card holds, oldest clip (left) to now (right): one stacked
	 * bar per time slice, clip count by archive state. */
	let { buckets, estimated = false }: { buckets: Bucket[]; estimated?: boolean } = $props();

	// No `color` in the config: ChartStyle would inject a <style> element for
	// it, which the CSP refuses. Series get their color directly instead.
	const config = {
		archived: { label: 'Archived' },
		waiting: { label: 'Waiting' },
		quarantined: { label: 'Failed' }
	} satisfies Chart.ChartConfig;
	const series = [
		{ key: 'archived', label: 'Archived', color: 'var(--ok)' },
		{ key: 'waiting', label: 'Waiting', color: 'var(--warn)' },
		{ key: 'quarantined', label: 'Failed', color: 'var(--bad)' }
	];

	const day = (iso: string) => new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
	const when = (iso: string) =>
		new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
</script>

{#if buckets.length}
	<figure class="flex flex-col gap-1">
		<Chart.Container {config} class="aspect-auto h-16 w-full">
			<BarChart
				data={buckets}
				x="start_utc"
				xScale={scaleBand().padding(0.15)}
				axis={false}
				rule={false}
				grid={false}
				{series}
				seriesLayout="stack"
				props={{ bars: { stroke: 'none' } }}
			>
				{#snippet tooltip()}
					<Chart.Tooltip labelFormatter={(iso: string) => when(iso)} />
				{/snippet}
			</BarChart>
		</Chart.Container>
		<figcaption class="text-subtle flex justify-between text-xs">
			<span>{estimated ? '~' : ''}{day(buckets[0].start_utc)}</span>
			<span>now</span>
		</figcaption>
	</figure>
{:else}
	<p class="text-subtle text-xs">No clips on the card yet.</p>
{/if}
