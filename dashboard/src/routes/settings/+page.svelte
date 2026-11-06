<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type Config } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Alert from '#lib/components/ui/alert/index.js';
	import * as Tabs from '#lib/components/ui/tabs/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import DestinationsForm from '#lib/components/settings/DestinationsForm.svelte';
	import RecoveryForm from '#lib/components/settings/RecoveryForm.svelte';
	import AlertsForm from '#lib/components/settings/AlertsForm.svelte';
	import ConfigSection from '#lib/components/settings/ConfigSection.svelte';
	import PasswordForm from '#lib/components/settings/PasswordForm.svelte';
	import { toast } from 'svelte-sonner';
	import FileWarning from '@lucide/svelte/icons/file-warning';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import Lock from '@lucide/svelte/icons/lock';

	const tabs = ['destinations', 'backup', 'recovery', 'alerts', 'web', 'system'] as const;
	const tab = $derived(tabs.find((t) => t === page.url.searchParams.get('tab')) ?? 'destinations');

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
	description="Settings from reovault.toml: changes are written to the file, and edits to the file show up here. Cameras have their own page."
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

	<Tabs.Root value={tab} onValueChange={(v) => goto(`?tab=${v}`, { replace: true, reset: false })}>
		<Tabs.List class="mb-4 flex-wrap">
			<Tabs.Trigger value="destinations">Destinations</Tabs.Trigger>
			<Tabs.Trigger value="backup">Schedule</Tabs.Trigger>
			<Tabs.Trigger value="recovery">Recovery</Tabs.Trigger>
			<Tabs.Trigger value="alerts">Alerts</Tabs.Trigger>
			<Tabs.Trigger value="web">Web</Tabs.Trigger>
			<Tabs.Trigger value="system">System</Tabs.Trigger>
		</Tabs.List>

		<Tabs.Content value="destinations"><DestinationsForm writable={c.writable} /></Tabs.Content>
		<Tabs.Content value="backup">
			<ConfigSection
				config={c}
				path={['backup']}
				section="backup"
				title="Schedule"
				description="How often each camera's SD card is read and new clips are backed up. A run that takes longer simply continues."
				labels={{ interval_minutes: 'Back up every', password_file: 'Restic password file' }}
				{onsaved}
			/>
		</Tabs.Content>
		<Tabs.Content value="recovery"><RecoveryForm /></Tabs.Content>

		<Tabs.Content value="alerts" class="flex flex-col gap-4">
			<AlertsForm />
			<ConfigSection
				config={c}
				path={['alerts']}
				title="Alert channels"
				description="Set these in reovault.toml (applied without a restart). The *_file fields point at files holding the secret, e.g. next to reovault.toml; they are read-only here so a stolen session can't redirect a secret."
				labels={alertLabels}
				{onsaved}
			/>
		</Tabs.Content>

		<Tabs.Content value="web" class="flex flex-col gap-4">
			<PasswordForm />
			<ConfigSection
				config={c}
				path={['web']}
				section="web"
				title="Web"
				description="Session length, login limits and the daily update check apply right away. Locked fields can lock you out, so they change in reovault.toml only."
				{onsaved}
			/>
		</Tabs.Content>

		<Tabs.Content value="system" class="flex flex-col gap-4">
			<ConfigSection
				config={c}
				path={['storage']}
				title="Storage"
				description="Where ReoVault keeps its database and the batch waiting for upload. Change these in reovault.toml, then restart."
				{onsaved}
			/>
			<ConfigSection
				config={c}
				path={['tools']}
				title="Tools"
				description="The pinned reolink-cli, restic and rclone this release was verified with. Change these in reovault.toml, then restart."
				{onsaved}
			/>
			<p class="text-subtle text-xs">Config file: <span class="font-mono">{c.path ?? 'none'}</span></p>
		</Tabs.Content>
	</Tabs.Root>
{/if}
