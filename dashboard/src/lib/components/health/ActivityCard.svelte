<script lang="ts">
	import { api, type Activity } from '$lib/api';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Dialog from '$lib/components/ui/dialog';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import * as Field from '$lib/components/ui/field';
	import { Button, buttonVariants } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Spinner } from '$lib/components/ui/spinner';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import { localTime, relative } from '$lib/format';
	import { toast } from 'svelte-sonner';
	import Play from '@lucide/svelte/icons/play';
	import Square from '@lucide/svelte/icons/square';
	import CalendarRange from '@lucide/svelte/icons/calendar-range';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';

	let { device }: { device: number } = $props();

	const activity = query(() => {
		void live.activity;
		return api<Activity>('/activity', { query: { device } });
	});

	let busy = $state<string | null>(null);
	let backfillOpen = $state(false);
	let from = $state('');
	let to = $state('');
	let backfillError = $state<string | null>(null);

	async function act(name: 'run' | 'stop' | 'reconcile' | 'backfill', done: string) {
		busy = name;
		backfillError = null;
		try {
			const body = name === 'backfill' ? { from_local: from, to_local: to } : undefined;
			activity.data = await api<Activity>(`/actions/${name}`, {
				method: 'POST',
				query: { device },
				body
			});
			toast.success(done);
			backfillOpen = false;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			if (name === 'backfill') backfillError = msg;
			else toast.error(msg);
		} finally {
			busy = null;
		}
	}

	function openBackfill() {
		from = activity.data?.default_from ?? '';
		to = activity.data?.default_to ?? '';
		backfillError = null;
		backfillOpen = true;
	}

	const kinds: Record<string, string> = {
		submitted: 'started',
		executed: 'finished',
		error: 'failed',
		missed: 'missed',
		skipped_already_running: 'skipped, already running'
	};
	const jobName = (id: string) => id.split(':')[0].replace(/-\d+$/, '').replaceAll('_', ' ');
</script>

<Card.Root class="md:col-span-2">
	<Card.Header>
		<Card.Description>Activity</Card.Description>
		<Card.Title>
			{#if activity.data?.running}
				<span class="flex items-center gap-2"><Spinner /> Running {activity.data.running.trigger}</span>
			{:else if activity.data?.queued}
				{activity.data.queued} queued
			{:else}
				Idle
			{/if}
		</Card.Title>
	</Card.Header>
	<Card.Content class="flex flex-col gap-4">
		{#if activity.error}
			<ErrorAlert message={activity.error} />
		{:else if !activity.data}
			<Skeleton class="h-9 w-full" />
		{:else}
			{#if activity.data.running}
				<p class="text-muted-foreground text-sm">
					Started {localTime(activity.data.running.started_at, activity.data.timezone)}.
					{#if activity.data.queued}{activity.data.queued} more queued behind it.{/if}
				</p>
			{/if}
			{#if !activity.data.scheduler_available}
				<p class="text-warn text-sm">The scheduler isn't running on this server.</p>
			{/if}
			<div class="flex flex-wrap gap-2">
				<Button
					onclick={() => act('run', 'Archive run queued.')}
					disabled={!!busy || !activity.data.scheduler_available}
				>
					{#if busy === 'run'}<Spinner />{:else}<Play />{/if} Run now
				</Button>
				<Button variant="outline" onclick={openBackfill} disabled={!activity.data.scheduler_available}>
					<CalendarRange /> Backfill
				</Button>
				<Button
					variant="outline"
					onclick={() => act('reconcile', 'Vault check queued.')}
					disabled={!!busy || !activity.data.scheduler_available}
				>
					{#if busy === 'reconcile'}<Spinner />{:else}<ShieldCheck />{/if} Check vault
				</Button>
				{#if activity.data.running}
					<AlertDialog.Root>
						<AlertDialog.Trigger class={buttonVariants({ variant: 'outline' })}>
							<Square /> Stop
						</AlertDialog.Trigger>
						<AlertDialog.Content>
							<AlertDialog.Header>
								<AlertDialog.Title>Stop this run?</AlertDialog.Title>
								<AlertDialog.Description>
									The clip being downloaded right now finishes first; nothing is left half-written.
									Clips not reached yet are picked up by the next run.
								</AlertDialog.Description>
							</AlertDialog.Header>
							<AlertDialog.Footer>
								<AlertDialog.Cancel>Keep running</AlertDialog.Cancel>
								<AlertDialog.Action onclick={() => act('stop', 'Stopping after the current clip.')}>
									Stop run
								</AlertDialog.Action>
							</AlertDialog.Footer>
						</AlertDialog.Content>
					</AlertDialog.Root>
				{/if}
			</div>
			{#if activity.data.events.length}
				<ul class="flex flex-col gap-1.5 text-sm">
					{#each activity.data.events as e (e.at + e.job_id + e.kind)}
						<li class="flex items-baseline justify-between gap-3">
							<span class="min-w-0 truncate">
								<span class="capitalize">{jobName(e.job_id)}</span>
								<span class={e.kind === 'error' ? 'text-bad' : 'text-muted-foreground'}>
									{kinds[e.kind] ?? e.kind}</span
								>{#if e.detail}<span class="text-muted-foreground">: {e.detail}</span>{/if}
							</span>
							<span class="text-subtle shrink-0 text-xs" title={e.at}>{relative(e.at)}</span>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</Card.Content>
</Card.Root>

<Dialog.Root bind:open={backfillOpen}>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Header>
			<Dialog.Title>Backfill a time range</Dialog.Title>
			<Dialog.Description>
				Archive every clip the camera still has in this range, in the camera's own time
				({activity.data?.timezone}). Up to {activity.data?.max_backfill_days} days; clips already
				archived are skipped.
			</Dialog.Description>
		</Dialog.Header>
		<form
			class="flex flex-col gap-4"
			onsubmit={(e) => {
				e.preventDefault();
				act('backfill', 'Backfill queued.');
			}}
		>
			<Field.Field>
				<Field.Label for="bf-from">From</Field.Label>
				<Input id="bf-from" type="datetime-local" required bind:value={from} />
			</Field.Field>
			<Field.Field>
				<Field.Label for="bf-to">To</Field.Label>
				<Input id="bf-to" type="datetime-local" required bind:value={to} />
			</Field.Field>
			{#if backfillError}<p class="text-bad text-sm" role="alert">{backfillError}</p>{/if}
			<Dialog.Footer>
				<Button type="submit" disabled={busy === 'backfill'}>
					{#if busy === 'backfill'}<Spinner />{/if} Queue backfill
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>
