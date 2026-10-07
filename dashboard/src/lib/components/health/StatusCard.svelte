<script lang="ts">
	import type { Health } from '$lib/api';
	import * as Card from '$lib/components/ui/card';
	import StatusBadge from '$lib/components/app/StatusBadge.svelte';
	import { bytes, plural, relative } from '$lib/format';

	let { health }: { health: Health } = $props();

	const verdict = $derived.by(() => {
		const { running, last_run: last } = health;
		if (running) return `Run in progress (${running.trigger}).`;
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

	const label = { ok: 'Healthy', warn: 'Degraded', bad: 'Failing', idle: 'Idle' } as const;
</script>

<Card.Root>
	<Card.Header>
		<Card.Description>Archive status</Card.Description>
		<Card.Title class="text-xl leading-snug">{verdict}</Card.Title>
		<Card.Action>
			<StatusBadge level={health.status}>{health.running ? 'Running' : label[health.status]}</StatusBadge>
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
			<span class="text-warn font-medium">Camera gateway unreachable</span>
		{/if}
	</Card.Content>
</Card.Root>
