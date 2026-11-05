<script lang="ts">
	import { cn } from '#lib/utils.js';

	/** A horizontal stacked bar. The track is "free" space; segments are
	 * clipped to its rounded ends so they read as one bar. `marker` (percent)
	 * draws a limit line on an unclipped overlay; a segment's `title` is its hover
	 * text (a native SVG <title>). SVG attributes, not inline
	 * styles: the CSP's `style-src 'self'` would block a `style="width: ..."`. */
	let {
		segments,
		label,
		marker = null,
		class: className
	}: {
		segments: { value: number; class: string; title?: string }[];
		label: string;
		marker?: number | null;
		class?: string;
	} = $props();

	const parts = $derived.by(() => {
		let x = 0;
		return segments.map((s) => {
			const w = Math.max(0, Math.min(100 - x, s.value));
			const part = { x, w, class: s.class, title: s.title };
			x += w;
			return part;
		});
	});
</script>

<div class={cn('relative h-2 w-full', className)} role="img" aria-label={label}>
	<svg class="bg-muted-foreground/30 block size-full overflow-hidden rounded-full" aria-hidden="true">
		{#each parts as p, i (i)}
			<rect x="{p.x}%" y="0" width="{p.w}%" height="100%" class={p.class}>
				{#if p.title}<title>{p.title}</title>{/if}
			</rect>
		{/each}
	</svg>
	{#if marker != null}
		<svg class="pointer-events-none absolute inset-0 size-full overflow-visible" aria-hidden="true">
			<line x1="{marker}%" x2="{marker}%" y1="-25%" y2="125%" class="stroke-foreground" stroke-width="2" />
		</svg>
	{/if}
</div>
