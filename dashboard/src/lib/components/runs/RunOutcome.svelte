<script lang="ts">
	import type { Run } from '$lib/api';
	import StatusBadge, { type Level } from '$lib/components/app/StatusBadge.svelte';

	let { run }: { run: Run } = $props();

	const levels: Record<string, Level> = { success: 'ok', partial: 'warn', failed: 'bad', aborted: 'bad' };
	const level = $derived<Level>(run.finished_at ? (levels[run.outcome ?? ''] ?? 'idle') : 'ok');
	const text = $derived(run.finished_at ? (run.outcome ?? 'unknown') : 'running');
</script>

<StatusBadge {level}>{text}</StatusBadge>
