<script lang="ts">
	import { api, type Device, type Devices } from '$lib/api';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import { deviceLabel } from '$lib/state/devices.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import * as Empty from '$lib/components/ui/empty';
	import { Switch } from '$lib/components/ui/switch';
	import { Label } from '$lib/components/ui/label';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import StatusBadge from '$lib/components/app/StatusBadge.svelte';
	import Meter from '$lib/components/app/Meter.svelte';
	import RunOutcome from '$lib/components/runs/RunOutcome.svelte';
	import AddCameraDialog from '$lib/components/devices/AddCameraDialog.svelte';
	import { bytes, relative } from '$lib/format';
	import { toast } from 'svelte-sonner';
	import Cctv from '@lucide/svelte/icons/cctv';

	const fleet = query(() => {
		void live.devices;
		return api<Devices>('/devices');
	});

	let confirmDisable = $state<Device | null>(null);

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
			<Card.Root class={d.enabled ? '' : 'opacity-70'}>
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
						{:else if d.gateway_down}
							<StatusBadge level="warn">Gateway unreachable</StatusBadge>
						{:else}
							<StatusBadge level="ok">Archiving</StatusBadge>
						{/if}
						{#if d.sample?.mounted === false}<StatusBadge level="bad">SD card not mounted</StatusBadge>{/if}
					</div>
					<dl class="grid grid-cols-2 gap-3 text-sm">
						<div>
							<dt class="text-subtle text-xs">Archived</dt>
							<dd class="font-medium tabular-nums">{d.archived_count.toLocaleString()} clips<span class="sep"></span>{bytes(d.archived_bytes)}</dd>
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
						</div>
					{/if}
				</Card.Content>
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
