<script lang="ts">
	import { api, type Growth, type Health } from '$lib/api';
	import { devices } from '$lib/state/devices.svelte';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Alert from '$lib/components/ui/alert';
	import * as Card from '$lib/components/ui/card';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import NoDevice from '$lib/components/app/NoDevice.svelte';
	import StatusCard from '$lib/components/health/StatusCard.svelte';
	import StorageCards from '$lib/components/health/StorageCards.svelte';
	import GrowthCard from '$lib/components/health/GrowthCard.svelte';
	import ActivityCard from '$lib/components/health/ActivityCard.svelte';
	import { bytes, plural } from '$lib/format';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import ArrowRight from '@lucide/svelte/icons/arrow-right';

	const health = query(() => {
		void live.activity;
		void live.problems;
		const device = devices.currentId;
		return device == null ? null : api<Health>('/health', { query: { device } });
	});
	const growth = query(() => {
		void live.activity;
		const device = devices.currentId;
		return device == null ? null : api<Growth>('/growth', { query: { device, days: 30 } });
	});
</script>

<svelte:head><title>Health | ReoVault</title></svelte:head>

<PageHeader title="Health" description="Is everything being archived, and is there room for it." />

{#if devices.loaded && devices.currentId == null}
	<NoDevice />
{:else if health.error}
	<ErrorAlert message={health.error} />
{:else if !health.data}
	<div class="grid gap-4 md:grid-cols-2">
		{#each [0, 1, 2, 3] as i (i)}<Skeleton class="h-40 rounded-xl" />{/each}
	</div>
{:else}
	{@const h = health.data}
	<div class="flex flex-col gap-4">
		{#if h.banner}
			<Alert.Root variant={h.banner.level === 'bad' ? 'destructive' : 'default'} class={h.banner.level === 'warn' ? 'border-warn/40 text-warn' : ''}>
				<TriangleAlert />
				<Alert.Title>{h.banner.text}</Alert.Title>
				{#if h.banner.link === 'problems'}
					<Alert.Description><a href="/problems" class="underline underline-offset-4">Review problems</a></Alert.Description>
				{/if}
			</Alert.Root>
		{/if}

		<div class="grid gap-4 md:grid-cols-2">
			<StatusCard health={h} />
			<a href="/footage?date={h.today.date}" class="group block">
				<Card.Root class="group-hover:border-brand-text h-full transition-colors">
					<Card.Header>
						<Card.Description>Today’s footage</Card.Description>
						<Card.Title class="text-3xl tabular-nums">{plural(h.today.count, 'clip')}</Card.Title>
						<Card.Action><ArrowRight class="text-muted-foreground group-hover:text-foreground size-4 transition-colors" /></Card.Action>
					</Card.Header>
					<Card.Content class="text-muted-foreground text-sm">
						{bytes(h.today.archived_bytes)} archived so far today.
					</Card.Content>
				</Card.Root>
			</a>
			<ActivityCard device={devices.currentId!} />
			<StorageCards health={h} />
			{#if growth.data}
				<GrowthCard growth={growth.data} />
			{:else if growth.error}
				<div class="md:col-span-2"><ErrorAlert message={growth.error} /></div>
			{/if}
		</div>
	</div>
{/if}
