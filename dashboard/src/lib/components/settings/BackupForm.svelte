<script lang="ts">
	/** [backup] in reovault.toml: how often cameras are backed up. Where the
	 * backups go and how long they stay is per destination (Destinations tab). */
	import { api, type Config } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import * as InputGroup from '#lib/components/ui/input-group/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import { trackUnsaved } from '#lib/state/unsaved.svelte.js';
	import { toast } from 'svelte-sonner';

	let { config, onsaved }: { config: Config; onsaved: (c: Config) => void } = $props();

	const field = $derived(config.sections.find((s) => s.path.join('.') === 'backup')?.fields.find((f) => f.key === 'interval_minutes'));
	const saved = $derived((field?.value as number) ?? 60);
	let interval = $state(60);
	let seen = $state<number | null>(null);
	$effect(() => {
		if (seen === saved) return; // a live refetch must not wipe what's typed
		seen = saved;
		interval = saved;
	});
	const dirty = $derived(interval !== saved);
	trackUnsaved(() => dirty);

	let busy = $state(false);
	let error = $state<string | null>(null);

	async function save(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			onsaved(await api<Config>('/config/backup', { method: 'PUT', body: { version: config.version, values: { interval_minutes: interval } } }));
			toast.success('Backup settings saved.');
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<form onsubmit={save}>
	<Card.Root>
		<Card.Header>
			<Card.Title>Schedule</Card.Title>
			<Card.Description>How often each camera's SD card is read and new clips are backed up.</Card.Description>
		</Card.Header>
		<Card.Content class="flex flex-col gap-4">
			<Field.Field class="max-w-xs">
				<Field.Label for="bk-interval">Back up every</Field.Label>
				<InputGroup.Root>
					<InputGroup.Input id="bk-interval" type="number" min="5" max="1440" required disabled={!field?.editable} bind:value={interval} />
					<InputGroup.Addon align="inline-end">minutes</InputGroup.Addon>
				</InputGroup.Root>
				<Field.Description>A run that takes longer simply continues.</Field.Description>
			</Field.Field>
			{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
		</Card.Content>
		<Card.Footer class="gap-3">
			<Button type="submit" disabled={busy || !dirty || !config.writable}>{#if busy}<Spinner />{/if} Save</Button>
			<span class="text-muted-foreground text-sm">{dirty ? 'Unsaved changes' : 'No changes'}</span>
		</Card.Footer>
	</Card.Root>
</form>
