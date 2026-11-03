<script lang="ts">
	/** Change the login password and/or email. Asks for the current password
	 * even though you're signed in: a stolen session mustn't be able to take
	 * the account over. Every other browser is signed out. */
	import { api, ApiError, type Session } from '#lib/api/index.js';
	import { applySession } from '#lib/state/session.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { Input } from '#lib/components/ui/input/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import { toast } from 'svelte-sonner';

	let current = $state('');
	let password = $state('');
	let again = $state('');
	let email = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	const mismatch = $derived(again !== '' && again !== password);
	const dirty = $derived(password !== '' || email.trim() !== '');

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (mismatch || !dirty) return;
		busy = true;
		error = null;
		try {
			const body = { current, password: password || null, email: email.trim() || null };
			applySession(await api<Session>('/password', { method: 'POST', body }));
			current = password = again = email = '';
			toast.success('Saved. Every other browser was signed out.');
		} catch (err) {
			error = err instanceof ApiError ? err.message : 'Could not save.';
		} finally {
			busy = false;
		}
	}
</script>

<form onsubmit={save}>
	<Card.Root>
		<Card.Header>
			<Card.Title>Login</Card.Title>
			<Card.Description>
				Change the dashboard password, or the email you sign in with. Needs your current password.
			</Card.Description>
		</Card.Header>
		<Card.Content class="flex flex-col gap-4">
			<Field.Field>
				<Field.Label for="pw-current">Current password</Field.Label>
				<Input id="pw-current" type="password" autocomplete="current-password" required bind:value={current} />
			</Field.Field>
			<div class="grid gap-4 sm:grid-cols-2">
				<Field.Field>
					<Field.Label for="pw-new">New password</Field.Label>
					<Input id="pw-new" type="password" autocomplete="new-password" minlength={10} bind:value={password} />
					<Field.Description>At least 10 characters. Leave empty to keep it.</Field.Description>
				</Field.Field>
				<Field.Field>
					<Field.Label for="pw-again">New password again</Field.Label>
					<Input id="pw-again" type="password" autocomplete="new-password" bind:value={again} />
					{#if mismatch}<Field.Description class="text-bad">The two passwords differ.</Field.Description>{/if}
				</Field.Field>
			</div>
			<Field.Field>
				<Field.Label for="pw-email">New email</Field.Label>
				<Input id="pw-email" type="email" autocomplete="username" bind:value={email} />
				<Field.Description>Only the login name. Leave empty to keep it.</Field.Description>
			</Field.Field>
			{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
		</Card.Content>
		<Card.Footer class="border-t">
			<Button type="submit" disabled={busy || mismatch || !dirty}>
				{#if busy}<Spinner />{/if} Save
			</Button>
		</Card.Footer>
	</Card.Root>
</form>
