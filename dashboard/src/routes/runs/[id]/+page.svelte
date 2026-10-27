<script lang="ts">
	import { page } from '$app/state';
	import { api, type RunDetail } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Breadcrumb from '#lib/components/ui/breadcrumb/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import { bytes, localTime, plural } from '#lib/format.js';
	import { triggerLabel } from '#lib/labels.js';

	const detail = query(() => {
		void live.activity;
		return api<RunDetail>(`/runs/${Number(page.params.id)}`);
	});
</script>

<svelte:head><title>Run {page.params.id} | ReoVault</title></svelte:head>

<Breadcrumb.Root class="mb-4">
	<Breadcrumb.List>
		<Breadcrumb.Item><Breadcrumb.Link href={detail.data?.run.device_id ? `/runs?device=${detail.data.run.device_id}` : '/runs'}>Runs</Breadcrumb.Link></Breadcrumb.Item>
		<Breadcrumb.Separator />
		<Breadcrumb.Item><Breadcrumb.Page>Run {page.params.id}</Breadcrumb.Page></Breadcrumb.Item>
	</Breadcrumb.List>
</Breadcrumb.Root>

{#if detail.error}
	<ErrorAlert message={detail.error} />
{:else if !detail.data}
	<Skeleton class="h-64 w-full rounded-xl" />
{:else}
	{@const r = detail.data.run}
	{@const tz = detail.data.timezone}
	<Card.Root>
		<Card.Header>
			<Card.Description>{triggerLabel(r.trigger)} run</Card.Description>
			<Card.Title class="text-xl">{localTime(r.started_at, tz)}</Card.Title>
			<Card.Action><RunOutcome run={r} /></Card.Action>
		</Card.Header>
		<Card.Content class="flex flex-col gap-6">
			<dl class="grid grid-cols-2 gap-4 sm:grid-cols-5">
				{#each [['Found', r.discovered], ['Archived', r.downloaded], ['Duplicates', r.skipped_dup], ['Failed', r.failed]] as [k, v] (k)}
					<div>
						<dt class="text-subtle text-xs">{k}</dt>
						<dd class="text-2xl font-semibold tabular-nums">{v}</dd>
					</div>
				{/each}
				<div>
					<dt class="text-subtle text-xs">Size</dt>
					<dd class="text-2xl font-semibold tabular-nums">{bytes(r.bytes_archived)}</dd>
				</div>
			</dl>
			<dl class="grid gap-3 text-sm sm:grid-cols-2">
				<div><dt class="text-subtle text-xs">Finished</dt><dd>{localTime(r.finished_at, tz)}</dd></div>
				<div>
					<dt class="text-subtle text-xs">Window searched</dt>
					<dd>{localTime(r.window_from_utc, tz)} to {localTime(r.window_to_utc, tz)}</dd>
				</div>
			</dl>
			{#if r.failed}
				<p class="text-sm">
					{plural(r.failed, 'clip')} failed in this run.
					<a href="/problems?device={r.device_id}" class="text-brand-text underline-offset-4 hover:underline">
						Review this camera's problems
					</a>
					to see why and retry them.
				</p>
			{/if}
			{#if r.error}
				<div>
					<p class="text-subtle mb-1 text-xs">Error</p>
					<pre class="bg-muted overflow-x-auto rounded-md p-3 text-xs whitespace-pre-wrap">{r.error}</pre>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>
{/if}
