<script lang="ts">
	import { api, apiUrl, type ExportPreview } from '$lib/api';
	import * as Dialog from '$lib/components/ui/dialog';
	import * as Popover from '$lib/components/ui/popover';
	import { RangeCalendar } from '$lib/components/ui/range-calendar';
	import { Button, buttonVariants } from '$lib/components/ui/button';
	import { Spinner } from '$lib/components/ui/spinner';
	import { bytes, plural } from '$lib/format';
	import { parseDate, type DateValue } from '@internationalized/date';
	import type { DateRange } from 'bits-ui';
	import Download from '@lucide/svelte/icons/download';
	import CalendarIcon from '@lucide/svelte/icons/calendar';

	let { device, day, types }: { device: number; day: string; types: string } = $props();

	const MAX_DAYS = 31;
	let open = $state(false);
	let range = $state<DateRange>({ start: undefined, end: undefined });
	let preview = $state<ExportPreview | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(false);

	$effect(() => {
		if (open) range = { start: parseDate(day), end: parseDate(day) };
	});

	const from = $derived(range.start?.toString());
	const to = $derived(range.end?.toString());
	const label = $derived(
		from && to ? (from === to ? from : `${from} to ${to}`) : 'Pick a date range'
	);

	// Preview whenever the range is complete: count and size before
	// committing to a possibly multi-gigabyte download.
	$effect(() => {
		if (!open || !from || !to) return;
		const query = { device, from_: from, to, types };
		loading = true;
		error = null;
		preview = null;
		api<ExportPreview>('/export/preview', { query })
			.then((p) => (preview = p))
			.catch((e) => (error = e instanceof Error ? e.message : String(e)))
			.finally(() => (loading = false));
	});

	const href = $derived(from && to ? apiUrl('/export', { device, from_: from, to, types }) : '#');
	const tooLong = (d: DateValue) => !!range.start && !range.end && Math.abs(d.compare(range.start)) >= MAX_DAYS;
</script>

<Dialog.Root bind:open>
	<Dialog.Trigger class={buttonVariants({ variant: 'outline' })}>
		<Download /> Export
	</Dialog.Trigger>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Header>
			<Dialog.Title>Export clips</Dialog.Title>
			<Dialog.Description>
				Download decrypted clips as a zip, organized by day. Up to {MAX_DAYS} days at a time{types
					? `, only "${types === '__problems__' ? 'problems' : types}"`
					: ''}.
			</Dialog.Description>
		</Dialog.Header>
		<Popover.Root>
			<Popover.Trigger class={buttonVariants({ variant: 'outline', class: 'justify-start font-normal' })}>
				<CalendarIcon /> {label}
			</Popover.Trigger>
			<Popover.Content class="w-auto p-0" align="start">
				<RangeCalendar bind:value={range} numberOfMonths={1} weekStartsOn={1} isDateDisabled={tooLong} />
			</Popover.Content>
		</Popover.Root>
		<div class="text-sm" aria-live="polite">
			{#if loading}
				<span class="text-muted-foreground flex items-center gap-2"><Spinner /> Counting clips</span>
			{:else if error}
				<span class="text-bad">{error}</span>
			{:else if preview}
				{plural(preview.count, 'clip')}, {bytes(preview.bytes)}
			{/if}
		</div>
		<Dialog.Footer>
			{#if preview?.count && !loading}
				<Button href={href} download onclick={() => (open = false)}><Download /> Download zip</Button>
			{:else}
				<Button disabled><Download /> Download zip</Button>
			{/if}
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
