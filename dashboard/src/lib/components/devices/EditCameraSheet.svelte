<script lang="ts">
	/** Everything about one camera's settings, from its Devices card: its
	 * name and timezone, and whether it follows the default schedule or has
	 * its own. Saved into the camera's [[devices]] entry in reovault.toml.
	 * Alias and channel identify the camera, so they're shown, not edited.
	 * One Save covers both parts; only the parts that changed are written. */
	import { api, type Device, type Devices } from '$lib/api';
	import * as Sheet from '$lib/components/ui/sheet';
	import * as Field from '$lib/components/ui/field';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Separator } from '$lib/components/ui/separator';
	import { Spinner } from '$lib/components/ui/spinner';
	import ScheduleForm from '$lib/components/settings/ScheduleForm.svelte';
	import TimezoneInput from '$lib/components/app/TimezoneInput.svelte';
	import { deviceLabel } from '$lib/state/devices.svelte';
	import { trackUnsaved } from '$lib/state/unsaved.svelte';
	import { toast } from 'svelte-sonner';

	let {
		device,
		writable,
		onclose,
		onsaved
	}: {
		device: Device | null;
		writable: boolean;
		onclose: () => void;
		onsaved: (d: Devices) => void;
	} = $props();

	let name = $state('');
	let timezone = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let schedule = $state<ScheduleForm>();
	let scheduleDirty = $state(false);
	// Reset when another camera opens or its saved values change, not on
	// every fleet refetch (a live update), which would wipe what's typed.
	let loadedKey = '';
	$effect(() => {
		const key = device ? `${device.id}|${device.name ?? ''}|${device.timezone}` : '';
		if (key === loadedKey) return;
		loadedKey = key;
		if (device) {
			name = device.name ?? '';
			timezone = device.timezone;
			error = null;
		} else {
			scheduleDirty = false;
		}
	});
	const cameraDirty = $derived(!!device && (name !== (device.name ?? '') || timezone !== device.timezone));
	const dirty = $derived(cameraDirty || scheduleDirty);
	trackUnsaved(() => dirty);

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!device || !dirty) return;
		busy = true;
		error = null;
		try {
			if (cameraDirty)
				onsaved(
					await api<Devices>(`/devices/${device.id}/settings`, {
						method: 'PUT',
						body: { name: name || null, timezone }
					})
				);
			await schedule?.save();
			toast.success('Camera saved to reovault.toml.');
		} catch (err) {
			// The schedule shows its own error inline.
			if (cameraDirty) error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	function close(open: boolean) {
		if (open) return;
		if (!dirty || confirm('You have unsaved changes to this camera. Discard them?')) onclose();
	}
</script>

<Sheet.Root open={device !== null} onOpenChange={close}>
	<Sheet.Content
		side="right"
		class="gap-0 overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-xl"
	>
		{#if device}
			<Sheet.Header>
				<Sheet.Title>{deviceLabel(device)}</Sheet.Title>
				<Sheet.Description>
					<span class="font-mono">alias {device.alias}</span>. Changes are saved to reovault.toml.
				</Sheet.Description>
			</Sheet.Header>

			{#if !writable}
				<p class="text-warn mx-4 mb-2 text-sm">
					reovault.toml is read-only, so these settings can't be changed here.
				</p>
			{/if}

			<section class="flex flex-col gap-4 p-4" aria-labelledby="edit-camera-heading">
				<h3 id="edit-camera-heading" class="text-sm font-semibold">Camera</h3>
				<form id="edit-camera-form" class="flex flex-col gap-4" onsubmit={save}>
					<Field.Field>
						<Field.Label for="edit-cam-name">Display name</Field.Label>
						<Input
							id="edit-cam-name"
							maxlength={100}
							placeholder={device.alias}
							disabled={!writable}
							bind:value={name}
						/>
					</Field.Field>
					<Field.Field>
						<Field.Label for="edit-cam-tz">Timezone</Field.Label>
						<TimezoneInput id="edit-cam-tz" disabled={!writable} bind:value={timezone} />
						<Field.Description>The camera's own clock, e.g. Europe/Brussels.</Field.Description>
					</Field.Field>
					{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
				</form>
			</section>

			<Separator />

			<section class="flex flex-col gap-2 p-4" aria-labelledby="edit-schedule-heading">
				<h3 id="edit-schedule-heading" class="text-sm font-semibold">Schedule</h3>
				{#key device.id}
					<ScheduleForm
						bind:this={schedule}
						bind:dirty={scheduleDirty}
						device={device.id}
						label={deviceLabel(device)}
						bare
					/>
				{/key}
			</section>

			<Sheet.Footer class="bg-background sticky bottom-0 flex-row items-center gap-3 border-t">
				<Button type="submit" form="edit-camera-form" disabled={!writable || busy || !dirty}>
					{#if busy}<Spinner />{/if} Save
				</Button>
				<span class="text-muted-foreground text-sm">{dirty ? 'Unsaved changes' : 'No changes'}</span>
			</Sheet.Footer>
		{/if}
	</Sheet.Content>
</Sheet.Root>
