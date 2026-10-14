<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, ApiError, type Session } from '$lib/api';
	import { applySession, session } from '$lib/state/session.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import { Input } from '$lib/components/ui/input';
	import { Spinner } from '$lib/components/ui/spinner';
	import ErrorAlert from '$lib/components/app/ErrorAlert.svelte';

	let email = $state('');
	let password = $state('');
	let error = $state<string | null>(null);
	let busy = $state(false);

	// Only same-site paths: never bounce to another origin after login.
	function nextPath(): string {
		const next = page.url.searchParams.get('next') ?? '/';
		return next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/login') ? next : '/';
	}

	$effect(() => {
		if (session.authenticated) goto(nextPath(), { replaceState: true });
	});

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			applySession(await api<Session>('/login', { method: 'POST', body: { email, password } }));
		} catch (err) {
			error = err instanceof ApiError ? err.message : 'Sign-in failed.';
			password = '';
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head><title>Sign in | ReoVault</title></svelte:head>

<main class="grid min-h-dvh place-items-center p-4">
	<div class="w-full max-w-sm">
		<div class="mb-8 flex justify-center">
			<img src="/icons/wordmark-dark.png" alt="ReoVault" class="hidden h-10 w-auto dark:block" />
			<img src="/icons/wordmark-light.png" alt="ReoVault" class="block h-10 w-auto dark:hidden" />
		</div>
		<Card.Root>
			<Card.Header>
				<Card.Title>Sign in</Card.Title>
				<Card.Description>Your recordings stay on this server, encrypted.</Card.Description>
			</Card.Header>
			<Card.Content>
				<form onsubmit={submit} class="flex flex-col gap-5">
					{#if error}<ErrorAlert title="Couldn't sign in" message={error} />{/if}
					<Field.Field>
						<Field.Label for="email">Email</Field.Label>
						<Input id="email" type="email" autocomplete="username" required bind:value={email} />
					</Field.Field>
					<Field.Field>
						<Field.Label for="password">Password</Field.Label>
						<Input
							id="password"
							type="password"
							autocomplete="current-password"
							required
							bind:value={password}
						/>
					</Field.Field>
					<Button type="submit" disabled={busy} class="w-full">
						{#if busy}<Spinner />{/if}
						Sign in
					</Button>
				</form>
			</Card.Content>
		</Card.Root>
	</div>
</main>
