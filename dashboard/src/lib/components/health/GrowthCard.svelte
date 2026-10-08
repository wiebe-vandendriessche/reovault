<script lang="ts">
	import type { Growth } from '$lib/api';
	import * as Card from '$lib/components/ui/card';
	import * as Chart from '$lib/components/ui/chart';
	import { BarChart } from 'layerchart';
	import { scaleBand } from 'd3-scale';
	import { bytes, count, shortDay } from '$lib/format';

	let { growth }: { growth: Growth } = $props();

	// No `color` in the config: ChartStyle would inject a <style> element for
	// it, which the CSP refuses. Series get their color directly instead.
	const config = { bytes: { label: 'Archived' } } satisfies Chart.ChartConfig;
	// Every 5th date under the bars, plus the last (today); the tooltip has all.
	const tick = (day: string) => {
		const i = growth.days.findIndex((d) => d.day === day);
		return i % 5 === 0 || i === growth.days.length - 1 ? shortDay(day) : '';
	};
</script>

<Card.Root class="md:col-span-2">
	<Card.Header>
		<Card.Description>Vault growth, last {growth.days.length} days</Card.Description>
		<Card.Title class="text-3xl tabular-nums">{bytes(growth.archived_bytes)}</Card.Title>
		<Card.Action class="text-muted-foreground text-sm tabular-nums">
			{count(growth.archived_count)} clips
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
				series={[{ key: 'bytes', label: 'Archived', color: 'var(--brand)' }]}
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
		<dl class="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
			<div>
				<dt class="text-subtle text-xs">Per day</dt>
				<dd class="font-medium tabular-nums">{bytes(growth.avg_per_day)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Per year, projected</dt>
				<dd class="font-medium tabular-nums">{bytes(growth.yearly_projection)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Disk free</dt>
				<dd class="font-medium tabular-nums">
					{growth.vault_free_bytes == null ? '-' : bytes(growth.vault_free_bytes)}
					{#if growth.days_headroom != null}
						<span class="text-muted-foreground font-normal">(~{count(growth.days_headroom)} days)</span>
					{/if}
				</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Vault vs. retention cap</dt>
				<dd class="font-medium tabular-nums">
					{bytes(growth.vault_used_bytes)}{growth.vault_cap_bytes ? ` / ${bytes(growth.vault_cap_bytes)}` : ''}
				</dd>
			</div>
		</dl>
	</Card.Content>
</Card.Root>
