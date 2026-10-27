<script lang="ts">
	import type { Health } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import { bytes, plural, relative } from '#lib/format.js';
	import { triggerLabel } from '#lib/labels.js';

	let { health }: { health: Health } = $props();

	const verdict = $derived.by(() => {
		const { running, last_run: last } = health;
		if (running) return `${triggerLabel(running.trigger)} run in progress.`;
		if (!last) return 'No run yet.';
		switch (last.outcome) {
			case 'success':
				return `Last run archived ${plural(last.downloaded, 'clip')}, ${bytes(last.bytes_archived)}.`;
			case 'partial':
				return `Last run archived ${plural(last.downloaded, 'clip')}, ${last.failed} failed.`;
			case 'aborted':
				return 'Last run was stopped or failed to start.';
			default:
				return `Last run failed after ${plural(last.downloaded, 'clip')}.`;
		}
	});

</script>

<Card.Root>
	<Card.Header>
		<Card.Description>Runs</Card.Description>
		<Card.Title class="text-xl leading-snug">{verdict}</Card.Title>
		<Card.Action>
			{#if health.running}
				<StatusBadge level="idle">Running</StatusBadge>
			{:else if health.last_run}
				<RunOutcome run={health.last_run} />
			{:else}
				<StatusBadge level="idle">No runs yet</StatusBadge>
			{/if}
		</Card.Action>
	</Card.Header>
	<Card.Content class="text-muted-foreground flex flex-row flex-wrap gap-x-6 gap-y-1 text-sm">
		{#if health.last_run?.finished_at}
			<span title={health.last_run.finished_at}>Finished {relative(health.last_run.finished_at)}</span>
		{/if}
		{#if health.next_archive_utc}
			<span title={health.next_archive_utc}>Next run {relative(health.next_archive_utc)}</span>
		{:else if !health.running}
			<span>No scheduled run</span>
		{/if}
		{#if health.gateway_down}
			<span class="text-warn basis-full font-medium">
				Camera gateway unreachable. ReoVault starts it again before its next camera call; Run now tries
				straight away.
			</span>
		{/if}
	</Card.Content>
</Card.Root>
