<script lang="ts">
	/** Any reovault.toml table, rendered from the server's field list: an
	 * input per field by type, a lock where the value is set by an env var or
	 * is infrastructure (change it in the file, then restart). Saving writes
	 * only the changed fields back into the file. */
	import { api, ApiError, type Config, type ConfigField } from '$lib/api';
	import * as Card from '$lib/components/ui/card';
	import * as Field from '$lib/components/ui/field';
	import * as InputGroup from '$lib/components/ui/input-group';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Switch } from '$lib/components/ui/switch';
	import { Spinner } from '$lib/components/ui/spinner';
	import { toast } from 'svelte-sonner';
	import Lock from '@lucide/svelte/icons/lock';

	let {
		config,
		path,
		title,
		description,
		section,
		labels = {},
		onsaved
	}: {
		config: Config;
		path: string[];
		title: string;
		description?: string;
		/** PUT /config/{section}; omit for a read-only section. */
		section?: string;
		labels?: Record<string, string>;
		onsaved: (c: Config) => void;
	} = $props();

	const fields = $derived(
		config.sections.find((s) => s.path.join('.') === path.join('.'))?.fields ?? []
	);
	const editable = $derived(!!section && fields.some((f) => f.editable));

	// Values per field (number inputs bind numbers); reset whenever the
	// server's view changes, e.g. after a save or a hand edit of the file.
	let draft = $state<Record<string, unknown>>({});
	$effect(() => {
		draft = Object.fromEntries(fields.map((f) => [f.key, toText(f.value)]));
	});
	let busy = $state(false);
	let error = $state<string | null>(null);

	function toText(v: unknown): string {
		if (v === null || v === undefined) return '';
		if (Array.isArray(v)) return v.join(', ');
		return String(v);
	}

	function parse(f: ConfigField, text: string): unknown {
		if (f.kind === 'bool') return text === 'true';
		if (f.kind === 'list') return text.split(',').map((s) => s.trim()).filter(Boolean);
		if (text.trim() === '') return null;
		if (f.kind === 'int' || f.kind === 'float') return Number(text);
		return text;
	}

	const UNITS: [RegExp, string][] = [
		[/_secs$/, 'seconds'],
		[/_minutes$/, 'minutes'],
		[/_hours$/, 'hours'],
		[/_days$/, 'days'],
		[/_gb$/, 'GB'],
		[/_bytes$/, 'bytes'],
		[/_pct$/, '%']
	];
	const unit = (key: string) => UNITS.find(([re]) => re.test(key))?.[1];
	function label(key: string): string {
		if (labels[key]) return labels[key];
		const words = key.replace(/_(secs|minutes|hours|days|gb|bytes|pct)$/, '').replaceAll('_', ' ');
		return words.charAt(0).toUpperCase() + words.slice(1);
	}
	function lockReason(f: ConfigField): string | null {
		if (f.env_var) return `Set by the ${f.env_var} environment variable.`;
		if (f.restart) return 'Change this in reovault.toml, then restart ReoVault.';
		if (!config.writable) return 'reovault.toml is read-only.';
		return null;
	}

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!section) return;
		const values: Record<string, unknown> = {};
		for (const f of fields) {
			if (!f.editable) continue;
			const text = toText(draft[f.key]);
			if (text !== toText(f.value)) values[f.key] = parse(f, text);
		}
		if (!Object.keys(values).length) return;
		busy = true;
		error = null;
		try {
			onsaved(
				await api<Config>(`/config/${section}`, {
					method: 'PUT',
					body: { version: config.version, values }
				})
			);
			toast.success(`${title} saved to reovault.toml.`);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			if (err instanceof ApiError && err.status === 409) toast.error(error);
		} finally {
			busy = false;
		}
	}
</script>

<form onsubmit={save}>
	<Card.Root>
		<Card.Header>
			<Card.Title>{title}</Card.Title>
			{#if description}<Card.Description>{description}</Card.Description>{/if}
		</Card.Header>
		<Card.Content class="grid gap-x-6 gap-y-5 sm:grid-cols-2">
			{#each fields as f (f.key)}
				{@const reason = lockReason(f)}
				{@const locked = !f.editable}
				<Field.Field orientation={f.kind === 'bool' ? 'horizontal' : 'vertical'}>
					{#if f.kind === 'bool'}
						<Switch
							id="cfg-{path.join('-')}-{f.key}"
							disabled={locked}
							bind:checked={
								() => draft[f.key] === 'true', (on) => (draft[f.key] = on ? 'true' : 'false')
							}
						/>
					{/if}
					<Field.Content>
						<Field.Label for="cfg-{path.join('-')}-{f.key}" class="flex items-center gap-1.5">
							{label(f.key)}
							{#if locked && reason}
								<Tooltip.Root>
									<Tooltip.Trigger>
										{#snippet child({ props })}
											<span {...props} class="text-subtle" aria-label={reason}>
												<Lock class="size-3.5" />
											</span>
										{/snippet}
									</Tooltip.Trigger>
									<Tooltip.Content>{reason}</Tooltip.Content>
								</Tooltip.Root>
							{/if}
						</Field.Label>
						{#if f.kind !== 'bool'}
							{@const u = unit(f.key)}
							{#if u}
								<InputGroup.Root>
									<InputGroup.Input
										id="cfg-{path.join('-')}-{f.key}"
										type="number"
										step={f.kind === 'float' ? 'any' : '1'}
										disabled={locked}
										placeholder={toText(f.default) || 'Not set'}
										bind:value={draft[f.key]}
									/>
									<InputGroup.Addon align="inline-end">{u}</InputGroup.Addon>
								</InputGroup.Root>
							{:else}
								<Input
									id="cfg-{path.join('-')}-{f.key}"
									type={f.kind === 'int' || f.kind === 'float' ? 'number' : 'text'}
									disabled={locked}
									placeholder={toText(f.default) || 'Not set'}
									class={f.kind === 'path' ? 'font-mono text-xs' : ''}
									bind:value={draft[f.key]}
								/>
							{/if}
						{/if}
						<Field.Description>
							{f.source === 'default'
								? 'Default'
								: f.source === 'env'
									? 'From the environment'
									: 'From reovault.toml'}{f.kind === 'list' ? ', comma separated' : ''}
						</Field.Description>
					</Field.Content>
				</Field.Field>
			{/each}
		</Card.Content>
		{#if editable}
			<Card.Footer class="flex items-center gap-3 border-t">
				<Button type="submit" disabled={busy}>{#if busy}<Spinner />{/if} Save {title.toLowerCase()}</Button>
				{#if error}<p class="text-bad text-sm" role="alert">{error}</p>{/if}
			</Card.Footer>
		{/if}
	</Card.Root>
</form>
