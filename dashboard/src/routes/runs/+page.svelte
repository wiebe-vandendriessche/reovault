<script lang="ts">
	import { api, type Run, type RunsPage } from '$lib/api';
	import { devices } from '$lib/state/devices.svelte';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Table from '$lib/components/ui/table';
	import * as Empty from '$lib/components/ui/empty';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { Spinner } from '$lib/components/ui/spinner';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import NoDevice from '$lib/components/app/NoDevice.svelte';
	import RunOutcome from '$lib/components/runs/RunOutcome.svelte';
	import { bytes, localTime, relative } from '$lib/format';
	import ListChecks from '@lucide/svelte/icons/list-checks';

	// First page is live; older pages are appended by "Load more".
	const first = query(() => {
		void live.activity;
		const device = devices.currentId;
		return device == null ? null : api<RunsPage>('/runs', { query: { device } });
	});

	let older = $state<Run[]>([]);
	let cursor = $state<string | null | undefined>(undefined);
	let loadingMore = $state(false);
	$effect(() => {
		void devices.currentId;
		older = [];
		cursor = undefined;
	});

	const next = $derived(cursor === undefined ? first.data?.next : cursor);
	const runs = $derived([
		...(first.data?.items ?? []),
		...older.filter((r) => !first.data?.items.some((f) => f.id === r.id))
	]);

	async function more() {
		if (!next || devices.currentId == null) return;
		loadingMore = true;
		try {
			const page = await api<RunsPage>('/runs', { query: { device: devices.currentId, before_id: next } });
			older = [...older, ...page.items];
			cursor = page.next ?? null;
		} finally {
			loadingMore = false;
		}
	}
</script>

<svelte:head><title>Runs | ReoVault</title></svelte:head>

<PageHeader title="Runs" description="Every archive run, newest first." />

{#if devices.loaded && devices.currentId == null}
	<NoDevice />
{:else if first.error}
	<ErrorAlert message={first.error} />
{:else if !first.data}
	<Skeleton class="h-80 w-full rounded-xl" />
{:else}
	{@const tz = first.data.timezone}
	<div class="flex flex-col gap-6">
		<Card.Root>
			<Card.Header><Card.Title>Coming up</Card.Title></Card.Header>
			<Card.Content>
				<dl class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
					{#each first.data.scheduled as s (s.job)}
						<div>
							<dt class="text-subtle text-xs">{s.label}</dt>
							<dd class="text-sm font-medium">
								{#if s.next_run_utc}
									<span title={localTime(s.next_run_utc, tz)}>{relative(s.next_run_utc)}</span>
								{:else}
									<span class="text-muted-foreground">Off</span>
								{/if}
							</dd>
						</div>
					{/each}
				</dl>
			</Card.Content>
		</Card.Root>

		{#if runs.length === 0}
			<Empty.Root class="border">
				<Empty.Header>
					<Empty.Media variant="icon"><ListChecks /></Empty.Media>
					<Empty.Title>No runs yet</Empty.Title>
					<Empty.Description>The first scheduled run, or a “Run now” from Health, shows up here.</Empty.Description>
				</Empty.Header>
			</Empty.Root>
		{:else}
			<Card.Root class="py-0">
				<Table.Root>
					<Table.Header>
						<Table.Row>
							<Table.Head class="pl-4">Started</Table.Head>
							<Table.Head>Trigger</Table.Head>
							<Table.Head>Outcome</Table.Head>
							<Table.Head class="text-right">Found</Table.Head>
							<Table.Head class="text-right">Archived</Table.Head>
							<Table.Head class="text-right">Failed</Table.Head>
							<Table.Head class="pr-4 text-right">Size</Table.Head>
						</Table.Row>
					</Table.Header>
					<Table.Body>
						{#each runs as r (r.id)}
							<Table.Row class="relative">
								<Table.Cell class="pl-4">
									<a href="/runs/{r.id}" class="font-medium after:absolute after:inset-0 hover:underline">
										{localTime(r.started_at, tz)}
									</a>
								</Table.Cell>
								<Table.Cell class="text-muted-foreground">{r.trigger}</Table.Cell>
								<Table.Cell><RunOutcome run={r} /></Table.Cell>
								<Table.Cell class="text-right tabular-nums">{r.discovered}</Table.Cell>
								<Table.Cell class="text-right tabular-nums">{r.downloaded}</Table.Cell>
								<Table.Cell class="text-right tabular-nums {r.failed ? 'text-bad' : ''}">{r.failed}</Table.Cell>
								<Table.Cell class="pr-4 text-right tabular-nums">{bytes(r.bytes_archived)}</Table.Cell>
							</Table.Row>
						{/each}
					</Table.Body>
				</Table.Root>
			</Card.Root>
			{#if next}
				<Button variant="ghost" onclick={more} disabled={loadingMore} class="self-center">
					{#if loadingMore}<Spinner />{/if} Load older runs
				</Button>
			{/if}
		{/if}
	</div>
{/if}
