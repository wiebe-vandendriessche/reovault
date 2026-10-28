<script lang="ts">
	import type { Camera } from '#lib/api/index.js';
	import * as NativeSelect from '#lib/components/ui/native-select/index.js';

	/** Which camera a page shows. `all` adds an "All cameras" choice (value ''). */
	let {
		cameras,
		value,
		onchange,
		all = false
	}: { cameras: Camera[]; value: string; onchange: (alias: string) => void; all?: boolean } = $props();
</script>

<NativeSelect.Root
	{value}
	onchange={(e: Event) => onchange((e.currentTarget as HTMLSelectElement).value)}
	aria-label="Camera"
	class="min-w-44"
>
	{#if all}<NativeSelect.Option value="">All cameras</NativeSelect.Option>{/if}
	{#each cameras as c (c.alias)}
		<NativeSelect.Option value={c.alias}>{c.name || c.alias}</NativeSelect.Option>
	{/each}
</NativeSelect.Root>
