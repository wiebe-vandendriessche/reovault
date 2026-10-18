<script lang="ts">
	import type { components } from '$lib/api/schema';
	import * as Card from '$lib/components/ui/card';
	import Legend from '$lib/components/app/Legend.svelte';
	import Meter from '$lib/components/app/Meter.svelte';
	import { bytes, count, shortDay } from '$lib/format';

	/** The archive's disk: how much ReoVault uses, what else is on the
	 * volume, what's free, and how long that free space lasts. */
	let { vault }: { vault: components['schemas']['VaultOut'] } = $props();

	const pct = (n: number | null | undefined) =>
		vault.total_bytes && n != null ? (n / vault.total_bytes) * 100 : 0;
	const archive = $derived(pct(vault.archive_bytes));
	const other = $derived(Math.max(pct(vault.used_bytes) - archive, 0));
	const cap = $derived(vault.cap_bytes ? Math.min(pct(vault.cap_bytes), 100) : null);
	const low = $derived(vault.days_headroom != null && vault.days_headroom < 30);
	const room = $derived(
		vault.days_headroom == null
			? null
			: vault.days_headroom > 3650
				? '10+ years'
				: `about ${count(vault.days_headroom)} days`
	);
</script>

<Card.Root>
	<Card.Header>
		<Card.Description>Archive disk</Card.Description>
		<Card.Title class="text-lg tabular-nums {low ? 'text-warn' : ''}">
			{room ? `Room for ${room} at the current rate` : `${bytes(vault.free_bytes)} free`}
		</Card.Title>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		{#if vault.total_bytes}
			<Meter
				class="h-4"
				label="Disk: {bytes(vault.archive_bytes)} ReoVault, {bytes(Math.max(vault.used_bytes! - vault.archive_bytes, 0))} other, {bytes(vault.free_bytes)} free of {bytes(vault.total_bytes)}"
				segments={[
					{ value: archive, class: 'fill-brand' },
					{ value: other, class: 'fill-idle' }
				]}
				marker={cap}
			/>
			<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
				<Legend
					items={[
						{ label: `ReoVault ${bytes(vault.archive_bytes)}`, class: 'bg-brand' },
						{ label: `Other ${bytes(Math.max(vault.used_bytes! - vault.archive_bytes, 0))}`, class: 'bg-idle' },
						{ label: `Free ${bytes(vault.free_bytes)}`, class: 'bg-muted-foreground/30' },
						...(cap != null ? [{ label: `Size cap ${bytes(vault.cap_bytes)}`, class: 'bg-foreground', swatch: 'h-3 w-0.5 rounded-none' }] : [])
					]}
				/>
				<span class="text-muted-foreground ml-auto text-xs">of {bytes(vault.total_bytes)}</span>
			</div>
		{/if}
		<dl class="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
			<div><dt class="text-subtle text-xs">Clips</dt><dd class="font-medium tabular-nums">{count(vault.clip_count)}</dd></div>
			<div>
				<dt class="text-subtle text-xs">Footage from</dt>
				<dd class="font-medium">{vault.first_start_utc ? shortDay(vault.first_start_utc.slice(0, 10)) : '-'} to now</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Free</dt>
				<dd class="font-medium tabular-nums">
					{bytes(vault.free_bytes)}
					<span class="text-subtle text-xs font-normal">at {bytes(vault.avg_per_day)}/day</span>
				</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Kept for</dt>
				<dd class="font-medium">{vault.max_age_days ? `${vault.max_age_days} days` : 'Forever'}</dd>
			</div>
		</dl>
	</Card.Content>
</Card.Root>
