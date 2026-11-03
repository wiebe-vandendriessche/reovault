<script lang="ts">
	import type { Clip } from '#lib/api/index.js';
	import * as Collapsible from '#lib/components/ui/collapsible/index.js';
	import { apiUrl } from '#lib/api/index.js';
	import ClipRow from './ClipRow.svelte';
	import { addDays, bytes, plural } from '#lib/format.js';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';

	let { hour, clips, camera, day }: { hour: number; clips: Clip[]; camera: string; day: string } = $props();

	let open = $state(false);
	const hh = (h: number) => String(h).padStart(2, '0');
	const zip = $derived(
		apiUrl('/download/zip', {
			cameras: camera,
			from: `${day}T${hh(hour)}:00`,
			to: hour === 23 ? `${addDays(day, 1)}T00:00` : `${day}T${hh(hour + 1)}:00`
		})
	);
	const size = $derived(clips.reduce((n, c) => n + (c.size ?? 0), 0));
</script>

<Collapsible.Root bind:open class="rounded-xl border">
	<Collapsible.Trigger
		class="hover:bg-muted/50 flex w-full items-center gap-3 rounded-xl px-4 py-3 text-left transition-colors"
	>
		<ChevronRight class="text-muted-foreground size-4 transition-transform {open ? 'rotate-90' : ''}" />
		<span class="font-semibold tabular-nums">{String(hour).padStart(2, '0')}:00</span>
		<span class="text-muted-foreground text-sm">{plural(clips.length, 'clip')}</span>
		<span class="text-subtle ml-auto text-sm tabular-nums">{bytes(size)}</span>
	</Collapsible.Trigger>
	<Collapsible.Content class="flex flex-col gap-2 px-3 pb-3">
		<a class="text-muted-foreground hover:text-foreground self-end text-xs underline-offset-4 hover:underline" href={zip}>Download this hour</a>
		{#each clips as clip (clip.id)}<ClipRow {clip} />{/each}
	</Collapsible.Content>
</Collapsible.Root>
