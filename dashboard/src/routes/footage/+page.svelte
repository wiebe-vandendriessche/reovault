<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, apiUrl, type Camera, type Day } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Empty from '#lib/components/ui/empty/index.js';
	import * as ToggleGroup from '#lib/components/ui/toggle-group/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import NoCamera from '#lib/components/app/NoCamera.svelte';
	import CameraPicker from '#lib/components/app/CameraPicker.svelte';
	import MonthCalendar from '#lib/components/footage/MonthCalendar.svelte';
	import HourGroup from '#lib/components/footage/HourGroup.svelte';
	import { addDays, bytes, plural } from '#lib/format.js';
	import { typeLabel } from '#lib/labels.js';
	import ChevronLeft from '@lucide/svelte/icons/chevron-left';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import Download from '@lucide/svelte/icons/download';
	import VideoOff from '@lucide/svelte/icons/video-off';

	// The URL holds camera, date and filter: shareable, and the back button
	// walks through days.
	const params = $derived(page.url.searchParams);
	const types = $derived(params.get('types') ?? '');
	const date = $derived(params.get('date') ?? '');

	const cameras = query(() => {
		void live.settings;
		return api<Camera[]>('/cameras');
	});
	const camera = $derived(params.get('camera') ?? cameras.data?.[0]?.alias ?? '');

	const day = query(() => {
		void live.activity;
		if (!camera) return null;
		return api<Day>('/clips/day', { query: { camera, date_: date || undefined, types } });
	});

	// Without a date in the URL, the camera's own "today" is shown.
	const shown = $derived(date || day.data?.today || '');
	const clips = $derived(day.data?.hours.flatMap((h) => h.clips) ?? []);
	const backedUp = $derived(clips.filter((c) => c.state === 'backed_up').length);

	function nav(next: { camera?: string; date?: string; types?: string }) {
		const p = new URLSearchParams(page.url.search);
		for (const [k, v] of Object.entries(next)) {
			if (v) p.set(k, v);
			else p.delete(k);
		}
		goto(`?${p}`, { reset: false });
	}

	// Left/right arrows step a day, unless a field has focus.
	function onkeydown(e: KeyboardEvent) {
		if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || !shown) return;
		if ((e.target as HTMLElement).closest('input, textarea, select, [role="dialog"]')) return;
		if (e.key === 'ArrowLeft') nav({ date: addDays(shown, -1) });
		else if (e.key === 'ArrowRight' && day.data && shown < day.data.today) nav({ date: addDays(shown, 1) });
		else return;
		e.preventDefault();
	}

	const dateLabel = $derived(
		shown
			? new Date(`${shown}T00:00:00Z`).toLocaleDateString('en-GB', {
					weekday: 'long',
					day: 'numeric',
					month: 'long',
					year: 'numeric',
					timeZone: 'UTC'
				})
			: ''
	);
</script>

<svelte:head><title>Footage | ReoVault</title></svelte:head>
<svelte:window {onkeydown} />

<PageHeader
	title="Footage"
	description="Download backed-up clips by day. Clips still on the SD card play in the Reolink app."
>
	{#snippet actions()}
		{#if cameras.data && cameras.data.length > 1}
			<CameraPicker cameras={cameras.data} value={camera} onchange={(c) => nav({ camera: c, date: '' })} />
		{/if}
		{#if backedUp && shown}
			<Button href={apiUrl('/clips/day.zip', { camera, date_: shown, types })} variant="outline">
				<Download /> Download day
			</Button>
		{/if}
	{/snippet}
</PageHeader>

{#if cameras.error}
	<ErrorAlert message={cameras.error} />
{:else if cameras.data && cameras.data.length === 0}
	<NoCamera />
{:else if camera}
	<div class="grid items-start gap-6 lg:grid-cols-[18rem_1fr]">
		<Card.Root class="lg:sticky lg:top-24">
			<Card.Content>
				{#if shown}
					<MonthCalendar {camera} selected={shown} onselect={(d) => nav({ date: d })} />
				{:else}
					<Skeleton class="h-64 w-full" />
				{/if}
			</Card.Content>
		</Card.Root>

		<section class="flex min-w-0 flex-col gap-4" aria-label="Clips for {dateLabel}">
			<div class="flex flex-wrap items-center gap-2">
				<Button variant="outline" size="icon" onclick={() => nav({ date: addDays(shown, -1) })} disabled={!shown} aria-label="Previous day" aria-keyshortcuts="ArrowLeft">
					<ChevronLeft />
				</Button>
				<Button
					variant="outline"
					size="icon"
					onclick={() => nav({ date: addDays(shown, 1) })}
					disabled={!shown || !day.data || shown >= day.data.today}
					aria-label="Next day"
					aria-keyshortcuts="ArrowRight"
				>
					<ChevronRight />
				</Button>
				<div class="ml-1 min-w-0">
					<h2 class="truncate font-semibold">{dateLabel}</h2>
					{#if day.data}
						<p class="text-muted-foreground flex gap-x-3 text-sm tabular-nums">
							<span>{plural(clips.length, 'clip')}</span>
							<span>{bytes(clips.reduce((n, c) => n + (c.size ?? 0), 0))}</span>
						</p>
					{/if}
				</div>
			</div>

			{#if day.data && (day.data.types.length || types)}
				<ToggleGroup.Root
					type="single"
					variant="outline"
					size="sm"
					value={types}
					onValueChange={(v) => nav({ types: v ?? '' })}
					class="flex-wrap"
					aria-label="Filter clips"
				>
					{#each day.data.types as t (t)}
						<ToggleGroup.Item value={t}>{typeLabel(t)}</ToggleGroup.Item>
					{/each}
				</ToggleGroup.Root>
			{/if}

			{#if day.error}
				<ErrorAlert message={day.error} />
			{:else if !day.data}
				{#each [0, 1, 2] as i (i)}<Skeleton class="h-12 w-full rounded-xl" />{/each}
			{:else if clips.length === 0}
				<Empty.Root class="border">
					<Empty.Header>
						<Empty.Media variant="icon"><VideoOff /></Empty.Media>
						<Empty.Title>{types ? 'No clips match this filter' : 'No clips this day'}</Empty.Title>
						<Empty.Description>
							{types ? 'Clear the filter to see every clip from this day.' : 'Nothing was recorded, or the card has not been read yet.'}
						</Empty.Description>
					</Empty.Header>
					{#if types}
						<Empty.Content><Button variant="outline" onclick={() => nav({ types: '' })}>Clear filter</Button></Empty.Content>
					{/if}
				</Empty.Root>
			{:else}
				{#each day.data.hours as h (`${day.data.day}-${h.hour}-${types}`)}
					<HourGroup hour={h.hour} clips={h.clips} />
				{/each}
			{/if}
		</section>
	</div>
{/if}
