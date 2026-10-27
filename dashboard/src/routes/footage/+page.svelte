<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type Day } from '#lib/api/index.js';
	import { devices } from '#lib/state/devices.svelte.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Empty from '#lib/components/ui/empty/index.js';
	import * as ToggleGroup from '#lib/components/ui/toggle-group/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import NoDevice from '#lib/components/app/NoDevice.svelte';
	import MonthCalendar from '#lib/components/footage/MonthCalendar.svelte';
	import HourGroup from '#lib/components/footage/HourGroup.svelte';
	import ExportDialog from '#lib/components/footage/ExportDialog.svelte';
	import { addDays, bytes, plural, shortDay } from '#lib/format.js';
	import ChevronLeft from '@lucide/svelte/icons/chevron-left';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import VideoOff from '@lucide/svelte/icons/video-off';
	import { typeLabel } from '#lib/labels.js';

	const PROBLEMS = '__problems__';

	// URL is the source of truth for date and filter: shareable, and the
	// back button walks through days.
	const date = $derived(page.url.searchParams.get('date') ?? '');
	const types = $derived(page.url.searchParams.get('types') ?? '');

	const day = query(() => {
		void live.activity;
		void live.problems;
		const device = devices.currentId;
		if (device == null) return null;
		return api<Day>('/day', { query: { device, date_: date || undefined, types } });
	});

	// Without a date in the URL, the server's "today" (camera-local) is used.
	const shown = $derived(date || day.data?.today || '');

	function nav(next: { date?: string; types?: string }) {
		const params = new URLSearchParams(page.url.search);
		if (next.date !== undefined) params.set('date', next.date);
		if (next.types !== undefined) {
			if (next.types) params.set('types', next.types);
			else params.delete('types');
		}
		goto(`?${params}`, { reset: false });
	}

	// Left/right arrows step a day, unless a field or the video player has
	// focus (the player uses them to seek).
	function onkeydown(e: KeyboardEvent) {
		if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || !shown) return;
		const t = e.target as HTMLElement;
		if (t.closest('input, textarea, select, video, [role="dialog"]')) return;
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

<PageHeader title="Footage" description="Browse, play and download archived clips by day.">
	{#snippet actions()}
		{#if devices.currentId != null && shown}
			<ExportDialog device={devices.currentId} day={shown} {types} />
		{/if}
	{/snippet}
</PageHeader>

{#if devices.loaded && devices.currentId == null}
	<NoDevice />
{:else if devices.currentId != null}
	<div class="grid items-start gap-6 lg:grid-cols-[18rem_1fr]">
		<Card.Root class="lg:sticky lg:top-24">
			<Card.Content>
				{#if shown}
					<MonthCalendar device={devices.currentId} selected={shown} onselect={(d) => nav({ date: d })} />
				{:else}
					<Skeleton class="h-64 w-full" />
				{/if}
			</Card.Content>
		</Card.Root>

		<section class="flex min-w-0 flex-col gap-4" aria-label="Clips for {dateLabel}">
			<div class="flex flex-wrap items-center gap-2">
				<Button variant="outline" size="icon" onclick={() => nav({ date: addDays(shown, -1) })} disabled={!shown} aria-label="Previous day" aria-keyshortcuts="ArrowLeft" title="Previous day (Left arrow)">
					<ChevronLeft />
				</Button>
				<Button
					variant="outline"
					size="icon"
					onclick={() => nav({ date: addDays(shown, 1) })}
					disabled={!shown || !day.data || shown >= day.data.today}
					aria-label="Next day"
					aria-keyshortcuts="ArrowRight"
					title="Next day (Right arrow)"
				>
					<ChevronRight />
				</Button>
				<div class="ml-1 min-w-0">
					<h2 class="truncate font-semibold">{dateLabel}</h2>
					{#if day.data}
						<p class="text-muted-foreground flex gap-x-3 text-sm tabular-nums">
							<span>{plural(day.data.total, 'clip')}</span>
							<span>{bytes(day.data.total_bytes)}</span>
						</p>
					{/if}
				</div>
			</div>

			{#if day.data && (day.data.available_types.length || types)}
				<ToggleGroup.Root
					type="single"
					variant="outline"
					size="sm"
					value={types}
					onValueChange={(v) => nav({ types: v ?? '' })}
					class="flex-wrap"
					aria-label="Filter clips"
				>
					{#each day.data.available_types as t (t)}
						<ToggleGroup.Item value={t}>{typeLabel(t)}</ToggleGroup.Item>
					{/each}
					<ToggleGroup.Item value={PROBLEMS} class="data-[state=on]:text-bad">Problems</ToggleGroup.Item>
				</ToggleGroup.Root>
			{/if}

			{#if day.error}
				<ErrorAlert message={day.error} />
			{:else if !day.data}
				{#each [0, 1, 2] as i (i)}<Skeleton class="h-12 w-full rounded-xl" />{/each}
			{:else if day.data.total === 0}
				<Empty.Root class="border">
					<Empty.Header>
						<Empty.Media variant="icon"><VideoOff /></Empty.Media>
						<Empty.Title>{types ? 'No clips match this filter' : 'No clips this day'}</Empty.Title>
						<Empty.Description>
							{types
								? 'Clear the filter to see every clip from this day.'
								: "Nothing was recorded, or it hasn't been archived yet."}
						</Empty.Description>
					</Empty.Header>
					{#if types}
						<Empty.Content><Button variant="outline" onclick={() => nav({ types: '' })}>Clear filter</Button></Empty.Content>
					{:else if day.data.latest && day.data.latest !== shown}
						{@const latest = day.data.latest}
						<Empty.Content>
							<Button variant="outline" onclick={() => nav({ date: latest })}>Go to latest clips ({shortDay(latest)})</Button>
						</Empty.Content>
					{/if}
				</Empty.Root>
			{:else}
				{#each day.data.hours as h (`${day.data.date}-${h.hour}-${types}`)}
					<HourGroup
						device={devices.currentId}
						date={day.data.date}
						hour={h.hour}
						total={h.total}
						size={h.bytes}
						{types}
					/>
				{/each}
			{/if}
		</section>
	</div>
{/if}
