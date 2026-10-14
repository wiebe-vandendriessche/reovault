<script lang="ts">
	import { api, type AddDevice, type Devices, type Discovered } from '$lib/api';
	import * as Dialog from '$lib/components/ui/dialog';
	import * as Field from '$lib/components/ui/field';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Spinner } from '$lib/components/ui/spinner';
	import { toast } from 'svelte-sonner';
	import Plus from '@lucide/svelte/icons/plus';
	import Radar from '@lucide/svelte/icons/radar';

	let { onadded }: { onadded: (d: Devices) => void } = $props();

	let open = $state(false);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let found = $state<Discovered[] | null>(null);
	let scanning = $state(false);
	const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
	const blank = () => ({ alias: '', host: '', user: 'admin', password: '', timezone: browserTz, name: '' });
	let form = $state(blank());

	$effect(() => {
		if (open) {
			form = blank();
			error = null;
			found = null;
		}
	});

	async function scan() {
		scanning = true;
		error = null;
		try {
			found = await api<Discovered[]>('/devices/discover', { method: 'POST' });
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			scanning = false;
		}
	}

	function use(d: Discovered) {
		form.host = d.host;
		form.name = d.name ?? '';
		if (!form.alias) form.alias = (d.name ?? d.model ?? 'camera').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			const body: AddDevice = { ...form, name: form.name || null };
			onadded(await api<Devices>('/devices', { method: 'POST', body }));
			toast.success(`${form.name || form.alias} added.`);
			open = false;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
			form.password = '';
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Trigger>
		{#snippet child({ props })}<Button {...props}><Plus /> Add camera</Button>{/snippet}
	</Dialog.Trigger>
	<Dialog.Content class="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
		<Dialog.Header>
			<Dialog.Title>Add a camera</Dialog.Title>
			<Dialog.Description>
				The password goes straight to reolink-cli's encrypted credential store; ReoVault never
				keeps it.
			</Dialog.Description>
		</Dialog.Header>

		<div class="flex flex-col gap-2">
			<Button variant="outline" onclick={scan} disabled={scanning} class="self-start">
				{#if scanning}<Spinner />{:else}<Radar />{/if} Find cameras on my network
			</Button>
			{#if found?.length === 0}
				<p class="text-muted-foreground text-sm">No cameras answered. Enter the address by hand.</p>
			{/if}
			{#each found ?? [] as d (d.host)}
				<button
					type="button"
					onclick={() => use(d)}
					disabled={d.already_registered}
					class="hover:bg-muted flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-sm disabled:opacity-50"
				>
					<span class="flex flex-wrap gap-x-3">
						<span class="font-medium">{d.name ?? d.model ?? 'Camera'}</span>
						<span class="text-muted-foreground">{d.host}</span>
					</span>
					<span class="text-subtle text-xs">{d.already_registered ? 'Already added' : 'Use'}</span>
				</button>
			{/each}
		</div>

		<form class="flex flex-col gap-4" onsubmit={submit}>
			<div class="grid gap-4 sm:grid-cols-2">
				<Field.Field>
					<Field.Label for="cam-alias">Alias</Field.Label>
					<Input id="cam-alias" required maxlength={100} bind:value={form.alias} placeholder="front-door" />
					<Field.Description>Short id used by reolink-cli.</Field.Description>
				</Field.Field>
				<Field.Field>
					<Field.Label for="cam-name">Display name</Field.Label>
					<Input id="cam-name" maxlength={100} bind:value={form.name} placeholder="Front door" />
				</Field.Field>
				<Field.Field>
					<Field.Label for="cam-host">Address</Field.Label>
					<Input id="cam-host" required bind:value={form.host} placeholder="192.168.1.40" />
				</Field.Field>
				<Field.Field>
					<Field.Label for="cam-tz">Timezone</Field.Label>
					<Input id="cam-tz" required bind:value={form.timezone} />
				</Field.Field>
				<Field.Field>
					<Field.Label for="cam-user">Username</Field.Label>
					<Input id="cam-user" required autocomplete="off" bind:value={form.user} />
				</Field.Field>
				<Field.Field>
					<Field.Label for="cam-pass">Password</Field.Label>
					<Input id="cam-pass" type="password" required autocomplete="new-password" bind:value={form.password} />
				</Field.Field>
			</div>
			{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			<Dialog.Footer>
				<Button type="submit" disabled={busy}>{#if busy}<Spinner />{/if} Add camera</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>
