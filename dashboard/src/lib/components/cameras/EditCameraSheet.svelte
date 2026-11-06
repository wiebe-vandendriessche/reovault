<script lang="ts">
	/** One camera's name, timezone and login, saved into its [[cameras]]
	 * entry in reovault.toml. The id names its backup repository, so it's
	 * shown, not edited. The password is never sent back: leave it empty to
	 * keep it. */
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
	let host = $state('');
	let user = $state('');
	let password = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	// Reset when another camera opens or its saved values change, not on
	// every live refetch, which would wipe what's typed.
	let loadedKey = '';
	$effect(() => {
		const key = camera ? `${camera.id}|${camera.name ?? ''}|${camera.timezone}|${camera.host}|${camera.user}` : '';
		if (key === loadedKey) return;
		loadedKey = key;
		if (camera) {
			name = camera.name ?? '';
			timezone = camera.timezone;
			host = camera.host ?? '';
			user = camera.user;
			password = '';
			error = null;
		}
	});
	// A new address or user needs the password again: it's never sent to a new host on its own.
	const moved = $derived(!!camera && (host !== (camera.host ?? '') || user !== camera.user));
	const dirty = $derived(
		!!camera &&
			(name !== (camera.name ?? '') ||
				timezone !== camera.timezone ||
				host !== (camera.host ?? '') ||
				user !== camera.user ||
				password !== '')
	);
	trackUnsaved(() => dirty);

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!camera || !dirty) return;
		busy = true;
		error = null;
		try {
			onsaved(
				await api<Camera[]>(`/cameras/${camera.id}`, {
					method: 'PUT',
					body: camera.demo
						? { name: name || null, timezone, enabled: camera.enabled }
						: { name: name || null, timezone, enabled: camera.enabled, host, user, password: password || null }
				})
			);
			password = '';
			toast.success('Camera saved to reovault.toml.');
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	let removing = $state(false);
	async function remove() {
		if (!camera) return;
		busy = true;
		error = null;
		try {
			const path = `/cameras/${camera.id}`;
			onsaved(await api<Camera[]>(path, { method: 'DELETE' }));
			toast.success(`${camera.name || camera.id} removed. Its backups are untouched.`);
			removing = false;
			onclose();
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
				<Sheet.Title>{camera.name || camera.id}</Sheet.Title>
				<Sheet.Description>
					<span class="font-mono">{camera.id}</span>. Changes are saved to reovault.toml.
				</Sheet.Description>
			</Sheet.Header>

			{#if !writable}
				<p class="text-warn mx-4 mb-2 text-sm">reovault.toml is read-only, so this camera can't be changed here.</p>
			{/if}

			<form id="edit-camera-form" class="flex flex-col gap-4 p-4" onsubmit={save}>
				<Field.Field>
					<Field.Label for="edit-cam-name">Display name</Field.Label>
					<Input id="edit-cam-name" maxlength={100} placeholder={camera.id} disabled={!writable} bind:value={name} />
				</Field.Field>
				<Field.Field>
					<Field.Label for="edit-cam-tz">Timezone</Field.Label>
					<TimezoneInput id="edit-cam-tz" disabled={!writable} bind:value={timezone} />
					<Field.Description>The camera's own clock, e.g. Europe/Brussels.</Field.Description>
				</Field.Field>
				{#if !camera.demo}
					<Field.Field>
						<Field.Label for="edit-cam-host">Address</Field.Label>
						<Input id="edit-cam-host" required disabled={!writable} bind:value={host} />
					</Field.Field>
					<div class="grid gap-4 sm:grid-cols-2">
						<Field.Field>
							<Field.Label for="edit-cam-user">Username</Field.Label>
							<Input id="edit-cam-user" required autocomplete="off" disabled={!writable} bind:value={user} />
						</Field.Field>
						<Field.Field>
							<Field.Label for="edit-cam-pass">Password</Field.Label>
							<Input id="edit-cam-pass" type="password" autocomplete="new-password" placeholder={moved ? 'Required' : 'Unchanged'} required={moved} disabled={!writable} bind:value={password} />
						</Field.Field>
					</div>
					<Field.Description>
						A new address or user needs the password again, and is checked with the camera before it's saved.
					</Field.Description>
				{/if}
				{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			</form>

			<div class="mx-4 mb-4 mt-2 border-t pt-4">
				{#if !removing}
					<Button variant="outline" size="sm" disabled={!writable} onclick={() => (removing = true)}>
						Remove this camera
					</Button>
				{:else}
					<p class="text-sm">
						Stops backing up {camera.name || camera.id} and removes it, with its password, from
						reovault.toml. Everything already backed up stays in every destination; adding a camera
						with the same id again brings it back.
					</p>
					<div class="mt-3 flex gap-2">
						<Button variant="destructive" size="sm" disabled={busy} onclick={remove}>
							{#if busy}<Spinner />{/if} Remove
						</Button>
						<Button variant="ghost" size="sm" onclick={() => (removing = false)}>Keep it</Button>
					</div>
				{/if}
			</div>

			<Sheet.Footer class="bg-background sticky bottom-0 flex-row items-center gap-3 border-t">
				<Button type="submit" form="edit-camera-form" disabled={!writable || busy || !dirty}>
					{#if busy}<Spinner />{/if} Save
				</Button>
				<span class="text-muted-foreground text-sm">{dirty ? 'Unsaved changes' : 'No changes'}</span>
			</Sheet.Footer>
		{/if}
	</Sheet.Content>
</Sheet.Root>
