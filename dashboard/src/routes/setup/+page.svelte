<script lang="ts">
	import { goto } from '$app/navigation';
	import { api, ApiError, type Session } from '#lib/api/index.js';
	import { applySession, session } from '#lib/state/session.svelte.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Field from '#lib/components/ui/field/index.js';
	import { Input } from '#lib/components/ui/input/index.js';
	import { Spinner } from '#lib/components/ui/spinner/index.js';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';

	let code = $state('');
	let email = $state('');
	let password = $state('');
	let again = $state('');
	let error = $state<string | null>(null);
	let busy = $state(false);

	// Not a fresh install (or already signed in after finishing): leave.
	$effect(() => {
		if (!session.setupNeeded) goto(session.authenticated ? '/' : '/login', { replace: true });
	});

	const mismatch = $derived(again !== '' && again !== password);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (password !== again) return;
		busy = true;
		error = null;
		try {
			const body = { code, password, email: session.setupEmail ? null : email };
			applySession(await api<Session>('/setup', { method: 'POST', body }));
			goto('/', { replace: true });
		} catch (err) {
			error = err instanceof ApiError ? err.message : 'Setup failed.';
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head><title>Set up | ReoVault</title></svelte:head>

<main class="grid min-h-dvh place-items-center p-4">
	<div class="w-full max-w-md">
		<div class="mb-8 flex justify-center">
			<img src="/icons/wordmark-dark.svg" alt="ReoVault" class="hidden h-10 w-auto dark:block" />
			<img src="/icons/wordmark-light.svg" alt="ReoVault" class="block h-10 w-auto dark:hidden" />
		</div>
		<Card.Root>
			<Card.Header>
				<Card.Title>Welcome. Let's set up your login</Card.Title>
				<Card.Description>
					To prove you own this server, enter the setup code ReoVault printed in its log. With
					Docker: <span class="font-mono">docker compose logs reovault | grep setup.code</span>
				</Card.Description>
			</Card.Header>
			<Card.Content>
				<form onsubmit={submit} class="flex flex-col gap-5">
					{#if error}<ErrorAlert title="Couldn't finish setup" message={error} />{/if}
					<Field.Field>
						<Field.Label for="code">Setup code</Field.Label>
						<Input
							id="code"
							class="font-mono tracking-widest uppercase"
							placeholder="XXXX-XXXX"
							autocomplete="off"
							required
							bind:value={code}
						/>
					</Field.Field>
					{#if !session.setupEmail}
						<Field.Field>
							<Field.Label for="email">Email</Field.Label>
							<Input id="email" type="email" autocomplete="username" required bind:value={email} />
							<Field.Description>Only the login name: nothing is ever sent to it.</Field.Description>
						</Field.Field>
					{/if}
					<Field.Field>
						<Field.Label for="password">Password</Field.Label>
						<Input
							id="password"
							type="password"
							autocomplete="new-password"
							minlength={10}
							required
							bind:value={password}
						/>
						<Field.Description>At least 10 characters.</Field.Description>
					</Field.Field>
					<Field.Field>
						<Field.Label for="again">Password again</Field.Label>
						<Input
							id="again"
							type="password"
							autocomplete="new-password"
							required
							bind:value={again}
						/>
						{#if mismatch}<Field.Description class="text-bad">The two passwords differ.</Field.Description>{/if}
					</Field.Field>
					<Button type="submit" disabled={busy || mismatch} class="w-full">
						{#if busy}<Spinner />{/if}
						Create login
					</Button>
				</form>
			</Card.Content>
		</Card.Root>
	</div>
</main>
