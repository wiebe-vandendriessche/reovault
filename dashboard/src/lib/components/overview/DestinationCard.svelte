<script lang="ts">
	import type { Destination } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Badge } from '#lib/components/ui/badge/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import Meter from '#lib/components/app/Meter.svelte';
	import { bytes, count, relative } from '#lib/format.js';

	/** One place backups go: its role, how full it is against its size limit,
	 * and whether it is healthy. Read from the local index, so loading the
	 * overview never touches a remote destination. */
	let { destination: d }: { destination: Destination } = $props();

	const limit = $derived(d.max_gb * 1e9);
	const pct = $derived(limit ? Math.min(100, (d.used_bytes / limit) * 100) : 0);
	const seeding = $derived(d.seeding.reduce((n, s) => n + s.done, 0));
	const seedingTotal = $derived(d.seeding.reduce((n, s) => n + s.total, 0));
</script>

<Card.Root>
	<Card.Header>
		<Card.Description>{d.role === 'primary' ? 'Primary destination' : d.role === 'copy' ? 'Copy' : 'Turned off'}</Card.Description>
		<Card.Title class="min-w-0 truncate text-base">{d.name}</Card.Title>
		<Card.Action class="flex flex-col items-end gap-1">
			{#if d.error}
				<StatusBadge level="bad">Problem</StatusBadge>
			{:else if d.behind}
				<StatusBadge level="warn">Behind</StatusBadge>
			{:else if d.seeding.length}
				<StatusBadge level="idle">Copying {count(seeding)} of {count(seedingTotal)}</StatusBadge>
			{:else if d.role !== 'off'}
				<StatusBadge level="ok">Healthy</StatusBadge>
			{/if}
			{#if d.quota_short}<Badge variant="destructive">Size limit cut backups short</Badge>{/if}
		</Card.Action>
	</Card.Header>
	<Card.Content class="flex flex-col gap-3">
		<p class="text-muted-foreground truncate font-mono text-xs">{d.repository}</p>
		{#if d.max_gb}
			<div class="flex flex-col gap-1.5">
				<div class="text-subtle flex justify-between text-xs">
					<span>About {bytes(d.used_bytes)} of {d.max_gb} GB</span>
					<span class="tabular-nums">{Math.round(pct)}%</span>
				</div>
				<Meter label="{Math.round(pct)}% of the size limit used" segments={[{ value: pct, class: pct > 90 ? 'fill-warn' : 'fill-brand' }]} />
			</div>
		{/if}
		{#if d.error}<p class="text-bad text-xs break-words">{d.error}</p>{/if}
		<dl class="grid grid-cols-3 gap-3 text-sm">
			<div>
				<dt class="text-subtle text-xs">Snapshots</dt>
				<dd class="font-medium tabular-nums">{count(d.snapshots)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Footage</dt>
				<dd class="font-medium tabular-nums">{bytes(d.used_bytes)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Kept for</dt>
				<dd class="font-medium">{d.keep_days ? `${d.keep_days} days` : 'Forever'}</dd>
			</div>
		</dl>
		{#if d.last_ok}<p class="text-subtle text-xs">Last worked {relative(d.last_ok)}</p>{/if}
	</Card.Content>
</Card.Root>
