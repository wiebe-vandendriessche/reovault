<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type Activity, type Camera } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Table from '#lib/components/ui/table/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import CameraPicker from '#lib/components/app/CameraPicker.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import ClipRow from '#lib/components/footage/ClipRow.svelte';
	import { bytes, count, duration, localTime, plural } from '#lib/format.js';
	import { triggerLabel } from '#lib/labels.js';

	const camera = $derived(page.url.searchParams.get('camera') ?? '');

	const cameras = query(() => {
		void live.settings;
		return api<Camera[]>('/cameras');
	});
	const activity = query(() => {
		void live.activity;
		return api<Activity>('/activity', { query: { camera: camera || undefined } });
	});

	const names = $derived(new Map((cameras.data ?? []).map((c) => [c.alias, c.name || c.alias])));
	const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
	const took = (a: string, b: string | null | undefined) =>
		b ? duration((Date.parse(b) - Date.parse(a)) / 1000) : '-';
</script>

<svelte:head><title>Activity | ReoVault</title></svelte:head>

<PageHeader title="Activity" description="Every backup run, and the clips that need a word of explanation.">
	{#snippet actions()}
		{#if cameras.data && cameras.data.length > 1}
			<CameraPicker
				all
				cameras={cameras.data}
				value={camera}
				onchange={(c) => goto(c ? `?camera=${encodeURIComponent(c)}` : '?', { reset: false })}
			/>
		{/if}
	{/snippet}
</PageHeader>

{#if activity.error}
	<ErrorAlert message={activity.error} />
{:else if !activity.data}
	<Skeleton class="h-96 w-full rounded-xl" />
{:else}
	{@const a = activity.data}
	<div class="flex flex-col gap-6">
		{#if a.failing.length}
			<Card.Root>
				<Card.Header>
					<Card.Title>Clips that keep failing</Card.Title>
					<Card.Description>
						Still on the SD card, but {plural(a.failing.length, 'download')} failed several times. They are
						tried again once a day until they back up or the card overwrites them.
					</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-2">
					{#each a.failing as clip (clip.id)}<ClipRow {clip} withDate />{/each}
				</Card.Content>
			</Card.Root>
		{/if}

		{#if a.lost.length}
			<Card.Root>
				<Card.Header>
					<Card.Title>Lost this week</Card.Title>
					<Card.Description>
						The SD card overwrote {plural(a.lost.length, 'clip')} before it could be backed up. Nothing can bring
						these back; backing up more often, or a bigger card, prevents it.
					</Card.Description>
				</Card.Header>
				<Card.Content class="flex flex-col gap-2">
					{#each a.lost as clip (clip.id)}<ClipRow {clip} withDate />{/each}
				</Card.Content>
			</Card.Root>
		{/if}

		<Card.Root>
			<Card.Header>
				<Card.Title>Backup runs</Card.Title>
				<Card.Description>Newest first. Times in your browser's timezone.</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if a.runs.length === 0}
					<p class="text-muted-foreground text-sm">No backup has run yet.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Started</Table.Head>
								{#if !camera}<Table.Head>Camera</Table.Head>{/if}
								<Table.Head>Result</Table.Head>
								<Table.Head class="text-right">Clips</Table.Head>
								<Table.Head class="text-right">Size</Table.Head>
								<Table.Head class="text-right">Took</Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each a.runs as run (run.id)}
								<Table.Row>
									<Table.Cell class="whitespace-nowrap">
										{localTime(run.started_at, tz)}
										<span class="text-subtle block text-xs">{triggerLabel(run.trigger)}</span>
									</Table.Cell>
									{#if !camera}<Table.Cell>{names.get(run.camera) ?? run.camera}</Table.Cell>{/if}
									<Table.Cell class="max-w-md">
										<RunOutcome {run} />
										{#if run.error}<p class="text-muted-foreground mt-1 text-xs break-words whitespace-normal">{run.error}</p>{/if}
										{#if run.lost}<p class="text-subtle mt-1 text-xs">{plural(run.lost, 'clip')} lost</p>{/if}
									</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{count(run.clips)}</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{bytes(run.bytes)}</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{took(run.started_at, run.finished_at)}</Table.Cell>
								</Table.Row>
							{/each}
						</Table.Body>
					</Table.Root>
				{/if}
			</Card.Content>
		</Card.Root>
	</div>
{/if}
