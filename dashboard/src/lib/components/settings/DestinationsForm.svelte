<script lang="ts">
	/** Where backups go. One destination is the primary (cameras back up to it
	 * first); every other one receives copies (3-2-1). Each can have a keep-days
	 * and a size limit. Changes that can delete backups ask for the password. */
	import { api, ApiError, type Destination, type DestinationKind, type LimitPreview } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import DestinationCard from '#lib/components/overview/DestinationCard.svelte';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Dialog from '#lib/components/ui/dialog/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import * as InputGroup from '#lib/components/ui/input-group/index.js';
	import * as NativeSelect from '#lib/components/ui/native-select/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Input } from '#lib/components/ui/input/index.js';
	import { Switch } from '#lib/components/ui/switch/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import { bytes, count } from '#lib/format.js';
	import { toast } from 'svelte-sonner';
	import Plus from '@lucide/svelte/icons/plus';

	let { writable }: { writable: boolean } = $props();

	const list = query(() => {
		void live.settings;
		void live.activity;
		return api<Destination[]>('/destinations');
	});
	const kinds = query(() => api<DestinationKind[]>('/destinations/kinds'));

	let busy = $state(false);
	let error = $state<string | null>(null);
	const fail = (e: unknown) => (error = e instanceof ApiError ? e.message : 'Something went wrong.');

	// Connect
	let connecting = $state(false);
	let name = $state('');
	let kind = $state('local');
	let path = $state('');
	let values = $state<Record<string, string>>({});
	let keep = $state(0);
	let max = $state(0);
	const kindInfo = $derived(kinds.data?.find((k) => k.id === kind));

	function openConnect() {
		name = path = '';
		kind = 'local';
		values = {};
		keep = max = 0;
		error = null;
		connecting = true;
	}
	function onKind() {
		values = Object.fromEntries((kindInfo?.fields ?? []).map((f) => [f.key, f.default]));
	}
	async function connect(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			list.data = await api<Destination[]>('/destinations', {
				method: 'POST',
				body: { name, kind, path, values, keep_days: keep, max_gb: max }
			});
			connecting = false;
			toast.success(`${name} connected.`);
		} catch (err) {
			fail(err);
		} finally {
			busy = false;
		}
	}

	// Edit / make primary / disconnect, all behind the password.
	type Action = { kind: 'edit' | 'primary' | 'disconnect'; dest: Destination };
	let action = $state<Action | null>(null);
	let password = $state('');
	let eKeep = $state(0);
	let eMax = $state(0);
	let eOn = $state(true);
	let preview = $state<LimitPreview | null>(null);

	function open(kind: Action['kind'], dest: Destination) {
		action = { kind, dest };
		password = '';
		error = null;
		eKeep = dest.keep_days;
		eMax = dest.max_gb;
		eOn = dest.role !== 'off';
		preview = null;
	}
	$effect(() => {
		if (action?.kind !== 'edit') return;
		const { name: n } = action.dest;
		const k = eKeep;
		const m = eMax;
		api<LimitPreview>(`/destinations/${n}/preview`, { query: { keep_days: k, max_gb: m } })
			.then((p) => (preview = p))
			.catch(() => (preview = null));
	});
	async function run(e: SubmitEvent) {
		e.preventDefault();
		if (!action) return;
		busy = true;
		error = null;
		const base = `/destinations/${action.dest.name}`;
		const confirm_password = password || null;
		try {
			if (action.kind === 'edit')
				list.data = await api<Destination[]>(base, {
					method: 'PUT',
					body: { keep_days: eKeep, max_gb: eMax, enabled: eOn, confirm_password }
				});
			else if (action.kind === 'primary')
				list.data = await api<Destination[]>(`${base}/primary`, { method: 'POST', body: { confirm_password } });
			else list.data = await api<Destination[]>(base, { method: 'DELETE', body: { confirm_password } });
			action = null;
			toast.success('Saved.');
		} catch (err) {
			fail(err);
		} finally {
			busy = false;
		}
	}
	const title = { edit: 'Limits', primary: 'Make primary', disconnect: 'Disconnect' } as const;
</script>

