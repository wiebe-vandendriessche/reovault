<script lang="ts">
	import { api, type AlertRules, type Alerts, type TestResult } from '$lib/api';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import * as InputGroup from '$lib/components/ui/input-group';
	import { Button } from '$lib/components/ui/button';
	import { Switch } from '$lib/components/ui/switch';
	import { Spinner } from '$lib/components/ui/spinner';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import StatusBadge from '$lib/components/app/StatusBadge.svelte';
	import { relative } from '$lib/format';
	import { toast } from 'svelte-sonner';
	import Send from '@lucide/svelte/icons/send';

	const loaded = query(() => {
		void live.alert;
		void live.settings;
		return api<Alerts>('/alerts');
	});
	let rules = $state<AlertRules | null>(null);
	let lastSaved = '';
	$effect(() => {
		// Reset the form only when the saved rules changed (a save, or an edit
		// of reovault.toml), not on every alert event, so typing isn't lost.
		const saved = JSON.stringify(loaded.data?.rules ?? null);
		if (loaded.data && saved !== lastSaved) {
			lastSaved = saved;
			rules = { ...loaded.data.rules };
		}
	});
	let busy = $state<'save' | 'test' | null>(null);
	let error = $state<string | null>(null);
	const pinned = $derived(loaded.data?.env_pinned ?? false);
	const anyChannel = $derived(loaded.data?.channels.some((c) => c.configured) ?? false);

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!rules) return;
		busy = 'save';
		error = null;
		try {
			loaded.data = await api<Alerts>('/alerts/rules', { method: 'PUT', body: rules });
			rules = { ...loaded.data.rules };
			toast.success('Alert rules saved.');
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = null;
		}
	}

	async function test() {
		busy = 'test';
		try {
			const results = await api<TestResult[]>('/alerts/test', { method: 'POST' });
			for (const r of results)
				if (r.ok) toast.success(`Test alert sent via ${r.channel}.`);
				else toast.error(`${r.channel}: ${r.error}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : String(err));
		} finally {
			busy = null;
		}
	}

	const toggles: { key: keyof AlertRules; label: string; desc: string }[] = [
		{ key: 'run_failed', label: 'An archive run fails', desc: 'Or is stopped before it finishes.' },
		{ key: 'problems', label: 'Recordings need attention', desc: 'Failed or quarantined clips.' },
		{ key: 'coverage', label: 'Falling behind the SD card', desc: 'Unarchived clips are close to being overwritten.' },
		{ key: 'gateway_down', label: 'The camera gateway is down', desc: 'After the grace period below.' }
	];
</script>

{#if loaded.error}
	<ErrorAlert message={loaded.error} />
{:else if !loaded.data || !rules}
	<Skeleton class="h-96 w-full rounded-xl" />
{:else}
	<div class="flex flex-col gap-4">
		<Card.Root>
			<Card.Header>
				<Card.Title>Channels</Card.Title>
				<Card.Description>
					Which channels are set up. Tokens and webhook URLs stay in files you point to below.
				</Card.Description>
				<Card.Action>
					<Button variant="outline" size="sm" onclick={test} disabled={!anyChannel || busy === 'test'}>
						{#if busy === 'test'}<Spinner />{:else}<Send />{/if} Send test
					</Button>
				</Card.Action>
			</Card.Header>
			<Card.Content>
				<div class="flex flex-wrap gap-2">
					{#each loaded.data.channels as c (c.name)}
						<StatusBadge level={c.configured ? 'ok' : 'idle'} class="w-fit">
							{c.name}{c.configured ? '' : ': not set'}
						</StatusBadge>
					{/each}
				</div>
			</Card.Content>
		</Card.Root>

		{#if loaded.data.open.length}
			<Card.Root>
				<Card.Header><Card.Title>Open now</Card.Title></Card.Header>
				<Card.Content class="flex flex-col gap-3">
					{#each loaded.data.open as a (a.condition + a.device_id)}
						<div class="flex flex-col gap-0.5">
							<span class="text-sm font-medium">{a.title}</span>
							<span class="text-muted-foreground text-sm">{a.message}</span>
							<span class="text-subtle flex flex-wrap gap-x-3 text-xs">
								<span>Since {relative(a.first_seen_at)}</span>
								<span>{a.last_sent_at ? `Notified ${relative(a.last_sent_at)}` : 'Not sent yet'}</span>
							</span>
						</div>
					{/each}
				</Card.Content>
			</Card.Root>
		{/if}

		<form onsubmit={save}>
			<Card.Root>
				<Card.Header>
					<Card.Title>Rules</Card.Title>
					<Card.Description>
						{#if pinned}<span class="text-warn">Set by environment variables, so read-only here.</span>
						{:else}When to send an alert, for every camera.{/if}
					</Card.Description>
				</Card.Header>
				<Card.Content>
					<fieldset disabled={pinned} class="flex flex-col gap-5">
						<Field.Field orientation="horizontal">
							<Switch id="al-on" bind:checked={rules.enabled} />
							<Field.Content>
								<Field.Label for="al-on">Alerts on</Field.Label>
								<Field.Description>Turning this off clears open alerts without a "resolved" message.</Field.Description>
							</Field.Content>
						</Field.Field>
						<fieldset disabled={!rules.enabled} class="flex flex-col gap-5 pl-12 disabled:opacity-60">
							{#each toggles as t (t.key)}
								<Field.Field orientation="horizontal">
									<Switch id="al-{t.key}" bind:checked={rules[t.key] as boolean} />
									<Field.Content>
										<Field.Label for="al-{t.key}">{t.label}</Field.Label>
										<Field.Description>{t.desc}</Field.Description>
									</Field.Content>
								</Field.Field>
							{/each}
							<div class="grid gap-4 sm:grid-cols-2">
								<Field.Field>
									<Field.Label for="al-grace">Gateway grace period</Field.Label>
									<InputGroup.Root>
										<InputGroup.Input id="al-grace" type="number" min="1" max="1440" bind:value={rules.gateway_down_minutes} />
										<InputGroup.Addon align="inline-end">minutes</InputGroup.Addon>
									</InputGroup.Root>
								</Field.Field>
								<Field.Field>
									<Field.Label for="al-renotify">Remind every</Field.Label>
									<InputGroup.Root>
										<InputGroup.Input id="al-renotify" type="number" min="1" max="720" bind:value={rules.renotify_hours} />
										<InputGroup.Addon align="inline-end">hours</InputGroup.Addon>
									</InputGroup.Root>
								</Field.Field>
							</div>
							<Field.Field orientation="horizontal">
								<Switch id="al-resolve" bind:checked={rules.notify_on_resolve} />
								<Field.Content>
									<Field.Label for="al-resolve">Say when it's fixed</Field.Label>
								</Field.Content>
							</Field.Field>
						</fieldset>
					</fieldset>
				</Card.Content>
				<Card.Footer class="flex items-center gap-3 border-t">
					<Button type="submit" disabled={!!busy || pinned}>{#if busy === 'save'}<Spinner />{/if} Save rules</Button>
					{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
				</Card.Footer>
			</Card.Root>
		</form>
	</div>
{/if}
