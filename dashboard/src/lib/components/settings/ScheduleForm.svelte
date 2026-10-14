<script lang="ts">
	import { api, type Schedule, type ScheduleBody } from '$lib/api';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import * as InputGroup from '$lib/components/ui/input-group';
	import { NativeSelect } from '$lib/components/ui/native-select';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Switch } from '$lib/components/ui/switch';
	import { Spinner } from '$lib/components/ui/spinner';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import { toast } from 'svelte-sonner';

	import { live } from '$lib/state/live.svelte';

	/** `device` null edits the global default `[schedule]`; a camera id edits
	 * that camera's own `[devices.schedule]` (or switches it back to the
	 * default with "Use the default schedule"). */
	let {
		device,
		label,
		bare = false
	}: {
		device: number | null;
		label: string;
		/** No card chrome or title: for embedding (the camera edit panel). */
		bare?: boolean;
	} = $props();

	const path = $derived(device == null ? '/schedule/default' : '/schedule');
	const loaded = query(() => {
		void live.settings;
		return api<Schedule>(path, { query: { device: device ?? undefined } });
	});
	let form = $state<(ScheduleBody & { inherit: boolean }) | null>(null);
	$effect(() => {
		if (loaded.data) {
			const { env_pinned: _, inherits, ...body } = loaded.data;
			form = { ...body, inherit: device != null && !!inherits };
		}
	});
	let busy = $state(false);
	let error = $state<string | null>(null);
	const pinned = $derived(loaded.data?.env_pinned ?? false);
	const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!form) return;
		busy = true;
		error = null;
		try {
			const body = device == null ? { ...form, inherit: undefined } : form;
			loaded.data = await api<Schedule>(path, {
				method: 'PUT',
				query: { device: device ?? undefined },
				body
			});
			toast.success(`Schedule for ${label} saved to reovault.toml.`);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

{#if loaded.error}
	<ErrorAlert message={loaded.error} />
{:else if !form}
	<Skeleton class="h-96 w-full rounded-xl" />
{:else}
	<form onsubmit={save}>
		<Card.Root class={bare ? 'gap-4 border-0 bg-transparent py-0 shadow-none ring-0 [--card-spacing:0px]' : ''}>
			{#if !bare}
			<Card.Header>
				<Card.Title>{device == null ? 'Default schedule' : `Schedule for ${label}`}</Card.Title>
				<Card.Description>
					{device == null
						? 'Every camera follows this unless it has a schedule of its own.'
						: "Times are in the camera's own timezone"}{form.timezone
						? ` (overridden: ${form.timezone})`
						: ''}.
					{#if pinned}<span class="text-warn">Set by environment variables, so read-only here.</span>{/if}
				</Card.Description>
			</Card.Header>
			{/if}
			{#if device != null}
				<Card.Content class="pb-0">
					<Field.Field orientation="horizontal">
						<Switch id="sched-inherit" bind:checked={form.inherit} disabled={pinned} />
						<Field.Content>
							<Field.Label for="sched-inherit">Use the default schedule</Field.Label>
							<Field.Description>Off gives this camera its own schedule below.</Field.Description>
						</Field.Content>
					</Field.Field>
				</Card.Content>
			{/if}
			<Card.Content>
				<fieldset disabled={pinned || form.inherit} class="flex flex-col gap-6 disabled:opacity-60">
					<Field.Set>
						<Field.Field orientation="horizontal">
							<Switch id="arch-on" bind:checked={form.archive_enabled} />
							<Field.Content>
								<Field.Label for="arch-on">Download new clips daily</Field.Label>
								<Field.Description>The main archive run.</Field.Description>
							</Field.Content>
						</Field.Field>
						<div class="grid gap-4 pl-12 sm:grid-cols-2">
							<Field.Field>
								<Field.Label for="arch-time">At</Field.Label>
								<Input id="arch-time" type="time" required bind:value={form.archive_time} />
							</Field.Field>
							<Field.Field>
								<Field.Label for="arch-overlap">Look back</Field.Label>
								<InputGroup.Root>
									<InputGroup.Input id="arch-overlap" type="number" min="1" max="720" bind:value={form.archive_overlap_hours} />
									<InputGroup.Addon align="inline-end">hours</InputGroup.Addon>
								</InputGroup.Root>
							</Field.Field>
						</div>
					</Field.Set>

					<Field.Set>
						<Field.Field orientation="horizontal">
							<Switch id="bf-on" bind:checked={form.backfill_enabled} />
							<Field.Content>
								<Field.Label for="bf-on">Weekly deep catch-up</Field.Label>
								<Field.Description>Re-scans a longer window for anything a daily run missed.</Field.Description>
							</Field.Content>
						</Field.Field>
						<div class="grid gap-4 pl-12 sm:grid-cols-3">
							<Field.Field>
								<Field.Label for="bf-dow">On</Field.Label>
								<NativeSelect id="bf-dow" bind:value={form.backfill_dow}>
									{#each weekdays as name, i (name)}<option value={i}>{name}</option>{/each}
								</NativeSelect>
							</Field.Field>
							<Field.Field>
								<Field.Label for="bf-time">At</Field.Label>
								<Input id="bf-time" type="time" required bind:value={form.backfill_time} />
							</Field.Field>
							<Field.Field>
								<Field.Label for="bf-days">Covering</Field.Label>
								<InputGroup.Root>
									<InputGroup.Input id="bf-days" type="number" min="1" max="3650" bind:value={form.backfill_days} />
									<InputGroup.Addon align="inline-end">days</InputGroup.Addon>
								</InputGroup.Root>
							</Field.Field>
						</div>
					</Field.Set>

					<Field.Set>
						<Field.Field orientation="horizontal">
							<Switch id="rc-on" bind:checked={form.reconcile_enabled} />
							<Field.Content>
								<Field.Label for="rc-on">Check the vault against the database</Field.Label>
								<Field.Description>Cleans up after crashes; never deletes archived clips.</Field.Description>
							</Field.Content>
						</Field.Field>
						<div class="grid gap-4 pl-12 sm:grid-cols-2">
							<Field.Field>
								<Field.Label for="rc-int">Every</Field.Label>
								<InputGroup.Root>
									<InputGroup.Input id="rc-int" type="number" min="1" max="168" bind:value={form.reconcile_interval_hours} />
									<InputGroup.Addon align="inline-end">hours</InputGroup.Addon>
								</InputGroup.Root>
							</Field.Field>
						</div>
					</Field.Set>

					<Field.Set>
						<Field.Field orientation="horizontal">
							<Switch id="is-on" bind:checked={form.integrity_scan_enabled} />
							<Field.Content>
								<Field.Label for="is-on">Weekly corruption spot-check</Field.Label>
								<Field.Description>Decrypts a random sample and compares hashes.</Field.Description>
							</Field.Content>
						</Field.Field>
						<div class="grid gap-4 pl-12 sm:grid-cols-2">
							<Field.Field>
								<Field.Label for="is-pct">Sample</Field.Label>
								<InputGroup.Root>
									<InputGroup.Input id="is-pct" type="number" min="0.1" max="100" step="0.1" bind:value={form.integrity_scan_sample_pct} />
									<InputGroup.Addon align="inline-end">% of clips</InputGroup.Addon>
								</InputGroup.Root>
							</Field.Field>
						</div>
					</Field.Set>
				</fieldset>
			</Card.Content>
			<Card.Footer class={bare ? 'flex items-center gap-3' : 'flex items-center gap-3 border-t'}>
				<Button type="submit" disabled={busy || pinned}>{#if busy}<Spinner />{/if} Save schedule</Button>
				{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			</Card.Footer>
		</Card.Root>
	</form>
{/if}
