<script lang="ts">
	import type { Growth } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Chart from '#lib/components/ui/chart/index.js';
	import { BarChart } from 'layerchart';
	import { scaleBand } from 'd3-scale';
	import { bytes, count, shortDay } from '#lib/format.js';

	let { growth }: { growth: Growth } = $props();

	// No `color` in the config: ChartStyle would inject a <style> element for
	// it, which the CSP refuses. Series get their color directly instead.
	const config = { bytes: { label: 'Footage' } } satisfies Chart.ChartConfig;
	// Every 5th date under the bars, plus the last (today); the tooltip has all.
	const tick = (day: string) => {
		const i = growth.days.findIndex((d) => d.day === day);
		return i % 5 === 0 || i === growth.days.length - 1 ? shortDay(day) : '';
	};
</script>

<Card.Root class="md:col-span-2">
	<Card.Header>
		<Card.Description>Footage archived, last {growth.days.length} days</Card.Description>
		<Card.Title class="text-3xl tabular-nums">{bytes(growth.total_bytes)}</Card.Title>
		<Card.Action class="text-muted-foreground text-sm tabular-nums">
			{count(growth.total_clips)} clips
		</Card.Action>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		<Chart.Container {config} class="aspect-auto h-36 w-full">
			<BarChart
				data={growth.days}
				x="day"
				xScale={scaleBand().padding(0.2)}
				axis="x"
				rule={false}
				grid={false}
				series={[{ key: 'bytes', label: 'Footage', color: 'var(--brand)' }]}
				props={{ bars: { stroke: 'none', rounded: 'top' }, xAxis: { format: tick } }}
			>
				{#snippet tooltip()}
					<Chart.Tooltip labelFormatter={(d: string) => shortDay(d)}>
						{#snippet formatter({ value })}
							<span class="font-medium tabular-nums">{bytes(Number(value))}</span>
						{/snippet}
					</Chart.Tooltip>
				{/snippet}
			</BarChart>
		</Chart.Container>
		<dl class="grid grid-cols-2 gap-4 text-sm">
			<div>
				<dt class="text-subtle text-xs">Per day</dt>
				<dd class="font-medium tabular-nums">{bytes(growth.avg_per_day)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Per year, projected</dt>
				<dd class="font-medium tabular-nums">{bytes(growth.yearly_projection)}</dd>
			</div>
		</dl>
		<p class="text-subtle text-xs">By the day it was recorded. Disk space for the whole archive is on the Overview.</p>
	</Card.Content>
</Card.Root>
