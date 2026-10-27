<script lang="ts">
	import { goto } from '$app/navigation';
	import { api, type Overview } from '#lib/api/index.js';
	import { devices, selectDevice } from '#lib/state/devices.svelte.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Alert from '#lib/components/ui/alert/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import NoDevice from '#lib/components/app/NoDevice.svelte';
	import ArchiveCard from '#lib/components/overview/ArchiveCard.svelte';
	import SdCardTile from '#lib/components/overview/SdCardTile.svelte';
	import { deviceLabel } from '#lib/state/devices.svelte.js';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

	const overview = query(() => {
		void live.activity;
		void live.problems;
		void live.devices;
		return api<Overview>('/overview');
	});

	function open(id: number) {
		selectDevice(id);
		goto('/health');
	}
</script>

<svelte:head><title>Overview | ReoVault</title></svelte:head>

<PageHeader title="Overview" description="Is the archive healthy, and is every SD card's footage safe." />

{#if devices.loaded && devices.list.length === 0}
	<NoDevice />
{:else if overview.error}
	<ErrorAlert message={overview.error} />
{:else if !overview.data}
	<div class="flex flex-col gap-4">
		<Skeleton class="h-48 rounded-xl" />
		<div class="grid gap-4 md:grid-cols-2">
			{#each [0, 1] as i (i)}<Skeleton class="h-56 rounded-xl" />{/each}
		</div>
	</div>
{:else}
	{@const o = overview.data}
	<div class="flex flex-col gap-6">
		{#if o.banner}
			<Alert.Root variant={o.banner.level === 'bad' ? 'destructive' : 'default'} class={o.banner.level === 'warn' ? 'border-warn/40 text-warn' : ''}>
				<TriangleAlert />
				<Alert.Title>{o.banner.text}</Alert.Title>
			</Alert.Root>
		{/if}

		<ArchiveCard vault={o.vault} />

		<section class="flex flex-col gap-3" aria-labelledby="cards-heading">
			<h2 id="cards-heading" class="text-muted-foreground text-sm font-medium">SD cards</h2>
			<div class="grid gap-4 md:grid-cols-2">
				{#each o.cards as c (c.device_id)}
					<SdCardTile
						title={deviceLabel(c)}
						card={c.card}
						sample={c.sample}
						lastRunAt={c.last_run?.finished_at}
						nextRunAt={c.next_archive_utc}
						running={c.running}
						gatewayDown={c.gateway_down}
						onclick={() => open(c.device_id)}
					/>
				{/each}
			</div>
		</section>
	</div>
{/if}
