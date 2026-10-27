<script lang="ts">
	import { api, type Config, type Device, type Devices } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import { deviceLabel } from '#lib/state/devices.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as AlertDialog from '#lib/components/ui/alert-dialog/index.js';
	import * as Empty from '#lib/components/ui/empty/index.js';
	import { Switch } from '#lib/components/ui/switch/index.js';
	import { Label } from '#lib/components/ui/label/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import Meter from '#lib/components/app/Meter.svelte';
	import RunOutcome from '#lib/components/runs/RunOutcome.svelte';
	import AddCameraDialog from '#lib/components/devices/AddCameraDialog.svelte';
	import { bytes, count, plural, relative, span } from '#lib/format.js';
	import Legend from '#lib/components/app/Legend.svelte';
	import { toast } from 'svelte-sonner';
	import Cctv from '@lucide/svelte/icons/cctv';
	import Pencil from '@lucide/svelte/icons/pencil';
	import { Button } from '#lib/components/ui/button/index.js';
	import EditCameraSheet from '#lib/components/devices/EditCameraSheet.svelte';

	const fleet = query(() => {
		void live.devices;
		return api<Devices>('/devices');
	});

	let confirmDisable = $state<Device | null>(null);
	let editingId = $state<number | null>(null);
	const editing = $derived(fleet.data?.devices.find((d) => d.id === editingId) ?? null);
	// Only whether reovault.toml can be written: camera edits go into it.
	const config = query(() => {
		void live.settings;
		return api<Config>('/config');
	});

	async function setEnabled(d: Device, enabled: boolean) {
		try {
			fleet.data = await api<Devices>(`/devices/${d.id}/enabled`, { method: 'PUT', body: { enabled } });
			toast.success(`${deviceLabel(d)} ${enabled ? 'enabled' : 'disabled'}.`);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		}
	}

	const gb = (n: number) => `${n.toFixed(1)} GB`;
</script>

<svelte:head><title>Devices | ReoVault</title></svelte:head>

