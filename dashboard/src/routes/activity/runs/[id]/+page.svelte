<script lang="ts">
	import { page } from '$app/state';
	import { api, type RunDetail } from '#lib/api/index.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Table from '#lib/components/ui/table/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import { bytes, count, duration, localTime, plural } from '#lib/format.js';
	import { stateLabel, triggerLabel } from '#lib/labels.js';
	import ArrowLeft from '@lucide/svelte/icons/arrow-left';

	const detail = query(() => api<RunDetail>(`/activity/runs/${page.params.id}`));
	const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
	const level = { backed_up: 'ok', failed: 'bad', lost: 'warn' } as const;
</script>

<svelte:head><title>Run {page.params.id} | ReoVault</title></svelte:head>

<PageHeader title="Backup run" description="What this run uploaded, and what happened to each clip.">
	{#snippet actions()}
		<Button variant="outline" href="/activity"><ArrowLeft /> Activity</Button>
	{/snippet}
</PageHeader>

{#if detail.error}
	<ErrorAlert message={detail.error} />
{:else if !detail.data}
	<Skeleton class="h-96 w-full rounded-xl" />
{:else}
	{@const d = detail.data}
	<div class="flex flex-col gap-6">
		<Card.Root>
			<Card.Header>
				<Card.Title>{d.run.camera}, {localTime(d.run.started_at, tz)}</Card.Title>
				<Card.Description>
					{triggerLabel(d.run.trigger)} run, {plural(d.run.clips, 'clip')}, {bytes(d.run.bytes)}{#if d.run.finished_at},
						took {duration((Date.parse(d.run.finished_at) - Date.parse(d.run.started_at)) / 1000)}{/if}
				</Card.Description>
				<Card.Action><RunOutcome run={d.run} /></Card.Action>
			</Card.Header>
			{#if d.run.error}<Card.Content><p class="text-muted-foreground text-sm break-words">{d.run.error}</p></Card.Content>{/if}
		</Card.Root>

		<Card.Root>
			<Card.Header><Card.Title>Batches</Card.Title><Card.Description>Each batch is downloaded from the camera, then uploaded as one snapshot.</Card.Description></Card.Header>
			<Card.Content>
				{#if d.batches.length === 0}
					<p class="text-muted-foreground text-sm">No batch was uploaded.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Finished</Table.Head>
								<Table.Head>Snapshot</Table.Head>
								<Table.Head class="text-right">Clips</Table.Head>
								<Table.Head class="text-right">Size</Table.Head>
								<Table.Head class="text-right">Download</Table.Head>
								<Table.Head class="text-right">Upload</Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each d.batches as b (b.snapshot)}
								<Table.Row>
									<Table.Cell class="whitespace-nowrap">{localTime(b.finished_at, tz)}</Table.Cell>
									<Table.Cell class="font-mono text-xs">{b.snapshot.slice(0, 8)}</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{count(b.clips)}</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{bytes(b.bytes)}</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{duration(b.download_secs)}</Table.Cell>
									<Table.Cell class="text-right tabular-nums">{duration(b.upload_secs)}</Table.Cell>
								</Table.Row>
							{/each}
						</Table.Body>
					</Table.Root>
				{/if}
			</Card.Content>
		</Card.Root>

		<Card.Root>
			<Card.Header><Card.Title>Clips</Card.Title></Card.Header>
			<Card.Content class="flex flex-col gap-2">
				{#each d.clips as c (c.id)}
					<div class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-3 py-2 text-sm">
						<span class="tabular-nums">{localTime(c.start, tz)}</span>
						<span class="text-muted-foreground tabular-nums">{bytes(c.size)}</span>
						<StatusBadge level={level[c.outcome as keyof typeof level] ?? 'idle'}>{stateLabel(c.outcome)}</StatusBadge>
						{#if c.last_error}<span class="text-muted-foreground w-full text-xs break-words">{c.last_error}</span>{/if}
					</div>
				{/each}
				{#if d.truncated}<p class="text-subtle text-xs">Only the first clips are listed.</p>{/if}
				{#if d.clips.length === 0}<p class="text-muted-foreground text-sm">No clips were touched.</p>{/if}
			</Card.Content>
		</Card.Root>
	</div>
{/if}
