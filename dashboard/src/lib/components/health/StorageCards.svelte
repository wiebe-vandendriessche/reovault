<script lang="ts">
	import type { Health } from '$lib/api';
	import * as Card from '$lib/components/ui/card';
	import Meter from '$lib/components/app/Meter.svelte';
	import { relative } from '$lib/format';

	let { health }: { health: Health } = $props();

	const cov = $derived(health.coverage);
	const pct = $derived(cov.margin_fraction == null ? null : Math.round(cov.margin_fraction * 100));
	const sd = $derived(health.sd);
	const day = (iso: string | null | undefined) =>
		iso ? new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) : '?';
	const gb = (n: number) => `${n.toFixed(1)} GB`;
</script>

<Card.Root>
	<Card.Header>
		<Card.Description>Coverage margin</Card.Description>
		<Card.Title class="text-3xl tabular-nums">
			{pct == null ? 'Caught up' : `${pct}%`}
		</Card.Title>
	</Card.Header>
	<Card.Content class="flex flex-col gap-3">
		{#if pct != null}
			<Meter
				label="{pct}% of the card's retention window left before unarchived clips are overwritten"
				segments={[{ value: pct, class: cov.alarm ? 'fill-bad' : pct < 40 ? 'fill-warn' : 'fill-ok' }]}
			/>
			<p class="text-muted-foreground text-sm">
				Oldest clip not yet archived is from {day(cov.oldest_unarchived_utc)}; the card goes back to
				{day(cov.oldest_on_card_utc)}.
			</p>
		{:else}
			<p class="text-muted-foreground text-sm">
				{cov.oldest_unarchived_utc == null
					? 'Every clip found on the card is archived.'
					: 'No SD card sample yet, so the margin is unknown.'}
			</p>
		{/if}
	</Card.Content>
</Card.Root>

<Card.Root>
	<Card.Header>
		<Card.Description>SD card</Card.Description>
		<Card.Title class="text-3xl tabular-nums">
			{sd ? `${gb(sd.used_gb)} / ${gb(sd.total_gb)}` : 'No sample yet'}
		</Card.Title>
	</Card.Header>
	<Card.Content class="flex flex-col gap-3">
		{#if sd}
			<Meter
				label="SD card: {gb(sd.archived_gb)} archived, {gb(sd.other_gb)} other, {gb(sd.free_gb)} free"
				segments={[
					{ value: (sd.archived_gb / sd.total_gb) * 100, class: 'fill-brand' },
					{ value: (sd.other_gb / sd.total_gb) * 100, class: 'fill-idle' }
				]}
			/>
			<div class="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
				<span class="flex items-center gap-1.5"><span class="bg-brand size-2 rounded-full"></span>Archived {gb(sd.archived_gb)}</span>
				{#if sd.other_gb > 0.1}
					<span class="flex items-center gap-1.5"><span class="bg-idle size-2 rounded-full"></span>Other {gb(sd.other_gb)}</span>
				{/if}
				<span class="flex items-center gap-1.5"><span class="bg-muted-foreground/30 size-2 rounded-full"></span>Free {gb(sd.free_gb)}</span>
			</div>
		{/if}
		{#if health.sample}
			<p class="text-subtle flex flex-wrap gap-x-3 text-xs">
				<span>{health.sample.mounted === false ? 'Not mounted' : 'Mounted'}</span>
				<span>{health.sample.formatted === false ? 'Not formatted' : 'Formatted'}</span>
				<span>Sampled {relative(health.sample.sampled_at)}</span>
			</p>
		{/if}
	</Card.Content>
</Card.Root>
