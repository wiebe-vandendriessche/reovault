<script lang="ts">
	import { api, type Camera, type Config } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as AlertDialog from '#lib/components/ui/alert-dialog/index.js';
	import * as Empty from '#lib/components/ui/empty/index.js';
	import { Switch } from '#lib/components/ui/switch/index.js';
	import { Label } from '#lib/components/ui/label/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import AddCameraDialog from '#lib/components/cameras/AddCameraDialog.svelte';
	import EditCameraSheet from '#lib/components/cameras/EditCameraSheet.svelte';
	import { bytes, count, relative } from '#lib/format.js';
	import { VERDICTS } from '#lib/labels.js';
	import { toast } from 'svelte-sonner';
	import Cctv from '@lucide/svelte/icons/cctv';
	import Pencil from '@lucide/svelte/icons/pencil';

	const cameras = query(() => {
		void live.settings;
		void live.activity;
		return api<Camera[]>('/cameras');
	});
	// Only whether reovault.toml can be written: camera edits go into it.
	const config = query(() => {
		void live.settings;
		return api<Config>('/config');
	});

	let confirmOff = $state<Camera | null>(null);
	let editingAlias = $state<string | null>(null);
	const editing = $derived(cameras.data?.find((c) => c.alias === editingAlias) ?? null);
	const label = (c: Camera) => c.name || c.alias;

	async function setEnabled(c: Camera, enabled: boolean) {
		try {
			cameras.data = await api<Camera[]>(`/cameras/${c.alias}`, {
				method: 'PUT',
				body: { name: c.name ?? null, timezone: c.timezone, enabled }
			});
			toast.success(`${label(c)} turned ${enabled ? 'on' : 'off'}.`);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		}
	}
</script>

<svelte:head><title>Cameras | ReoVault</title></svelte:head>

<PageHeader title="Cameras" description="Every camera ReoVault backs up. Each one gets its own restic repository.">
	{#snippet actions()}
		{#if config.data?.writable}
			<AddCameraDialog onadded={(c) => (cameras.data = c)} />
		{/if}
	{/snippet}
</PageHeader>

{#if cameras.error}
	<ErrorAlert message={cameras.error} />
{:else if !cameras.data}
	<div class="grid gap-4 md:grid-cols-2">{#each [0, 1] as i (i)}<Skeleton class="h-48 rounded-xl" />{/each}</div>
{:else if cameras.data.length === 0}
	<Empty.Root class="border">
		<Empty.Header>
			<Empty.Media variant="icon"><Cctv /></Empty.Media>
			<Empty.Title>No cameras yet</Empty.Title>
			<Empty.Description>Add your first camera, or a demo camera to try ReoVault without hardware.</Empty.Description>
		</Empty.Header>
	</Empty.Root>
{:else}
	<div class="grid gap-4 md:grid-cols-2">
		{#each cameras.data as c (c.alias)}
			{@const verdict = VERDICTS[c.verdict]}
			<Card.Root class={c.enabled ? '' : 'bg-muted/40'}>
				<Card.Header>
					<Card.Title class="truncate">{label(c)}</Card.Title>
					<Card.Description class="truncate">
						{[c.demo ? 'Demo camera' : c.alias, c.timezone].join(', ')}
					</Card.Description>
					<Card.Action class="flex items-center gap-2">
						<Label for="cam-{c.alias}" class="text-muted-foreground text-xs">{c.enabled ? 'On' : 'Off'}</Label>
						<Switch
							id="cam-{c.alias}"
							disabled={!config.data?.writable}
							bind:checked={() => c.enabled, (on) => (on ? setEnabled(c, true) : (confirmOff = c))}
							aria-label="Back up {label(c)}"
						/>
					</Card.Action>
				</Card.Header>
				<Card.Content class="flex flex-col gap-4">
					<div class="flex flex-wrap gap-2">
						{#if c.enabled}
							<StatusBadge level={verdict.level}>{verdict.label}</StatusBadge>
						{:else}
							<StatusBadge level="idle">Off</StatusBadge>
						{/if}
					</div>
					<dl class="grid grid-cols-2 gap-3 text-sm">
						<div>
							<dt class="text-subtle text-xs">Backed up</dt>
							<dd class="flex flex-wrap gap-x-3 font-medium tabular-nums">
								<span>{count(c.backed_up_clips)} clips</span>
								<span>{bytes(c.backed_up_bytes)}</span>
							</dd>
						</div>
						<div>
							<dt class="text-subtle text-xs">Last backup</dt>
							<dd class="flex items-center gap-2">
								{#if c.last_run}
									<RunOutcome run={c.last_run} />
									<span class="text-muted-foreground text-xs">{relative(c.last_run.finished_at)}</span>
								{:else}
									<span class="text-muted-foreground">None yet</span>
								{/if}
							</dd>
						</div>
					</dl>
				</Card.Content>
				<Card.Footer class="justify-end">
					<Button variant="ghost" size="sm" onclick={() => (editingAlias = c.alias)}>
						<Pencil /> Edit
					</Button>
				</Card.Footer>
			</Card.Root>
		{/each}
	</div>
{/if}

<AlertDialog.Root open={confirmOff !== null} onOpenChange={(o) => !o && (confirmOff = null)}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Stop backing up {confirmOff ? label(confirmOff) : ''}?</AlertDialog.Title>
			<AlertDialog.Description>
				Its backups stop. Everything already backed up stays in its repository and can still be
				downloaded, and you can turn it back on any time.
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel>Keep backing up</AlertDialog.Cancel>
			<AlertDialog.Action
				onclick={() => {
					if (confirmOff) setEnabled(confirmOff, false);
					confirmOff = null;
				}}
			>
				Turn off
			</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>

<EditCameraSheet
	camera={editing}
	writable={config.data?.writable ?? false}
	onclose={() => (editingAlias = null)}
	onsaved={(c) => (cameras.data = c)}
/>
