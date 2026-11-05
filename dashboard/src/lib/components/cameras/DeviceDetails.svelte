<script lang="ts">
	import { api, type Camera, type Device } from '#lib/api/index.js';
	import { query } from '#lib/state/query.svelte.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import Meter from '#lib/components/app/Meter.svelte';

	/** The camera itself: model and firmware, where it is, and its SD card.
	 * Asked from the camera (the server caches it for 10 minutes). */
	let { camera }: { camera: Camera } = $props();

	const device = query(() => api<Device>(`/cameras/${camera.id}/device`));
	const card = $derived(device.data?.card);
	const usedGb = $derived(card?.total_gb != null && card.free_gb != null ? card.total_gb - card.free_gb : null);
	const usedPct = $derived(card?.total_gb && usedGb != null ? (usedGb / card.total_gb) * 100 : null);
	const holdsDays = $derived(
		camera.oldest_on_card_utc ? Math.max(1, Math.round((Date.now() - Date.parse(camera.oldest_on_card_utc)) / 86400000)) : null
	);
</script>

{#if !device.data}
	<div class="flex flex-col gap-2"><Skeleton class="h-4 w-2/3" /><Skeleton class="h-4 w-1/2" /><Skeleton class="h-2 w-full" /></div>
{:else}
	{@const d = device.data}
	<dl class="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
		<div>
			<dt class="text-subtle text-xs">Model</dt>
			<dd class="font-medium">{d.model ?? 'Unknown'}</dd>
		</div>
		<div>
			<dt class="text-subtle text-xs">Firmware</dt>
			<dd class="truncate font-medium" title={d.firmware ?? ''}>{d.firmware ?? 'Unknown'}</dd>
		</div>
		<div>
			<dt class="text-subtle text-xs">Address</dt>
			<dd class="truncate font-mono text-xs">{camera.demo ? 'None (demo)' : camera.host}</dd>
		</div>
		<div>
			<dt class="text-subtle text-xs">User</dt>
			<dd class="truncate">{camera.demo ? 'None (demo)' : camera.user}</dd>
		</div>
		{#if d.hardware}
			<div class="col-span-2">
				<dt class="text-subtle text-xs">Hardware</dt>
				<dd class="truncate font-mono text-xs">{d.hardware}</dd>
			</div>
		{/if}
	</dl>
	{#if d.error}
		<p class="text-muted-foreground text-sm">{d.error}</p>
	{:else if card?.mounted === false}
		<p class="text-bad text-sm font-medium">No SD card</p>
	{:else if card && usedPct != null}
		<div class="flex flex-col gap-1.5">
			<div class="text-subtle flex justify-between gap-2 text-xs">
				<span>SD card</span>
				<span class="tabular-nums">
					{usedGb?.toFixed(0)} of {card.total_gb?.toFixed(0)} GB used{#if holdsDays}, holds about {holdsDays} days{/if}
				</span>
			</div>
			<Meter label="SD card {Math.round(usedPct)}% used" segments={[{ value: usedPct, class: 'fill-idle' }]} />
		</div>
	{/if}
{/if}
