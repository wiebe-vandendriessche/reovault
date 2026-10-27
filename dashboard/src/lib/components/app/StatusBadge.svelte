<script lang="ts">
	import { Badge } from '#lib/components/ui/badge/index.js';
	import { cn } from '#lib/utils.js';
	import type { Snippet } from 'svelte';

	export type Level = 'ok' | 'warn' | 'bad' | 'idle';

	let {
		level,
		children,
		href,
		class: className
	}: { level: Level; children: Snippet; href?: string; class?: string } = $props();

	const styles: Record<Level, string> = {
		ok: 'bg-ok/12 text-ok border-ok/30',
		warn: 'bg-warn/12 text-warn border-warn/30',
		bad: 'bg-bad/12 text-bad border-bad/30 [a]:hover:bg-bad/20',
		idle: 'bg-idle/12 text-muted-foreground border-idle/30'
	};
</script>

<Badge variant="outline" {href} class={cn('h-6 gap-1.5 px-2.5', styles[level], className)}>
	<span class="size-1.5 rounded-full bg-current" aria-hidden="true"></span>
	{@render children()}
</Badge>