<PageHeader title="Devices" description="Every camera ReoVault knows about, across the whole fleet.">
	{#snippet actions()}
		{#if fleet.data?.registry_available}
			<AddCameraDialog onadded={(d) => (fleet.data = d)} />
		{/if}
	{/snippet}
</PageHeader>

{#if fleet.error}
	<ErrorAlert message={fleet.error} />
{:else if !fleet.data}
	<div class="grid gap-4 md:grid-cols-2">{#each [0, 1] as i (i)}<Skeleton class="h-56 rounded-xl" />{/each}</div>
{:else if fleet.data.devices.length === 0}
	<Empty.Root class="border">
		<Empty.Header>
			<Empty.Media variant="icon"><Cctv /></Empty.Media>
			<Empty.Title>No cameras yet</Empty.Title>
			<Empty.Description>Add your first camera to start archiving its recordings.</Empty.Description>
		</Empty.Header>
	</Empty.Root>
{:else}
	<div class="grid gap-4 md:grid-cols-2">
		{#each fleet.data.devices as d (d.id)}
			<Card.Root class={d.enabled ? '' : 'bg-muted/40'}>
				<Card.Header>
					<Card.Title class="truncate">{deviceLabel(d)}</Card.Title>
					<Card.Description class="truncate">
						{[d.model, d.host, d.timezone].filter(Boolean).join(', ')}
					</Card.Description>
					<Card.Action class="flex items-center gap-2">
						<Label for="dev-{d.id}" class="text-muted-foreground text-xs">{d.enabled ? 'On' : 'Off'}</Label>
						<Switch
							id="dev-{d.id}"
							bind:checked={
								() => d.enabled, (on) => (on ? setEnabled(d, true) : (confirmDisable = d))
							}
							aria-label="Archive {deviceLabel(d)}"
						/>
					</Card.Action>
				</Card.Header>
				<Card.Content class="flex flex-col gap-4">
					<div class="flex flex-wrap gap-2">
						{#if !d.enabled}
							<StatusBadge level="idle">Disabled</StatusBadge>
						{:else}
							{#if d.card_state === 'not_mounted'}
								<StatusBadge level="bad">SD card not mounted</StatusBadge>
							{:else if d.card_state === 'at_risk'}
								<StatusBadge level="warn">At risk: overwritten in about {span(d.safe_for_seconds)}</StatusBadge>
							{:else if d.card_state === 'waiting'}
								<StatusBadge level="idle">{d.waiting_count} waiting</StatusBadge>
							{:else if d.card_state === 'all_archived'}
								<StatusBadge level="ok">All archived</StatusBadge>
							{:else}
								<StatusBadge level="idle">No data yet</StatusBadge>
							{/if}
							{#if d.gateway_down}<StatusBadge level="warn">Gateway unreachable</StatusBadge>{/if}
							{#if d.problems_count}
								<StatusBadge level="bad" href="/problems?device={d.id}">{plural(d.problems_count, 'problem')}</StatusBadge>
							{/if}
						{/if}
						{#if d.has_own_schedule}<StatusBadge level="idle">Own schedule</StatusBadge>{/if}
					</div>
					<dl class="grid grid-cols-2 gap-3 text-sm">
						<div>
							<dt class="text-subtle text-xs">Archived</dt>
							<dd class="flex flex-wrap gap-x-3 font-medium tabular-nums">
								<span>{count(d.archived_count)} clips</span>
								<span>{bytes(d.archived_bytes)}</span>
							</dd>
						</div>
						<div>
							<dt class="text-subtle text-xs">Last run</dt>
							<dd class="flex items-center gap-2">
								{#if d.last_run}
									<RunOutcome run={d.last_run} />
									<span class="text-muted-foreground text-xs">{relative(d.last_run.finished_at)}</span>
								{:else}
									<span class="text-muted-foreground">None yet</span>
								{/if}
							</dd>
						</div>
					</dl>
					{#if d.sd}
						<div class="flex flex-col gap-1.5">
							<div class="text-subtle flex justify-between text-xs">
								<span>SD card</span>
								<span class="tabular-nums">{gb(d.sd.used_gb)} / {gb(d.sd.total_gb)}</span>
							</div>
							<Meter
								label="SD card: {gb(d.sd.archived_gb)} archived, {gb(d.sd.other_gb)} other, {gb(d.sd.free_gb)} free"
								segments={[
									{ value: (d.sd.archived_gb / d.sd.total_gb) * 100, class: 'fill-brand' },
									{ value: (d.sd.other_gb / d.sd.total_gb) * 100, class: 'fill-idle' }
								]}
							/>
							<Legend
								items={[
									{ label: `Archived ${gb(d.sd.archived_gb)}`, class: 'bg-brand' },
									{ label: `Other ${gb(d.sd.other_gb)}`, class: 'bg-idle' },
									{ label: `Free ${gb(d.sd.free_gb)}`, class: 'bg-muted-foreground/30' }
								]}
							/>
						</div>
					{/if}
				</Card.Content>
				<Card.Footer class="justify-end">
					<Button variant="ghost" size="sm" onclick={() => (editingId = d.id)}>
						<Pencil /> Edit
					</Button>
				</Card.Footer>
			</Card.Root>
		{/each}
	</div>
{/if}

<AlertDialog.Root open={confirmDisable !== null} onOpenChange={(o) => !o && (confirmDisable = null)}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Stop archiving {confirmDisable ? deviceLabel(confirmDisable) : ''}?</AlertDialog.Title>
			<AlertDialog.Description>
				Its scheduled runs stop. Everything already archived stays in the vault and stays playable,
				and you can turn it back on any time.
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel>Keep archiving</AlertDialog.Cancel>
			<AlertDialog.Action
				onclick={() => {
					if (confirmDisable) setEnabled(confirmDisable, false);
					confirmDisable = null;
				}}
			>
				Disable
			</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>

<EditCameraSheet
	device={editing}
	writable={config.data?.writable ?? false}
	onclose={() => (editingId = null)}
	onsaved={(d) => (fleet.data = d)}
/>
