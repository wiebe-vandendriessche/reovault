<script lang="ts">
	/** One camera's name and timezone, saved into its [[cameras]] entry in
	 * reovault.toml. The alias names its backup repository, so it's shown,
	 * not edited. */
	import { api, type Camera } from '#lib/api/index.js';
	import * as Sheet from '#lib/components/ui/sheet/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Input } from '#lib/components/ui/input/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import TimezoneInput from '#lib/components/app/TimezoneInput.svelte';
	import { trackUnsaved } from '#lib/state/unsaved.svelte.js';
	import { toast } from 'svelte-sonner';

	let {
		camera,
		writable,
		onclose,
		onsaved
	}: {
		camera: Camera | null;
		writable: boolean;
		onclose: () => void;
		onsaved: (cameras: Camera[]) => void;
	} = $props();

	let name = $state('');
	let timezone = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	// Reset when another camera opens or its saved values change, not on
	// every live refetch, which would wipe what's typed.
	let loadedKey = '';
	$effect(() => {
		const key = camera ? `${camera.alias}|${camera.name ?? ''}|${camera.timezone}` : '';
		if (key === loadedKey) return;
		loadedKey = key;
		if (camera) {
			name = camera.name ?? '';
			timezone = camera.timezone;
			error = null;
		}
	});
	const dirty = $derived(!!camera && (name !== (camera.name ?? '') || timezone !== camera.timezone));
	trackUnsaved(() => dirty);

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!camera || !dirty) return;
		busy = true;
		error = null;
		try {
			onsaved(
				await api<Camera[]>(`/cameras/${camera.alias}`, {
					method: 'PUT',
					body: { name: name || null, timezone, enabled: camera.enabled }
				})
			);
			toast.success('Camera saved to reovault.toml.');
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	function close(open: boolean) {
		if (open) return;
		if (!dirty || confirm('You have unsaved changes to this camera. Discard them?')) onclose();
	}
</script>

<Sheet.Root open={camera !== null} onOpenChange={close}>
	<Sheet.Content side="right" class="gap-0 overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-md">
		{#if camera}
			<Sheet.Header>
				<Sheet.Title>{camera.name || camera.alias}</Sheet.Title>
				<Sheet.Description>
					<span class="font-mono">alias {camera.alias}</span>. Changes are saved to reovault.toml.
				</Sheet.Description>
			</Sheet.Header>

			{#if !writable}
				<p class="text-warn mx-4 mb-2 text-sm">reovault.toml is read-only, so this camera can't be changed here.</p>
			{/if}

			<form id="edit-camera-form" class="flex flex-col gap-4 p-4" onsubmit={save}>
				<Field.Field>
					<Field.Label for="edit-cam-name">Display name</Field.Label>
					<Input id="edit-cam-name" maxlength={100} placeholder={camera.alias} disabled={!writable} bind:value={name} />
				</Field.Field>
				<Field.Field>
					<Field.Label for="edit-cam-tz">Timezone</Field.Label>
					<TimezoneInput id="edit-cam-tz" disabled={!writable} bind:value={timezone} />
					<Field.Description>The camera's own clock, e.g. Europe/Brussels.</Field.Description>
				</Field.Field>
				{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			</form>

			<Sheet.Footer class="bg-background sticky bottom-0 flex-row items-center gap-3 border-t">
				<Button type="submit" form="edit-camera-form" disabled={!writable || busy || !dirty}>
					{#if busy}<Spinner />{/if} Save
				</Button>
				<span class="text-muted-foreground text-sm">{dirty ? 'Unsaved changes' : 'No changes'}</span>
			</Sheet.Footer>
		{/if}
	</Sheet.Content>
</Sheet.Root>
