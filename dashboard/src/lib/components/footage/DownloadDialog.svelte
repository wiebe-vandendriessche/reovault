<script lang="ts">
	/** Download a time range as one zip, a folder per camera. Times are each
	 * camera's own clock. The preview says what the zip will hold, and when
	 * some of it sits on a slower destination. */
	import { api, apiUrl, type Camera, type DownloadPreview } from '#lib/api/index.js';
	import * as Dialog from '#lib/components/ui/dialog/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import * as ToggleGroup from '#lib/components/ui/toggle-group/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Input } from '#lib/components/ui/input/index.js';
	import { addDays, bytes, plural } from '#lib/format.js';
	import { typeLabel } from '#lib/labels.js';
	import Download from '@lucide/svelte/icons/download';

	let {
		open = $bindable(),
		cameras,
		camera,
		date,
		today,
		types: available
	}: {
		open: boolean;
		cameras: Camera[];
		camera: string;
		date: string;
		today: string;
		types: string[];
	} = $props();

	let picked = $state<string[]>([]);
	let from = $state('');
	let to = $state('');
	let kinds = $state<string[]>([]);
	let preview = $state<DownloadPreview | null>(null);
	let failed = $state<string | null>(null);

	$effect(() => {
		if (!open) return;
		picked = [camera];
		kinds = [];
		setRange(date, date);
	});

	function setRange(first: string, last: string) {
		from = `${first}T00:00`;
		to = `${addDays(last, 1)}T00:00`;
	}
	const presets = $derived([
		{ label: 'This day', run: () => setRange(date, date) },
		{ label: 'Last 3 days', run: () => setRange(addDays(today, -2), today) },
		{ label: 'Last 7 days', run: () => setRange(addDays(today, -6), today) }
	]);

	const params = $derived({ cameras: picked.join(','), from, to, types: kinds.join(',') });
	const ready = $derived(picked.length > 0 && !!from && !!to && from < to);
	$effect(() => {
		const p = params;
		if (!open || !ready) {
			preview = null;
			return;
		}
		let live = true;
		const t = setTimeout(
			() =>
				api<DownloadPreview>('/download/preview', { query: p })
					.then((r) => live && ((preview = r), (failed = null)))
					.catch((e) => live && ((preview = null), (failed = e instanceof Error ? e.message : String(e)))),
			250
		);
		return () => {
			live = false;
			clearTimeout(t);
		};
	});
	const slow = $derived(preview?.cameras.reduce((n, c) => n + c.on_slow_destination, 0) ?? 0);
	const away = $derived(preview?.cameras.reduce((n, c) => n + c.on_unconnected_destination, 0) ?? 0);

	function toggle(alias: string) {
		picked = picked.includes(alias) ? picked.filter((a) => a !== alias) : [...picked, alias];
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content>
		<Dialog.Header>
			<Dialog.Title>Download footage</Dialog.Title>
			<Dialog.Description>One zip with a folder per camera, from what is backed up.</Dialog.Description>
		</Dialog.Header>
		<div class="flex flex-col gap-4">
			<div class="flex flex-wrap gap-2">
				{#each presets as p (p.label)}
					<Button size="sm" variant="outline" onclick={p.run}>{p.label}</Button>
				{/each}
			</div>
			<div class="grid gap-4 sm:grid-cols-2">
				<Field.Field>
					<Field.Label for="dl-from">From</Field.Label>
					<Input id="dl-from" type="datetime-local" bind:value={from} />
				</Field.Field>
				<Field.Field>
					<Field.Label for="dl-to">Until</Field.Label>
					<Input id="dl-to" type="datetime-local" bind:value={to} />
				</Field.Field>
			</div>
			{#if cameras.length > 1}
				<fieldset class="flex flex-wrap gap-x-4 gap-y-2">
					<legend class="text-sm font-medium">Cameras</legend>
					{#each cameras as c (c.alias)}
						<label class="flex items-center gap-2 text-sm">
							<input type="checkbox" checked={picked.includes(c.alias)} onchange={() => toggle(c.alias)} />
							{c.name || c.alias}
						</label>
					{/each}
				</fieldset>
			{/if}
			{#if available.length}
				<div class="flex flex-col gap-1.5">
					<span class="text-sm font-medium">Only</span>
					<ToggleGroup.Root type="multiple" variant="outline" size="sm" bind:value={kinds} class="flex-wrap" aria-label="Detection types">
						{#each available as t (t)}<ToggleGroup.Item value={t}>{typeLabel(t)}</ToggleGroup.Item>{/each}
					</ToggleGroup.Root>
					<p class="text-subtle text-xs">Nothing selected means every clip.</p>
				</div>
			{/if}
			<p class="text-sm" aria-live="polite">
				{#if failed}<span class="text-bad">{failed}</span>
				{:else if !ready}<span class="text-muted-foreground">Pick a camera and a range.</span>
				{:else if preview}
					<span class="font-medium">{plural(preview.clips, 'clip')}, {bytes(preview.bytes)}</span>
					{#if slow}<span class="text-muted-foreground block">{plural(slow, 'clip')} sit only on a slower destination, so this takes longer.</span>{/if}
					{#if away}<span class="text-warn block">{plural(away, 'clip')} are only on a destination that isn't connected now and are skipped.</span>{/if}
				{:else}<span class="text-muted-foreground">Counting...</span>{/if}
			</p>
		</div>
		<Dialog.Footer>
			<Button href={apiUrl('/download/zip', params)} disabled={!ready || !preview?.clips}>
				<Download /> Download zip
			</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
