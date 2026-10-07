<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { devices, deviceLabel, currentDevice } from '$lib/state/devices.svelte';
	import * as Tabs from '$lib/components/ui/tabs';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import NoDevice from '$lib/components/app/NoDevice.svelte';
	import ScheduleForm from '$lib/components/settings/ScheduleForm.svelte';
	import RetentionForm from '$lib/components/settings/RetentionForm.svelte';
	import AlertsForm from '$lib/components/settings/AlertsForm.svelte';

	const tabs = ['schedule', 'retention', 'alerts'] as const;
	const tab = $derived(
		tabs.find((t) => t === page.url.searchParams.get('tab')) ?? 'schedule'
	);
	const cam = $derived(currentDevice());
</script>

<svelte:head><title>Settings | ReoVault</title></svelte:head>

<PageHeader title="Settings" description="When ReoVault archives, how long it keeps clips, and who it tells." />

<Tabs.Root value={tab} onValueChange={(v) => goto(`?tab=${v}`, { replaceState: true, noScroll: true })}>
	<Tabs.List class="mb-4">
		<Tabs.Trigger value="schedule">Schedule</Tabs.Trigger>
		<Tabs.Trigger value="retention">Retention</Tabs.Trigger>
		<Tabs.Trigger value="alerts">Alerts</Tabs.Trigger>
	</Tabs.List>
	<Tabs.Content value="schedule">
		{#if devices.loaded && !cam}
			<NoDevice />
		{:else if cam}
			{#key cam.id}<ScheduleForm device={cam.id} label={deviceLabel(cam)} />{/key}
		{/if}
	</Tabs.Content>
	<Tabs.Content value="retention"><RetentionForm /></Tabs.Content>
	<Tabs.Content value="alerts"><AlertsForm /></Tabs.Content>
</Tabs.Root>
