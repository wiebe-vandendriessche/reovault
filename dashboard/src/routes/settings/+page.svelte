<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type Config } from '$lib/api';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Alert from '$lib/components/ui/alert';
	import * as Tabs from '$lib/components/ui/tabs';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import PageHeader from '$lib/components/app/PageHeader.svelte';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import ScheduleForm from '$lib/components/settings/ScheduleForm.svelte';
	import RetentionForm from '$lib/components/settings/RetentionForm.svelte';
	import AlertsForm from '$lib/components/settings/AlertsForm.svelte';
	import ConfigSection from '$lib/components/settings/ConfigSection.svelte';
	import { toast } from 'svelte-sonner';
	import FileWarning from '@lucide/svelte/icons/file-warning';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import Lock from '@lucide/svelte/icons/lock';

	const tabs = ['schedule', 'retention', 'alerts', 'web', 'system'] as const;
	const tab = $derived(tabs.find((t) => t === page.url.searchParams.get('tab')) ?? 'schedule');

	// The whole of reovault.toml; refetched when the file changes, whoever
	// changed it. Edits made to the file itself are announced.
	const config = query(() => {
		void live.settings;
		return api<Config>('/config');
	});
	const seenEdits = live.fileEdits;
	$effect(() => {
		if (live.fileEdits > seenEdits) toast.info('reovault.toml was edited outside the dashboard.');
	});
	function onsaved(c: Config) {
		config.data = c;
	}

	const alertLabels: Record<string, string> = {
		ntfy_url: 'ntfy topic URL',
		ntfy_token_file: 'ntfy token file',
		webhook_url_file: 'Webhook URL file',
		smtp_host: 'SMTP host',
		smtp_port: 'SMTP port',
		smtp_starttls: 'SMTP STARTTLS',
		smtp_user: 'SMTP user',
		smtp_password_file: 'SMTP password file',
		smtp_from: 'From address',
		smtp_to: 'Send to'
	};
</script>

<svelte:head><title>Settings | ReoVault</title></svelte:head>

<PageHeader
	title="Settings"
	description="App-wide settings from reovault.toml: changes are written to the file, and edits to the file show up here. Per-camera settings live on Devices."
/>

{#if config.error}
	<ErrorAlert message={config.error} />
{:else if !config.data}
	<Skeleton class="h-96 w-full rounded-xl" />
{:else}
	{@const c = config.data}
	<div class="mb-4 flex flex-col gap-3">
		{#if c.error}
			<Alert.Root variant="destructive">
				<FileWarning />
				<Alert.Title>reovault.toml has an error</Alert.Title>
				<Alert.Description>
					{c.error}. ReoVault keeps running on the last valid settings until the file is fixed.
				</Alert.Description>
			</Alert.Root>
		{/if}
		{#if c.restart_required.length}
			<Alert.Root class="border-warn/40 text-warn">
				<RotateCw />
				<Alert.Title>Restart ReoVault to apply</Alert.Title>
				<Alert.Description>Changed in reovault.toml: {c.restart_required.join(', ')}.</Alert.Description>
			</Alert.Root>
		{/if}
		{#if !c.writable}
			<Alert.Root>
				<Lock />
				<Alert.Title>Settings are read-only</Alert.Title>
				<Alert.Description>
					{c.path ? `${c.path} can't be written.` : 'No config file is in use.'} Mount the folder that holds
					reovault.toml read-write to edit settings here.
				</Alert.Description>
			</Alert.Root>
		{/if}
	</div>

	<Tabs.Root value={tab} onValueChange={(v) => goto(`?tab=${v}`, { replaceState: true, noScroll: true })}>
		<Tabs.List class="mb-4 flex-wrap">
			<Tabs.Trigger value="schedule">Schedule</Tabs.Trigger>
			<Tabs.Trigger value="retention">Retention</Tabs.Trigger>
			<Tabs.Trigger value="alerts">Alerts</Tabs.Trigger>
			<Tabs.Trigger value="web">Web</Tabs.Trigger>
			<Tabs.Trigger value="system">System</Tabs.Trigger>
		</Tabs.List>

		<Tabs.Content value="schedule" class="flex flex-col gap-3">
			<ScheduleForm device={null} label="all cameras" />
			<p class="text-muted-foreground text-sm">
				A camera can have a schedule of its own:
				<a href="/devices" class="text-brand-text underline-offset-4 hover:underline">Devices</a>,
				then Edit on that camera.
			</p>
		</Tabs.Content>

		<Tabs.Content value="retention"><RetentionForm /></Tabs.Content>

		<Tabs.Content value="alerts" class="flex flex-col gap-4">
			<AlertsForm />
			<ConfigSection
				config={c}
				path={['alerts']}
				section="alerts"
				title="Alert channels"
				description="Point the *_file fields at files holding the secret (Docker secrets); the secrets themselves never go in reovault.toml."
				labels={alertLabels}
				{onsaved}
			/>
		</Tabs.Content>

		<Tabs.Content value="web">
			<ConfigSection
				config={c}
				path={['web']}
				section="web"
				title="Web"
				description="Session length, login limits and paging apply right away. Locked fields can lock you out, so they change in reovault.toml only."
				{onsaved}
			/>
		</Tabs.Content>

		<Tabs.Content value="system" class="flex flex-col gap-4">
			<ConfigSection
				config={c}
				path={['storage']}
				title="Storage"
				description="Where ReoVault keeps its database, key and vault. Change these in reovault.toml, then restart."
				{onsaved}
			/>
			<ConfigSection
				config={c}
				path={['reolink_cli']}
				title="reolink-cli"
				description="The camera tool and its gateway. Change these in reovault.toml, then restart."
				{onsaved}
			/>
			<p class="text-subtle text-xs">Config file: <span class="font-mono">{c.path ?? 'none'}</span></p>
		</Tabs.Content>
	</Tabs.Root>
{/if}
