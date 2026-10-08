<script lang="ts">
	import { api, type Calendar } from '$lib/api';
	import { live } from '$lib/state/live.svelte';
	import { query } from '$lib/state/query.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Skeleton } from '$lib/components/ui/skeleton';
	import { cn } from '$lib/utils';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { plural } from '$lib/format';
	import ChevronLeft from '@lucide/svelte/icons/chevron-left';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';

	let {
		device,
		selected,
		onselect
	}: { device: number; selected: string; onselect: (date: string) => void } = $props();

	// Follows the selected day; prev/next month buttons override it locally.
	let month = $derived(selected.slice(0, 7));

	const cal = query(() => {
		void live.activity;
		return api<Calendar>('/calendar', { query: { device, month } });
	});

	function shift(delta: number) {
		const [y, m] = month.split('-').map(Number);
		const d = new Date(Date.UTC(y, m - 1 + delta, 1));
		month = d.toISOString().slice(0, 7);
	}

	const label = $derived(
		new Date(`${month}-01T00:00:00Z`).toLocaleDateString('en-GB', {
			month: 'long',
			year: 'numeric',
			timeZone: 'UTC'
		})
	);

	// Monday-first weeks; leading blanks for the first weekday.
	const cells = $derived.by(() => {
		const [y, m] = month.split('-').map(Number);
		const first = new Date(Date.UTC(y, m - 1, 1));
		const lead = (first.getUTCDay() + 6) % 7;
		const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
		const byDate = new Map((cal.data?.month === month ? cal.data.days : []).map((d) => [d.date, d]));
		const out: ({ date: string; day: number; count: number; problems: number; density: number } | null)[] =
			Array(lead).fill(null);
		for (let i = 1; i <= days; i++) {
			const date = `${month}-${String(i).padStart(2, '0')}`;
			const b = byDate.get(date);
			out.push({ date, day: i, count: b?.count ?? 0, problems: b?.problems ?? 0, density: b?.density ?? 0 });
		}
		return out;
	});

	const today = $derived(cal.data?.today ?? '');
	const heat = ['bg-transparent', 'bg-brand/25', 'bg-brand/45', 'bg-brand/70', 'bg-brand'];
</script>

<div class="flex flex-col gap-3">
	<div class="flex items-center justify-between">
		<Button variant="ghost" size="icon-sm" onclick={() => shift(-1)} aria-label="Previous month">
			<ChevronLeft />
		</Button>
		<span class="text-sm font-medium">{label}</span>
		<Button
			variant="ghost"
			size="icon-sm"
			onclick={() => shift(1)}
			disabled={!!today && month >= today.slice(0, 7)}
			aria-label="Next month"
		>
			<ChevronRight />
		</Button>
	</div>
	{#if !cal.data && cal.loading}
		<Skeleton class="h-56 w-full" />
	{:else}
		<div class="grid grid-cols-7 gap-1 text-center" role="grid" aria-label="{label}, clips per day">
			{#each ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'] as wd (wd)}
				<span class="text-subtle pb-1 text-xs" role="columnheader">{wd}</span>
			{/each}
			{#each cells as c, i (c?.date ?? `blank-${i}`)}
				{#if c}
					{@const future = !!today && c.date > today}
					<Tooltip.Root>
						<Tooltip.Trigger>
							{#snippet child({ props })}
								<button
									{...props}
									type="button"
									disabled={future}
									onclick={() => onselect(c.date)}
									aria-pressed={c.date === selected}
									aria-label="{c.date}: {c.count} clips{c.problems ? `, ${c.problems} problems` : ''}"
									class={cn(
										'hover:bg-muted relative flex aspect-square flex-col items-center justify-center gap-1 rounded-md text-sm tabular-nums transition-colors disabled:opacity-35',
										c.date === selected && 'bg-muted ring-brand-text ring-2 ring-inset',
										c.date === today && 'font-semibold text-brand-text',
										!c.count && 'text-muted-foreground'
									)}
								>
									{c.day}
									<span class={cn('h-1 w-5 rounded-full', heat[c.density])}></span>
									{#if c.problems}
										<span class="bg-bad absolute top-1 right-1 size-1.5 rounded-full" aria-hidden="true"></span>
									{/if}
								</button>
							{/snippet}
						</Tooltip.Trigger>
						<Tooltip.Content>
							{#if future}
								Still to come
							{:else if c.count}
								<span class="flex gap-x-3">
									<span>{plural(c.count, 'clip')}</span>
									{#if c.problems}<span>{plural(c.problems, 'problem')}</span>{/if}
								</span>
							{:else}
								No clips
							{/if}
						</Tooltip.Content>
					</Tooltip.Root>
				{:else}
					<span></span>
				{/if}
			{/each}
		</div>
	{/if}
</div>
