<script lang="ts">
	/** A horizontal stacked bar. SVG attributes, not inline styles: the CSP's
	 * `style-src 'self'` would block a `style="width: ..."`. */
	let {
		segments,
		label
	}: { segments: { value: number; class: string }[]; label: string } = $props();

	const parts = $derived.by(() => {
		let x = 0;
		return segments.map((s) => {
			const w = Math.max(0, Math.min(100 - x, s.value));
			const part = { x, w, class: s.class };
			x += w;
			return part;
		});
	});
</script>

<svg class="bg-muted block h-2 w-full overflow-hidden rounded-full" role="img" aria-label={label}>
	{#each parts as p, i (i)}
		<rect x="{p.x}%" y="0" width="{p.w}%" height="100%" class={p.class} />
	{/each}
</svg>
