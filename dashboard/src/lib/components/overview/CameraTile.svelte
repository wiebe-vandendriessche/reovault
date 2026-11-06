<script lang="ts">
	import { api, type Camera } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Badge } from '#lib/components/ui/badge/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import CoverageRows from './CoverageRows.svelte';
	import { plural, relative } from '#lib/format.js';
	import { VERDICTS } from '#lib/labels.js';
	import { toast } from 'svelte-sonner';
	import Play from '@lucide/svelte/icons/play';
	import Square from '@lucide/svelte/icons/square';

	/** One camera: is its footage safe, and where is it? A verdict in words,
	 * then where the chosen span of footage lives: on the SD card, on the
	 * primary, on each copy. */
	let { cam, onchange }: { cam: Camera; onchange: (c: Camera) => void } = $props();

	const verdict = $derived(VERDICTS[cam.verdict]);
	let busy = $state(false);

	async function act(action: 'run' | 'stop') {
		busy = true;
		try {
			onchange(await api<Camera>(`/cameras/${cam.id}/${action}`, { method: 'POST' }));
			toast.success(action === 'run' ? 'Backup started.' : 'Stopping after the current batch.');
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			busy = false;
		}
	}
</script>

<Card.Root>
	<Card.Header>
		<Card.Description>{cam.demo ? 'Demo camera' : 'Camera'}</Card.Description>
		<Card.Title class="min-w-0 truncate text-lg">{cam.name || cam.id}</Card.Title>
		<Card.Action class="flex flex-col items-end gap-1">
			<StatusBadge level={verdict.level}>{verdict.label}</StatusBadge>
			{#if cam.lost_7d}
				<Badge href="/activity?camera={cam.id}" variant="destructive">
					{plural(cam.lost_7d, 'clip')} lost this week
				</Badge>
			{/if}
		</Card.Action>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		<p class="text-sm font-medium">{cam.text}</p>
		{#if cam.coverage}<CoverageRows coverage={cam.coverage} />{/if}
	</Card.Content>
	<Card.Footer class="flex flex-wrap items-center justify-between gap-2">
		<span class="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
			{#if cam.running}
				<StatusBadge level="idle">Backing up now</StatusBadge>
			{:else if cam.last_run}
				<RunOutcome run={cam.last_run} />
				<span>{relative(cam.last_run.finished_at)}</span>
			{/if}
			{#if cam.next_run_at && !cam.running}<span>Next {relative(cam.next_run_at)}</span>{/if}
		</span>
		{#if cam.enabled}
			{#if cam.running}
				<Button size="sm" variant="outline" onclick={() => act('stop')} disabled={busy}>
					{#if busy}<Spinner />{:else}<Square />{/if} Stop
				</Button>
			{:else}
				<Button size="sm" variant="outline" onclick={() => act('run')} disabled={busy}>
					{#if busy}<Spinner />{:else}<Play />{/if} Back up now
				</Button>
			{/if}
		{/if}
	</Card.Footer>
</Card.Root>
