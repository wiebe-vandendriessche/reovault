<script lang="ts">
	import { cn } from '$lib/utils';
	import type { Snippet } from 'svelte';

	export type Level = 'ok' | 'warn' | 'bad' | 'idle';

	let { level, children, class: className }: { level: Level; children: Snippet; class?: string } =
		$props();

	const styles: Record<Level, string> = {
		ok: 'bg-ok/12 text-ok border-ok/30',
		warn: 'bg-warn/12 text-warn border-warn/30',
		bad: 'bg-bad/12 text-bad border-bad/30',
		idle: 'bg-idle/12 text-muted-foreground border-idle/30'
	};
</script>

<span
	class={cn(
		'inline-flex h-6 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium whitespace-nowrap',
		styles[level],
		className
	)}
>
	<span class="size-1.5 rounded-full bg-current" aria-hidden="true"></span>
	{@render children()}
</span>
