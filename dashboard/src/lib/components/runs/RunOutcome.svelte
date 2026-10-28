<script lang="ts">
	import type { Run } from '#lib/api/index.js';
	import StatusBadge, { type Level } from '#lib/components/app/StatusBadge.svelte';
	import { outcomeLabel } from '#lib/labels.js';

	let { run }: { run: Run } = $props();

	const levels: Record<string, Level> = { ok: 'ok', error: 'bad', stopped: 'idle' };
	// A run still going is neutral: it hasn't succeeded yet.
	const level = $derived<Level>(run.finished_at ? (levels[run.outcome ?? ''] ?? 'idle') : 'idle');
	const text = $derived(run.finished_at ? outcomeLabel(run.outcome) : 'Running');
</script>

<StatusBadge {level}>{text}</StatusBadge>
