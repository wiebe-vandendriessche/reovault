<script lang="ts">
	import { api, type Retention, type RetentionSave } from '$lib/api';
	import { query } from '$lib/state/query.svelte';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import * as InputGroup from '$lib/components/ui/input-group';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import { Button } from '$lib/components/ui/button';
	import { Spinner } from '$lib/components/ui/spinner';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';
	import { bytes, plural } from '$lib/format';
	import { toast } from 'svelte-sonner';

	const loaded = query(() => api<Retention>('/retention'));
	let age = $state<number | null>(null);
	let cap = $state<number | null>(null);
	$effect(() => {
		if (loaded.data) {
			age = loaded.data.max_age_days ?? null;
			cap = loaded.data.max_vault_gb ?? null;
		}
	});
	let busy = $state(false);
	let error = $state<string | null>(null);
	let confirm = $state<RetentionSave['confirm']>(null);
	const pinned = $derived(loaded.data?.env_pinned ?? false);

	async function save(token?: string) {
		busy = true;
		error = null;
		try {
			const r = await api<RetentionSave>('/retention', {
				method: 'PUT',
				body: { max_age_days: age || null, max_vault_gb: cap || null, confirm: token ?? null }
			});
			loaded.data = r.retention;
			confirm = r.confirm ?? null;
			if (r.saved) toast.success('Retention saved.');
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

{#if loaded.error}
	<ErrorAlert message={loaded.error} />
{:else if !loaded.data}
	<Skeleton class="h-64 w-full rounded-xl" />
{:else}
	<form onsubmit={(e) => (e.preventDefault(), save())}>
		<Card.Root>
			<Card.Header>
				<Card.Title>Retention</Card.Title>
				<Card.Description>
					Applies to the whole vault, every camera. The oldest clips are deleted first; a deleted clip
					is never downloaded again. Leave a field empty for no limit.
					{#if pinned}<span class="text-warn">Set by environment variables, so read-only here.</span>{/if}
				</Card.Description>
			</Card.Header>
			<Card.Content>
				<fieldset disabled={pinned} class="grid gap-4 sm:grid-cols-2">
					<Field.Field>
						<Field.Label for="ret-age">Keep clips for at most</Field.Label>
						<InputGroup.Root>
							<InputGroup.Input id="ret-age" type="number" min="1" placeholder="No limit" bind:value={age} />
							<InputGroup.Addon align="inline-end">days</InputGroup.Addon>
						</InputGroup.Root>
					</Field.Field>
					<Field.Field>
						<Field.Label for="ret-cap">Cap the vault at</Field.Label>
						<InputGroup.Root>
							<InputGroup.Input id="ret-cap" type="number" min="1" step="any" placeholder="No limit" bind:value={cap} />
							<InputGroup.Addon align="inline-end">GB</InputGroup.Addon>
						</InputGroup.Root>
						<Field.Description>The vault holds {bytes(loaded.data.vault_used_bytes)} now.</Field.Description>
					</Field.Field>
				</fieldset>
			</Card.Content>
			<Card.Footer class="flex items-center gap-3 border-t">
				<Button type="submit" disabled={busy || pinned}>{#if busy}<Spinner />{/if} Save retention</Button>
				{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			</Card.Footer>
		</Card.Root>
	</form>
{/if}

<AlertDialog.Root open={confirm !== null} onOpenChange={(o) => !o && (confirm = null)}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Delete {confirm ? plural(confirm.count, 'archived clip') : ''}?</AlertDialog.Title>
			<AlertDialog.Description>
				This policy removes {confirm ? bytes(confirm.bytes) : ''} from the vault right away, oldest
				first. Deleted clips cannot be recovered.
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="bg-destructive text-white hover:bg-destructive/90"
				onclick={() => confirm && save(confirm.token)}
			>
				Delete and save
			</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
