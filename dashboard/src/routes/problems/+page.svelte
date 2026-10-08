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
	import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import { buttonVariants } from '$lib/components/ui/button';
	import { plural } from '$lib/format';
	import { toast } from 'svelte-sonner';

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

	// Header count: the nav badge's endpoint, so it covers every page, not
	// just the rows loaded so far.
	const count = query(() => {
		void live.problems;
		const device = devices.currentId;
		return device == null ? null : api<{ count: number }>('/problems/count', { query: { device } });
	});
	let retrying = $state(false);

	async function retryAll() {
		if (devices.currentId == null) return;
		retrying = true;
		try {
			const r = await api<{ count: number }>('/problems/retry', {
				method: 'POST',
				query: { device: devices.currentId }
			});
			toast.success(`${plural(r.count, 'clip')} queued for the next run.`);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			retrying = false;
		}
	}

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
>
	{#snippet actions()}
		{#if count.data?.count}
			<AlertDialog.Root>
				<AlertDialog.Trigger class={buttonVariants({ variant: 'outline' })} disabled={retrying}>
					{#if retrying}<Spinner />{:else}<RotateCcw />{/if} Retry all
				</AlertDialog.Trigger>
				<AlertDialog.Content>
					<AlertDialog.Header>
						<AlertDialog.Title>Queue {plural(count.data.count, 'clip')} for the next run?</AlertDialog.Title>
						<AlertDialog.Description>
							Every failed or quarantined recording of this camera gets a fresh set of attempts. Clips
							the camera no longer has will fail again.
						</AlertDialog.Description>
					</AlertDialog.Header>
					<AlertDialog.Footer>
						<AlertDialog.Cancel>Cancel</AlertDialog.Cancel>
						<AlertDialog.Action onclick={retryAll}>Retry all</AlertDialog.Action>
					</AlertDialog.Footer>
				</AlertDialog.Content>
			</AlertDialog.Root>
		{/if}
	{/snippet}
</PageHeader>

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
