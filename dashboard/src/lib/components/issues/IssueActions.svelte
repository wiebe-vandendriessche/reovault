<script lang="ts">
	import { api, type Overview } from '#lib/api/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import GiveUpButton from './GiveUpButton.svelte';
	import { toast } from 'svelte-sonner';

	type Issue = Overview['issues'][number];
	/** The way out of a problem, next to it: a button for what ReoVault can do
	 * now, a link to the settings that answer it. */
	let { issue }: { issue: Issue } = $props();
	let busy = $state(false);

	const cam = $derived(issue.camera ? `/cameras/${encodeURIComponent(issue.camera)}` : null);

	async function post(path: string, done: string) {
		busy = true;
		try {
			await api(path, { method: 'POST', body: {} });
			toast.success(done);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			busy = false;
		}
	}
</script>

{#if issue.actions.length}
	<div class="mt-2 flex flex-wrap gap-2">
		{#each issue.actions as action (action)}
			{#if action === 'run' && cam}
				<Button size="sm" variant="outline" disabled={busy} onclick={() => post(`${cam}/run`, 'Backup started.')}>
					Back up now
				</Button>
			{:else if action === 'check' && cam}
				<Button
					size="sm"
					variant="outline"
					disabled={busy}
					onclick={() => post(`${cam}/check`, 'The check runs after this backup; it can take an hour.')}
				>
					Check again
				</Button>
			{:else if action === 'retry_clips' && cam}
				<Button
					size="sm"
					variant="outline"
					disabled={busy}
					onclick={() => post(`${cam}/clips/retry`, 'The failing clips are tried again now.')}
				>
					Retry clips
				</Button>
			{:else if action === 'give_up_clips' && issue.camera}
				<GiveUpButton camera={issue.camera} label="Give up on them" />
			{:else if action === 'destinations'}
				<Button size="sm" variant="ghost" href="/settings?tab=destinations">Open destinations</Button>
			{:else if action === 'recovery'}
				<Button size="sm" variant="ghost" href="/settings?tab=recovery">Open recovery</Button>
			{:else if action === 'alerts'}
				<Button size="sm" variant="ghost" href="/settings?tab=alerts">Open alert settings</Button>
			{/if}
		{/each}
		{#if issue.actions.includes('retry_clips') && issue.camera}
			<Button size="sm" variant="ghost" href="/activity?camera={encodeURIComponent(issue.camera)}">See the clips</Button>
		{/if}
	</div>
{/if}
