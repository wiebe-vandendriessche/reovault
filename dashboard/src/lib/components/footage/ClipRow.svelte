<script lang="ts">
	import { apiUrl, type Clip } from '#lib/api/index.js';
	import { Badge } from '#lib/components/ui/badge/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import { bytes, plural } from '#lib/format.js';
	import { stateLabel, typeLabel, typesOf } from '#lib/labels.js';
	import Download from '@lucide/svelte/icons/download';

	/** One clip of a day. Only a backed-up clip can be downloaded: it comes
	 * straight out of restic. Clips still on the SD card play in the Reolink
	 * app. */
	let { clip, withDate = false }: { clip: Clip; withDate?: boolean } = $props();

	const time = $derived(clip.start.slice(11, 19));
	const when = $derived(withDate ? `${clip.start.slice(0, 10)} ${time}` : time);
	const level = $derived(clip.state === 'pending' ? 'warn' : 'idle');
</script>

<div class="rounded-lg border">
	<div class="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
		<span class="font-medium tabular-nums">{when}</span>
		<div class="flex flex-wrap gap-1">
			{#each typesOf(clip.rec_type) as t (t)}<Badge variant="secondary">{typeLabel(t)}</Badge>{/each}
		</div>
		{#if clip.state !== 'backed_up'}
			<StatusBadge {level}>{stateLabel(clip.state)}</StatusBadge>
		{/if}
		<span class="text-muted-foreground ml-auto text-sm tabular-nums">{bytes(clip.size)}</span>
		{#if clip.state === 'backed_up'}
			<Button size="sm" variant="outline" href={apiUrl(`/clips/${clip.id}/download`)} aria-label="Download clip from {when}">
				<Download /> Download
			</Button>
		{/if}
	</div>
	{#if clip.state === 'pending' && clip.last_error}
		<div class="border-t px-3 py-2.5 text-sm">
			<p class="text-muted-foreground font-mono text-xs break-all">{clip.last_error}</p>
			<p class="text-subtle mt-1 text-xs">{plural(clip.attempts, 'attempt')}; tried again on the next backup.</p>
		</div>
	{/if}
</div>
