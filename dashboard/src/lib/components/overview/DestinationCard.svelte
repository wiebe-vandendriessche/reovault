<script lang="ts">
	import type { components } from '#lib/api/schema.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { bytes, count } from '#lib/format.js';

	/** Where the backups go and how long they stay. Read from the local
	 * index, so loading the overview never touches a remote destination. */
	let { destination }: { destination: components['schemas']['DestinationOut'] } = $props();
</script>

<Card.Root>
	<Card.Header>
		<Card.Description>Backup destination</Card.Description>
		<Card.Title class="min-w-0 truncate font-mono text-base">
			{destination.repository ?? 'Not set'}
		</Card.Title>
		<Card.Action>
			<Button size="sm" variant="ghost" href="/settings?tab=backup">Change</Button>
		</Card.Action>
	</Card.Header>
	<Card.Content>
		<dl class="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
			<div>
				<dt class="text-subtle text-xs">Clips backed up</dt>
				<dd class="font-medium tabular-nums">{count(destination.clips)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Footage</dt>
				<dd class="font-medium tabular-nums">{bytes(destination.bytes)}</dd>
			</div>
			<div>
				<dt class="text-subtle text-xs">Kept for</dt>
				<dd class="font-medium">{destination.keep_days ? `${destination.keep_days} days` : 'Forever'}</dd>
			</div>
		</dl>
	</Card.Content>
</Card.Root>
