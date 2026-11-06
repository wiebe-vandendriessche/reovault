<script lang="ts">
	/** The safety net: the restic password is the one thing that can't be lost,
	 * so say where it lives, offer a recovery sheet (without it) and show that
	 * ReoVault's own config is backed up next to the footage. */
	import { api, apiUrl, type Recovery } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import StatusBadge from '#lib/components/app/StatusBadge.svelte';
	import { relative } from '#lib/format.js';
	import { toast } from 'svelte-sonner';
	import Download from '@lucide/svelte/icons/download';

	const rec = query(() => {
		void live.settings;
		return api<Recovery>('/recovery');
	});

	async function confirm() {
		try {
			rec.data = await api<Recovery>('/recovery/confirm', { method: 'POST' });
			toast.success('Noted.');
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		}
	}
</script>

{#if rec.data}
	{@const r = rec.data}
	<div class="flex flex-col gap-4">
		<Card.Root>
			<Card.Header>
				<Card.Title>Restic password</Card.Title>
				<Card.Description>
					Every backup is encrypted with it. If this server is lost and you don't have the password somewhere else, the
					backups can't be read, by anyone. ReoVault never shows it.
				</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-3 text-sm">
				<p>
					It lives in <span class="font-mono">{r.password_file ?? 'no file configured'}</span>
					{#if r.password_file && !r.password_file_found}<StatusBadge level="bad">file not found</StatusBadge>{/if}
				</p>
				{#if r.confirmed_at}
					<p class="text-muted-foreground">You said you stored a copy {relative(r.confirmed_at)}.</p>
				{:else}
					<Button class="self-start" variant="outline" onclick={confirm}>I stored a copy somewhere safe</Button>
				{/if}
			</Card.Content>
		</Card.Root>

		<Card.Root>
			<Card.Header>
				<Card.Title>Recovery sheet</Card.Title>
				<Card.Description>
					Where everything lives and the commands to get a clip back with plain restic. It contains no passwords.
				</Card.Description>
				<Card.Action>
					<Button variant="outline" href={apiUrl('/recovery/sheet')}><Download /> Download</Button>
				</Card.Action>
			</Card.Header>
		</Card.Root>

		<Card.Root>
			<Card.Header>
				<Card.Title>Config backup</Card.Title>
				<Card.Description>
					Your reovault.toml (cameras and their passwords included) and rclone remotes are backed up into each destination whenever they change, so a new server
					can pick up where this one stopped.
				</Card.Description>
			</Card.Header>
			<Card.Content class="flex flex-col gap-2 text-sm">
				{#each r.destinations as d (d.name)}
					<div class="flex flex-wrap items-center gap-2">
						<span class="font-medium">{d.name}</span>
						{#if d.config_error}
							<StatusBadge level="bad">Failed</StatusBadge>
							<span class="text-muted-foreground text-xs break-all">{d.config_error}</span>
						{:else if d.config_backed_up_at}
							<StatusBadge level="ok">Backed up {relative(d.config_backed_up_at)}</StatusBadge>
						{:else}
							<StatusBadge level="idle">Not yet</StatusBadge>
						{/if}
					</div>
				{/each}
			</Card.Content>
		</Card.Root>
	</div>
{/if}
