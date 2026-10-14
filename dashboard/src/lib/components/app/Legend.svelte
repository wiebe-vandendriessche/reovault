<script lang="ts">
	import { cn } from '$lib/utils';

	/** Legend for non-chart bars (`Meter`): the same rounded-square swatch as
	 * the shadcn chart tooltip and layerchart legends. `swatch` overrides the
	 * swatch's shape, e.g. a thin line for a limit marker. */
	let {
		items,
		class: className
	}: { items: { label: string; class: string; swatch?: string }[]; class?: string } = $props();
</script>

<ul class={cn('text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1 text-xs', className)}>
	{#each items as item (item.label)}
		<li class="flex items-center gap-1.5">
			<span class={cn('size-2.5 shrink-0 rounded-[2px]', item.swatch, item.class)} aria-hidden="true"></span>
			{item.label}
		</li>
	{/each}
</ul>
