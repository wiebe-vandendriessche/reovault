<script lang="ts">
	import type { Clip } from '#lib/api/index.js';
	import * as Collapsible from '#lib/components/ui/collapsible/index.js';
	import ClipRow from './ClipRow.svelte';
	import { bytes, plural } from '#lib/format.js';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';

	let { hour, clips }: { hour: number; clips: Clip[] } = $props();

	let open = $state(false);
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
		{#each clips as clip (clip.id)}<ClipRow {clip} />{/each}
	</Collapsible.Content>
</Collapsible.Root>
