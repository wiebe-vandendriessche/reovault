<script lang="ts">
	/** [backup] in reovault.toml: where backups go, how often, how long they
	 * stay. Saved through the generic config endpoint, so env-set fields stay
	 * locked exactly like everywhere else in Settings. */
	import { api, type Config, type DestinationCheck, type RetentionPreview } from '#lib/api/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import * as InputGroup from '#lib/components/ui/input-group/index.js';
	import * as AlertDialog from '#lib/components/ui/alert-dialog/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Input } from '#lib/components/ui/input/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import { bytes, count } from '#lib/format.js';
	import { trackUnsaved } from '#lib/state/unsaved.svelte.js';
	import { toast } from 'svelte-sonner';

	let { config, onsaved }: { config: Config; onsaved: (c: Config) => void } = $props();

	const fields = $derived(
		new Map((config.sections.find((s) => s.path.join('.') === 'backup')?.fields ?? []).map((f) => [f.key, f]))
	);
	const editable = (key: string) => fields.get(key)?.editable ?? false;

	let repository = $state('');
	let interval = $state(60);
	let keep = $state(0);
	let saved = $state('');
	$effect(() => {
		const next = JSON.stringify([fields.get('repository')?.value, fields.get('interval_minutes')?.value, fields.get('keep_days')?.value]);
		if (next === saved) return; // a live refetch must not wipe what's typed
		saved = next;
		repository = (fields.get('repository')?.value as string | null) ?? '';
		interval = (fields.get('interval_minutes')?.value as number) ?? 60;
		keep = (fields.get('keep_days')?.value as number) ?? 0;
	});
	const savedKeep = $derived((fields.get('keep_days')?.value as number) ?? 0);
	const dirty = $derived(
		JSON.stringify([repository || null, interval, keep]) !== JSON.stringify(JSON.parse(saved || '[null,60,0]'))
	);
	trackUnsaved(() => dirty);

	let busy = $state(false);
	let error = $state<string | null>(null);
	let checks = $state<DestinationCheck[] | null>(null);
	let testing = $state(false);
	let confirm = $state<RetentionPreview | null>(null);

	async function test() {
		testing = true;
		checks = null;
		try {
			checks = await api<DestinationCheck[]>('/config/backup/test', { method: 'POST', body: { repository } });
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			testing = false;
		}
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		// Shortening retention deletes footage at the next daily cleanup: say how much first.
		if (keep && (!savedKeep || keep < savedKeep)) {
			const preview = await api<RetentionPreview>('/config/backup/retention-preview', { query: { keep_days: keep } });
			if (preview.clips) return void (confirm = preview);
		}
		await save();
	}

	async function save() {
		confirm = null;
		busy = true;
		error = null;
		const values: Record<string, unknown> = {};
		if (editable('repository')) values.repository = repository || null;
		if (editable('interval_minutes')) values.interval_minutes = interval;
		if (editable('keep_days')) values.keep_days = keep;
		try {
			onsaved(await api<Config>('/config/backup', { method: 'PUT', body: { version: config.version, values } }));
			toast.success('Backup settings saved.');
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const stateText: Record<DestinationCheck['state'], [string, 'ok' | 'idle' | 'bad']> = {
		ok: ['Repository found', 'ok'],
		missing: ['Created by the first backup', 'idle'],
		wrong_password: ['Wrong restic password', 'bad'],
		error: ['Not reachable', 'bad']
	};
</script>

<form onsubmit={submit}>
	<Card.Root>
		<Card.Header>
			<Card.Title>Backups</Card.Title>
			<Card.Description>
				Each camera is backed up into its own restic repository inside the destination. A local path,
				or an rclone remote from your rclone.conf such as <span class="font-mono">rclone:nextcloud:reovault</span>.
			</Card.Description>
		</Card.Header>
		<Card.Content class="flex flex-col gap-5">
			<Field.Field>
				<Field.Label for="bk-repo">Destination</Field.Label>
				<div class="flex flex-wrap gap-2">
					<Input id="bk-repo" class="min-w-0 flex-1 font-mono" bind:value={repository} disabled={!editable('repository')} placeholder="rclone:nextcloud:reovault" />
					<Button type="button" variant="outline" onclick={test} disabled={!repository || testing}>
						{#if testing}<Spinner />{/if} Test
					</Button>
				</div>
				{#if fields.get('repository')?.env_var}
					<Field.Description>Set by {fields.get('repository')?.env_var}.</Field.Description>
				{/if}
				{#if checks}
					<ul class="mt-1 flex flex-col gap-1.5 text-sm">
						{#each checks as c (c.camera)}
							{@const [text, level] = stateText[c.state]}
							<li class="flex flex-wrap items-center gap-2">
								<span class="font-medium">{c.camera}</span>
								<StatusBadge {level}>{text}</StatusBadge>
								{#if c.error}<span class="text-muted-foreground text-xs break-all">{c.error}</span>{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</Field.Field>
			<div class="grid gap-4 sm:grid-cols-2">
				<Field.Field>
					<Field.Label for="bk-interval">Back up every</Field.Label>
					<InputGroup.Root>
						<InputGroup.Input id="bk-interval" type="number" min="5" max="1440" required disabled={!editable('interval_minutes')} bind:value={interval} />
						<InputGroup.Addon align="inline-end">minutes</InputGroup.Addon>
					</InputGroup.Root>
					<Field.Description>A run that takes longer simply continues.</Field.Description>
				</Field.Field>
				<Field.Field>
					<Field.Label for="bk-keep">Keep backups for</Field.Label>
					<InputGroup.Root>
						<InputGroup.Input id="bk-keep" type="number" min="0" required disabled={!editable('keep_days')} bind:value={keep} />
						<InputGroup.Addon align="inline-end">days</InputGroup.Addon>
					</InputGroup.Root>
					<Field.Description>0 keeps everything. Counted from the newest backup, so an outage never deletes history.</Field.Description>
				</Field.Field>
			</div>
			{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
		</Card.Content>
		<Card.Footer class="gap-3">
			<Button type="submit" disabled={busy || !dirty || !config.writable}>{#if busy}<Spinner />{/if} Save</Button>
			<span class="text-muted-foreground text-sm">{dirty ? 'Unsaved changes' : 'No changes'}</span>
		</Card.Footer>
	</Card.Root>
</form>

<AlertDialog.Root open={confirm !== null} onOpenChange={(o) => !o && (confirm = null)}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Delete older backups?</AlertDialog.Title>
			<AlertDialog.Description>
				Keeping {keep} days removes about {count(confirm?.clips ?? 0)} clips ({bytes(confirm?.bytes)}) at the next
				daily cleanup. They can't be restored after that.
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel>Keep them</AlertDialog.Cancel>
			<AlertDialog.Action onclick={save}>Delete after {keep} days</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
