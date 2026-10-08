<script lang="ts">
	import { MediaQuery } from 'svelte/reactivity';
	import { api, apiUrl, type Recording, type Verify } from '$lib/api';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Spinner } from '$lib/components/ui/spinner';
	import * as AspectRatio from '$lib/components/ui/aspect-ratio';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import StatusBadge from '$lib/components/app/StatusBadge.svelte';
	import { bytes, duration, plural, relative } from '$lib/format';
	import { toast } from 'svelte-sonner';
	import { cn } from '$lib/utils';
	import Play from '@lucide/svelte/icons/play';
	import Download from '@lucide/svelte/icons/download';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';
	import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
	import X from '@lucide/svelte/icons/x';

	let {
		rec: initial,
		problemView = false,
		onopen
	}: {
		rec: Recording;
		problemView?: boolean;
		/** Set by the hour list: on phones, Play opens its bottom-sheet player. */
		onopen?: () => void;
	} = $props();

	const phone = new MediaQuery('max-width: 767px');

	function play() {
		if (phone.current && onopen) return onopen();
		open = !open;
		unsupported = false;
	}

	// Follows the prop, and is overwritten locally by retry/verify results.
	let rec = $derived(initial);

	let open = $state(false);
	let unsupported = $state(false);
	let busy = $state<'verify' | 'retry' | null>(null);

	const archived = $derived(rec.state === 'archived');
	const isProblem = $derived(rec.state === 'failed' || rec.state === 'quarantined');
	const stream = $derived(apiUrl(`/recordings/${rec.id}/stream`));
	const download = $derived(apiUrl(`/recordings/${rec.id}/stream`, { download: 1 }));

	async function verify() {
		busy = 'verify';
		try {
			const r = await api<Verify>(`/recordings/${rec.id}/verify`, { method: 'POST' });
			rec = r.recording;
			if (r.ok) toast.success('Integrity verified: the stored clip matches its hash.');
			else toast.error('Integrity check failed: the stored clip does not match its hash.');
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			busy = null;
		}
	}

	async function retry() {
		busy = 'retry';
		try {
			rec = await api<Recording>(`/recordings/${rec.id}/retry`, { method: 'POST' });
			toast.success('Queued for the next run.');
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			busy = null;
		}
	}

	// A paused clip elsewhere on the page shouldn't keep playing under this one.
	function onplay(e: Event) {
		for (const v of document.querySelectorAll('video')) if (v !== e.currentTarget) v.pause();
	}
</script>

<div class={cn('rounded-lg border transition-colors', open && 'bg-card')}>
	<div class="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
		<span class="font-medium tabular-nums" title={rec.local_datetime}>
			{problemView ? rec.local_datetime : rec.local_time}
		</span>
		<div class="flex flex-wrap gap-1">
			{#each rec.types as t (t)}<Badge variant="secondary">{t}</Badge>{/each}
		</div>
		{#if !archived}
			<StatusBadge level={isProblem ? 'bad' : rec.state === 'pruned' ? 'idle' : 'warn'}>{rec.state}</StatusBadge>
		{/if}
		<span class="text-muted-foreground ml-auto flex gap-x-3 text-sm tabular-nums">
			<span>{duration(rec.duration_s)}</span>
			<span>{bytes(rec.plaintext_size ?? rec.remote_size)}</span>
		</span>
		<div class="flex gap-1">
			{#if archived}
				<Button
					size="sm"
					variant={open ? 'secondary' : 'outline'}
					onclick={play}
					aria-expanded={open}
				>
					{#if open}<X /> Close{:else}<Play /> Play{/if}
				</Button>
				<Tooltip.Root>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<Button {...props} size="icon-sm" variant="ghost" href={download} aria-label="Download clip">
								<Download />
							</Button>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content>Download</Tooltip.Content>
				</Tooltip.Root>
				<Tooltip.Root>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<Button {...props} size="icon-sm" variant="ghost" onclick={verify} disabled={!!busy} aria-label="Verify integrity">
								{#if busy === 'verify'}<Spinner />{:else}<ShieldCheck />{/if}
							</Button>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content>Re-check the stored clip against its hash</Tooltip.Content>
				</Tooltip.Root>
			{:else if isProblem}
				<Button size="sm" variant="outline" onclick={retry} disabled={!!busy || !!rec.next_attempt_at}>
					{#if busy === 'retry'}<Spinner />{:else}<RotateCcw />{/if}
					{rec.next_attempt_at ? 'Queued' : 'Retry'}
				</Button>
			{/if}
		</div>
	</div>

	{#if isProblem && (rec.last_error_class_text || rec.last_error)}
		<div class="border-t px-3 py-2.5 text-sm">
			{#if rec.last_error_class_text}<p>{rec.last_error_class_text}</p>{/if}
			{#if rec.last_error}
				<p class="text-muted-foreground mt-1 font-mono text-xs break-all">{rec.last_error}</p>
			{/if}
			<p class="text-subtle mt-1 flex flex-wrap gap-x-3 text-xs">
				<span>{plural(rec.attempts, 'attempt')}</span>
				{#if rec.next_attempt_at}<span>Next try {relative(rec.next_attempt_at)}</span>{/if}
			</p>
		</div>
	{/if}

	{#if open && archived}
		<div class="border-t p-3">
			{#if unsupported}
				<div class="bg-muted flex flex-col items-start gap-3 rounded-md p-4 text-sm">
					<p>This browser can't play this clip's video format (often H.265).</p>
					<Button size="sm" href={download}><Download /> Download to watch</Button>
				</div>
			{:else}
				<AspectRatio.Root ratio={16 / 9} class="overflow-hidden rounded-md bg-black">
					<!-- svelte-ignore a11y_media_has_caption -->
					<video
						class="size-full"
						src={stream}
						controls
						autoplay
						playsinline
						preload="metadata"
						{onplay}
						onerror={() => (unsupported = true)}
					></video>
				</AspectRatio.Root>
			{/if}
		</div>
	{/if}
</div>