<div class="flex flex-col gap-4">
	<Card.Root>
		<Card.Header>
			<Card.Title>Destinations</Card.Title>
			<Card.Description>
				Cameras back up to the <strong>primary</strong> destination, ideally a local disk. New backups are then copied to
				every other destination, so a dead disk or a deleted account never takes the only copy. Backups are encrypted with
				your restic password before they leave this machine.
			</Card.Description>
			<Card.Action>
				<Button onclick={openConnect} disabled={!writable}><Plus /> Connect</Button>
			</Card.Action>
		</Card.Header>
	</Card.Root>

	{#if list.data}
		<div class="grid gap-4 md:grid-cols-2">
			{#each list.data as d (d.name)}
				<div class="flex flex-col gap-2">
					<DestinationCard destination={d} />
					<div class="flex flex-wrap gap-2">
						<Button size="sm" variant="outline" onclick={() => open('edit', d)} disabled={!writable}>Limits</Button>
						{#if d.role === 'copy'}
							<Button size="sm" variant="outline" onclick={() => open('primary', d)} disabled={!writable}>Make primary</Button>
						{/if}
						<Button size="sm" variant="ghost" onclick={() => open('disconnect', d)} disabled={!writable}>Disconnect</Button>
					</div>
				</div>
			{/each}
		</div>
		{#if list.data.length === 0}
			<p class="text-muted-foreground text-sm">Nothing is backed up until you connect a destination.</p>
		{/if}
	{/if}
</div>

<Dialog.Root open={connecting} onOpenChange={(o) => (connecting = o)}>
	<Dialog.Content class="max-h-[90vh] overflow-y-auto">
		<form onsubmit={connect} class="flex flex-col gap-4">
			<Dialog.Header>
				<Dialog.Title>Connect a destination</Dialog.Title>
				<Dialog.Description>It is tested first, so a wrong password shows up here and not at the first backup.</Dialog.Description>
			</Dialog.Header>
			<Field.Field>
				<Field.Label for="d-name">Name</Field.Label>
				<Input id="d-name" required bind:value={name} placeholder="nas" />
				<Field.Description>Letters, digits, - and _. Can't be changed later.</Field.Description>
			</Field.Field>
			<Field.Field>
				<Field.Label for="d-kind">Type</Field.Label>
				<NativeSelect.Root id="d-kind" bind:value={kind} onchange={onKind}>
					<NativeSelect.Option value="local">Local folder or mounted disk</NativeSelect.Option>
					{#each kinds.data ?? [] as k (k.id)}<NativeSelect.Option value={k.id}>{k.label}</NativeSelect.Option>{/each}
				</NativeSelect.Root>
			</Field.Field>
			{#if kind === 'local'}
				<Field.Field>
					<Field.Label for="d-path">Folder</Field.Label>
					<Input id="d-path" class="font-mono" required bind:value={path} placeholder="/backups" />
					<Field.Description>The path inside the container. Mount the disk there first.</Field.Description>
				</Field.Field>
			{:else if kindInfo}
				{#if kindInfo.help}<p class="text-muted-foreground text-sm">{kindInfo.help}</p>{/if}
				{#if kindInfo.paste}
					<Field.Field>
						<Field.Label for="d-section">rclone section</Field.Label>
						<textarea id="d-section" class="border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 min-h-28 rounded-md border bg-transparent p-2 font-mono text-xs outline-none focus-visible:ring-3" required bind:value={values.section}></textarea>
					</Field.Field>
				{:else}
					{#each kindInfo.fields as f (f.key)}
						<Field.Field>
							<Field.Label for="d-{f.key}">{f.label}</Field.Label>
							<Input id="d-{f.key}" type={f.secret ? 'password' : 'text'} autocomplete="off" required={f.required} bind:value={values[f.key]} />
							{#if f.help}<Field.Description>{f.help}</Field.Description>{/if}
						</Field.Field>
					{/each}
				{/if}
				<Field.Field>
					<Field.Label for="d-path">Folder on the remote</Field.Label>
					<Input id="d-path" class="font-mono" bind:value={path} placeholder="reovault" />
				</Field.Field>
			{/if}
			<div class="grid gap-4 sm:grid-cols-2">
				<Field.Field>
					<Field.Label for="d-keep">Keep for</Field.Label>
					<InputGroup.Root>
						<InputGroup.Input id="d-keep" type="number" min="0" bind:value={keep} />
						<InputGroup.Addon align="inline-end">days</InputGroup.Addon>
					</InputGroup.Root>
					<Field.Description>0 keeps everything.</Field.Description>
				</Field.Field>
				<Field.Field>
					<Field.Label for="d-max">Size limit</Field.Label>
					<InputGroup.Root>
						<InputGroup.Input id="d-max" type="number" min="0" bind:value={max} />
						<InputGroup.Addon align="inline-end">GB</InputGroup.Addon>
					</InputGroup.Root>
					<Field.Description>0 means no limit. The oldest backups go first.</Field.Description>
				</Field.Field>
			</div>
			{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			<Dialog.Footer>
				<Button type="submit" disabled={busy}>{#if busy}<Spinner />{/if} Connect</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>

<Dialog.Root open={action !== null} onOpenChange={(o) => !o && (action = null)}>
	<Dialog.Content>
		{#if action}
			<form onsubmit={run} class="flex flex-col gap-4">
				<Dialog.Header>
					<Dialog.Title>{title[action.kind]}: {action.dest.name}</Dialog.Title>
					<Dialog.Description>
						{#if action.kind === 'edit'}
							Lowering a limit deletes older backups from this destination at the next cleanup.
						{:else if action.kind === 'primary'}
							Cameras will back up to {action.dest.name} first. It must have caught up with the current primary.
						{:else}
							Stops using {action.dest.name}. The backups on it are not deleted.
						{/if}
					</Dialog.Description>
				</Dialog.Header>
				{#if action.kind === 'edit'}
					<div class="grid gap-4 sm:grid-cols-2">
						<Field.Field>
							<Field.Label for="e-keep">Keep for (days)</Field.Label>
							<Input id="e-keep" type="number" min="0" bind:value={eKeep} />
						</Field.Field>
						<Field.Field>
							<Field.Label for="e-max">Size limit (GB)</Field.Label>
							<Input id="e-max" type="number" min="0" bind:value={eMax} />
						</Field.Field>
					</div>
					{#if preview?.snapshots}
						<p class="text-warn text-sm">
							About {count(preview.snapshots)} snapshots ({bytes(preview.bytes)}) would be deleted from {action.dest.name}.
						</p>
					{/if}
					<label class="flex items-center gap-2 text-sm"><Switch bind:checked={eOn} /> In use</label>
				{/if}
				<Field.Field>
					<Field.Label for="a-pw">Your dashboard password</Field.Label>
					<Input id="a-pw" type="password" autocomplete="current-password" bind:value={password} />
					<Field.Description>Asked for when a change could delete backups.</Field.Description>
				</Field.Field>
				{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
				<Dialog.Footer>
					<Button type="submit" variant={action.kind === 'disconnect' ? 'destructive' : 'default'} disabled={busy}>
						{#if busy}<Spinner />{/if} {title[action.kind]}
					</Button>
				</Dialog.Footer>
			</form>
		{/if}
	</Dialog.Content>
</Dialog.Root>
