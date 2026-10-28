<script lang="ts">
	import { api, type Camera, type Overview } from '#lib/api/index.js';
	import { live } from '#lib/state/live.svelte.js';
	import { query } from '#lib/state/query.svelte.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Skeleton } from '#lib/components/ui/skeleton/index.js';
	import PageHeader from '#lib/components/app/PageHeader.svelte';
	import ErrorAlert from '#lib/components/app/ErrorAlert.svelte';
	import NoCamera from '#lib/components/app/NoCamera.svelte';
	import CameraTile from '#lib/components/overview/CameraTile.svelte';
	import DestinationCard from '#lib/components/overview/DestinationCard.svelte';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';

	const overview = query(() => {
		void live.activity;
		void live.settings;
		void live.alert;
		return api<Overview>('/overview');
	});

	// Worst first, so the camera that needs you is at the top.
	const rank: Record<Camera['verdict'], number> = {
		not_mounted: 0,
		failing: 1,
		at_risk: 2,
		catching_up: 3,
		unknown: 4,
		up_to_date: 5
	};
	const cameras = $derived(
		[...(overview.data?.cameras ?? [])].sort((a, b) => rank[a.verdict] - rank[b.verdict])
	);

	function replace(c: Camera) {
		if (!overview.data) return;
		overview.data.cameras = overview.data.cameras.map((x) => (x.alias === c.alias ? c : x));
	}
</script>

<svelte:head><title>Overview | ReoVault</title></svelte:head>

<PageHeader title="Overview" description="Is every SD card backed up, and where do the backups go." />

{#if overview.error}
	<ErrorAlert message={overview.error} />
{:else if !overview.data}
	<div class="flex flex-col gap-4">
		<Skeleton class="h-24 rounded-xl" />
		<div class="grid gap-4 md:grid-cols-2">
			{#each [0, 1] as i (i)}<Skeleton class="h-56 rounded-xl" />{/each}
		</div>
	</div>
{:else if overview.data.cameras.length === 0}
	<NoCamera />
{:else}
	{@const o = overview.data}
	<div class="flex flex-col gap-8">
		<section aria-labelledby="verdict-heading">
			{#if o.issues.length === 0}
				<div class="flex items-start gap-3">
					<ShieldCheck class="text-ok mt-0.5 size-6 shrink-0" aria-hidden="true" />
					<div>
						<h2 id="verdict-heading" class="text-xl font-semibold">Nothing needs attention</h2>
						<p class="text-muted-foreground mt-1 text-sm">
							{cameras.every((c) => c.verdict === 'up_to_date')
								? 'Every clip on every SD card is backed up.'
								: 'Backups are running normally.'}
						</p>
					</div>
				</div>
			{:else}
				<Card.Root class="border-warn/40">
					<Card.Header>
						<Card.Title id="verdict-heading" class="text-xl">
							{o.issues.length === 1 ? '1 thing needs attention' : `${o.issues.length} things need attention`}
						</Card.Title>
					</Card.Header>
					<Card.Content>
						<ul class="flex flex-col gap-4">
							{#each o.issues as issue, i (i)}
								<li class="flex items-start gap-3">
									<TriangleAlert class="text-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
									<div class="min-w-0">
										<p class="text-sm font-medium">{issue.title}</p>
										<p class="text-muted-foreground text-sm">{issue.message}</p>
									</div>
								</li>
							{/each}
						</ul>
					</Card.Content>
				</Card.Root>
			{/if}
		</section>

		<section class="flex flex-col gap-3" aria-labelledby="cameras-heading">
			<h2 id="cameras-heading" class="text-base font-semibold">Cameras</h2>
			<div class="grid gap-4 md:grid-cols-2">
				{#each cameras as cam (cam.alias)}
					<CameraTile {cam} onchange={replace} />
				{/each}
			</div>
		</section>

		<DestinationCard destination={o.destination} />
	</div>
{/if}
