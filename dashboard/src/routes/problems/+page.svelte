<script lang="ts">
	import { api, type Recording, type RecordingsPage } from '$lib/api';
	import { devices } from '$lib/state/devices.svelte';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Empty from '$lib/components/ui/empty';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { Spinner } from '$lib/components/ui/spinner';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import NoDevice from '$lib/components/app/NoDevice.svelte';
	import RecordingRow from '$lib/components/footage/RecordingRow.svelte';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';

	const first = query(() => {
		void live.problems;
		const device = devices.currentId;
		return device == null ? null : api<RecordingsPage>('/problems', { query: { device } });
	});

	let older = $state<Recording[]>([]);
	let cursor = $state<string | null | undefined>(undefined);
	let loadingMore = $state(false);
	$effect(() => {
		void devices.currentId;
		void live.problems;
		older = [];
		cursor = undefined;
	});

	const next = $derived(cursor === undefined ? first.data?.next : cursor);
	const rows = $derived([...(first.data?.items ?? []), ...older]);

	async function more() {
		if (!next || devices.currentId == null) return;
		loadingMore = true;
		try {
			const page = await api<RecordingsPage>('/problems', { query: { device: devices.currentId, after: next } });
			older = [...older, ...page.items];
			cursor = page.next ?? null;
		} finally {
			loadingMore = false;
		}
	}
</script>

<svelte:head><title>Problems | ReoVault</title></svelte:head>

<PageHeader
	title="Problems"
	description="Recordings that failed or were quarantined. Retry queues one for the next run."
/>

{#if devices.loaded && devices.currentId == null}
	<NoDevice />
{:else if first.error}
	<ErrorAlert message={first.error} />
{:else if !first.data}
	<div class="flex flex-col gap-2">{#each [0, 1, 2] as i (i)}<Skeleton class="h-24 w-full rounded-lg" />{/each}</div>
{:else if rows.length === 0}
	<Empty.Root class="border">
		<Empty.Header>
			<Empty.Media variant="icon"><ShieldCheck /></Empty.Media>
			<Empty.Title>Nothing needs attention</Empty.Title>
			<Empty.Description>Every recording ReoVault found has been archived and verified.</Empty.Description>
		</Empty.Header>
	</Empty.Root>
{:else}
	<div class="flex flex-col gap-2">
		{#each rows as rec (rec.id)}<RecordingRow {rec} problemView />{/each}
		{#if next}
			<Button variant="ghost" onclick={more} disabled={loadingMore} class="self-center">
				{#if loadingMore}<Spinner />{/if} Load more
			</Button>
		{/if}
	</div>
{/if}
