<script lang="ts">
	import type { Run } from '$lib/api';
	import StatusBadge, { type Level } from '$lib/components/app/StatusBadge.svelte';
	import { outcomeLabel } from '$lib/labels';

	let { run }: { run: Run } = $props();

	const levels: Record<string, Level> = { success: 'ok', partial: 'warn', failed: 'bad', aborted: 'bad' };
	// A run still going is neutral: it hasn't succeeded yet.
	const level = $derived<Level>(run.finished_at ? (levels[run.outcome ?? ''] ?? 'idle') : 'idle');
	const text = $derived(run.finished_at ? outcomeLabel(run.outcome) : 'Running');
</script>

<StatusBadge {level}>{text}</StatusBadge>
