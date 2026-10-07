<script lang="ts">
	import { untrack } from 'svelte';
	import { api, type Recording, type RecordingsPage } from '$lib/api';
	import * as Collapsible from '$lib/components/ui/collapsible';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { Spinner } from '$lib/components/ui/spinner';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import RecordingRow from './RecordingRow.svelte';
	import { bytes, plural } from '$lib/format';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';

	let {
		device,
		date,
		hour,
		total,
		size,
		types,
		initiallyOpen
	}: {
		device: number;
		date: string;
		hour: number;
		total: number;
		size: number;
		types: string;
		initiallyOpen: boolean;
	} = $props();

	let open = $state(untrack(() => initiallyOpen));
	let rows = $state<Recording[] | null>(null);
	let next = $state<string | null>(null);
	let error = $state<string | null>(null);
	let loadingMore = $state(false);

	async function load(after: string | null) {
		const page = await api<RecordingsPage>('/day/hour', {
			query: { device, date_: date, hour, types, after: after ?? undefined }
		});
		rows = after ? [...(rows ?? []), ...page.items] : page.items;
		next = page.next ?? null;
	}

	// Lazy: an hour's clips load the first time it opens, and reload when
	// the day, filter, or its own clip count (a live update) changes.
	$effect(() => {
		void total;
		if (!open) return;
		error = null;
		load(null).catch((e) => (error = e instanceof Error ? e.message : String(e)));
	});

	async function more() {
		loadingMore = true;
		try {
			await load(next);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loadingMore = false;
		}
	}
</script>

<Collapsible.Root bind:open class="rounded-xl border">
	<Collapsible.Trigger
		class="hover:bg-muted/50 flex w-full items-center gap-3 rounded-xl px-4 py-3 text-left transition-colors"
	>
		<ChevronRight class="text-muted-foreground size-4 transition-transform {open ? 'rotate-90' : ''}" />
		<span class="font-semibold tabular-nums">{String(hour).padStart(2, '0')}:00</span>
		<span class="text-muted-foreground text-sm">{plural(total, 'clip')}</span>
		<span class="text-subtle ml-auto text-sm tabular-nums">{bytes(size)}</span>
	</Collapsible.Trigger>
	<Collapsible.Content class="flex flex-col gap-2 px-3 pb-3">
		{#if error}
			<ErrorAlert message={error} />
		{:else if rows === null}
			{#each Array(Math.min(total, 3)) as _, i (i)}<Skeleton class="h-11 w-full" />{/each}
		{:else}
			{#each rows as rec (rec.id)}<RecordingRow {rec} />{/each}
			{#if next}
				<Button variant="ghost" size="sm" onclick={more} disabled={loadingMore} class="self-center">
					{#if loadingMore}<Spinner />{/if} Load more
				</Button>
			{/if}
		{/if}
	</Collapsible.Content>
</Collapsible.Root>
