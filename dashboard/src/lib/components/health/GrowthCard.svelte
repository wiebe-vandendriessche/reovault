<script lang="ts">
	import type { Growth } from '$lib/api';
	import * as Card from '$lib/components/ui/card';
	import { bytes } from '$lib/format';

	let { growth }: { growth: Growth } = $props();

	// Bars as SVG geometry (percent of the viewBox), never inline styles.
	const bars = $derived.by(() => {
		const values = growth.days.map((d) => d.bytes);
		const peak = Math.max(1, ...values);
		const slot = 100 / Math.max(1, values.length);
		return growth.days.map((d, i) => {
			const h = Math.max((d.bytes / peak) * 100, d.bytes ? 6 : 3);
			return { x: i * slot + slot * 0.1, w: slot * 0.8, y: 100 - h, h, day: d.day, bytes: d.bytes };
		});
	});
</script>

<Card.Root class="md:col-span-2">
	<Card.Header>
		<Card.Description>Vault growth, last {growth.days.length} days</Card.Description>
		<Card.Title class="text-3xl tabular-nums">{bytes(growth.archived_bytes)}</Card.Title>
		<Card.Action class="text-muted-foreground text-sm tabular-nums">
			{growth.archived_count.toLocaleString()} clips
		</Card.Action>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		<svg viewBox="0 0 100 100" preserveAspectRatio="none" class="h-24 w-full" role="img" aria-label="Bytes archived per day">
			{#each bars as b (b.day)}
				<rect x={b.x} y={b.y} width={b.w} height={b.h} rx="0.6" class={b.bytes ? 'fill-brand' : 'fill-muted'}>
					<title>{b.day}: {bytes(b.bytes)}</title>
				</rect>
			{/each}
		</svg>
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
						<span class="text-muted-foreground font-normal">(~{growth.days_headroom.toLocaleString()} days)</span>
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
