<script lang="ts">
	import { api, type Camera } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Badge } from '#lib/components/ui/badge/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import Meter from '#lib/components/app/Meter.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import { bytes, count, plural, relative, span } from '#lib/format.js';
	import { VERDICTS } from '#lib/labels.js';
	import { toast } from 'svelte-sonner';
	import Play from '@lucide/svelte/icons/play';
	import Square from '@lucide/svelte/icons/square';

	/** One camera: is everything on its SD card backed up, and if not, how
	 * far behind it is and when that catches up. */
	let { cam, onchange }: { cam: Camera; onchange: (c: Camera) => void } = $props();

	const verdict = $derived(VERDICTS[cam.verdict]);
	const card = $derived(cam.card);
	const used = $derived(
		card?.total_gb && card.free_gb != null ? ((card.total_gb - card.free_gb) / card.total_gb) * 100 : null
	);
	const windowDays = $derived(
		cam.oldest_on_card_utc ? (Date.now() - Date.parse(cam.oldest_on_card_utc)) / 86400000 : null
	);
	let busy = $state(false);

	async function act(action: 'run' | 'stop') {
		busy = true;
		try {
			onchange(await api<Camera>(`/cameras/${cam.alias}/${action}`, { method: 'POST' }));
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
		<Card.Title class="min-w-0 truncate text-lg">{cam.name || cam.alias}</Card.Title>
		<Card.Action class="flex flex-col items-end gap-1">
			<StatusBadge level={verdict.level}>{verdict.label}</StatusBadge>
			{#if cam.lost_7d}
				<Badge href="/activity?camera={cam.alias}" variant="destructive">
					{plural(cam.lost_7d, 'clip')} lost this week
				</Badge>
			{/if}
		</Card.Action>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		<p class="text-sm font-medium">{cam.text}</p>

		{#if cam.pending}
			<div class="flex flex-col gap-1.5">
				<div class="text-subtle flex justify-between text-xs">
					<span>Waiting for backup</span>
					<span class="tabular-nums">
						{count(cam.pending)} clips, {bytes(cam.pending_bytes)}{#if cam.eta_seconds}, about {span(cam.eta_seconds)} left{/if}
					</span>
				</div>
				<Meter
					label="{count(cam.backed_up_clips)} of {count(cam.backed_up_clips + cam.pending)} clips backed up"
					segments={[
						{ value: (cam.backed_up_clips / Math.max(cam.backed_up_clips + cam.pending, 1)) * 100, class: 'fill-brand' }
					]}
				/>
			</div>
		{/if}

		{#if card}
			<div class="flex flex-col gap-1.5">
				<div class="text-subtle flex justify-between text-xs">
					<span>SD card</span>
					<span class="tabular-nums">
						{#if card.total_gb}{card.total_gb.toFixed(0)} GB{/if}{#if windowDays != null}, holds about {Math.max(1, Math.round(windowDays))} days{/if}
					</span>
				</div>
				<Meter
					label={used == null ? 'SD card fill unknown' : `SD card ${Math.round(used)}% full`}
					segments={[{ value: used ?? 0, class: 'fill-idle' }]}
				/>
			</div>
		{/if}

		<dl class="grid grid-cols-2 gap-3 text-sm">
			<div>
				<dt class="text-subtle text-xs">Backed up</dt>
				<dd class="flex flex-wrap gap-x-3 font-medium tabular-nums">
					<span>{count(cam.backed_up_clips)} clips</span>
					<span>{bytes(cam.backed_up_bytes)}</span>
				</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Last backup</dt>
				<dd class="flex flex-wrap items-center gap-2">
					{#if cam.running}
						<StatusBadge level="idle">Running now</StatusBadge>
					{:else if cam.last_run}
						<RunOutcome run={cam.last_run} />
						<span class="text-muted-foreground text-xs">{relative(cam.last_run.finished_at)}</span>
					{:else}
						<span class="text-muted-foreground">None yet</span>
					{/if}
				</dd>
			</div>
		</dl>
	</Card.Content>
	<Card.Footer class="flex items-center justify-between gap-2">
		<span class="text-muted-foreground text-xs">
			{#if cam.next_run_at && !cam.running}Next backup {relative(cam.next_run_at)}{/if}
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
