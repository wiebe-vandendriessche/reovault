<script lang="ts">
	import type { Coverage } from '#lib/api/index.js';
	import Meter from '#lib/components/app/Meter.svelte';

	/** Where a camera's footage of the chosen span is, one row per place: the
	 * SD card, the primary, each copy. Same time axis for every row, oldest on
	 * the left. Colour is never the only signal: each row also says in words
	 * what needs attention, and hovering a stretch gives its times. */
	let { coverage }: { coverage: Coverage } = $props();

	type Row = Coverage['rows'][number];
	type Run = Row['runs'][number];
	type State = Run['state'];
	// --brand-text, not --brand, for waiting: --brand is under 3:1 on the dark card.
	const FILL: Record<State, string> = {
		safe: 'fill-ok',
		waiting: 'fill-brand-text',
		failing: 'fill-bad',
		lost: 'fill-warn',
		none: 'fill-transparent'
	};
	const WORDS: Record<State, string> = {
		safe: 'backed up',
		waiting: 'waiting',
		failing: 'keep failing',
		lost: 'lost',
		none: ''
	};

	const start = $derived(Date.parse(coverage.start));
	const step = $derived((Date.parse(coverage.end) - start) / coverage.buckets);
	const when = (ms: number) =>
		new Date(ms).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

	function title(run: Run, kind: string): string {
		const span = `${when(start + run.start * step)} to ${when(start + (run.start + run.length) * step)}`;
		if (run.state === 'none') return `${span}: ${kind === 'card' ? 'not on the card' : 'not here'}`;
		return `${span}: ${run.clips} ${run.clips === 1 ? 'clip' : 'clips'} ${WORDS[run.state]}`;
	}

	const TONE = { failing: 'text-bad', lost: 'text-warn', waiting: 'text-brand-text' } as const;

	/** The row in a few words, each part in its own colour: what needs
	 * attention first, else that everything is held. */
	function summary(row: Row): { text: string; tone: string }[] {
		const total = (s: State) => row.runs.filter((r) => r.state === s).reduce((n, r) => n + r.clips, 0);
		const parts = (['failing', 'lost', 'waiting'] as const)
			.filter((s) => total(s))
			.map((s) => ({ text: `${total(s)} ${s === 'failing' ? 'failing' : WORDS[s]}`, tone: TONE[s] }));
		if (parts.length) return parts;
		return [{ text: total('safe') ? 'all backed up' : 'nothing yet', tone: 'text-subtle' }];
	}

	// The left end of the axis, in words: "12 days ago", or a date further back.
	const ago = $derived.by(() => {
		const days = (Date.parse(coverage.end) - start) / 86400000;
		if (days < 1.5) return `${Math.max(1, Math.round(days * 24))} hours ago`;
		if (days < 60) return `${Math.round(days)} days ago`;
		return new Date(start).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
	});
</script>

<div class="grid grid-cols-[minmax(0,5.5rem)_1fr_auto] items-center gap-x-3 gap-y-2">
	{#each coverage.rows as row (row.kind + row.name)}
		{@const s = summary(row)}
		<span class="text-subtle truncate text-xs" title={row.name}>{row.name}</span>
		<Meter
			class="h-2.5"
			label="{row.name}, since {ago}: {s.map((p) => p.text).join(', ')}"
			marker={row.marker}
			segments={row.runs.map((r) => ({
				value: (r.length / coverage.buckets) * 100,
				class: FILL[r.state],
				title: title(r, row.kind)
			}))}
		/>
		<span class="max-w-36 truncate text-right text-xs tabular-nums" aria-hidden="true">
			{#each s as part, i (part.text)}{#if i}<span class="text-subtle">,</span>{' '}{/if}<span class={part.tone}>{part.text}</span>{/each}
		</span>
	{/each}
	<span></span>
	<div class="text-subtle flex justify-between text-xs">
		<span>{ago}</span>
		<span>Now</span>
	</div>
	<span></span>
</div>
