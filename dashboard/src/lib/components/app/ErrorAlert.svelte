<script lang="ts">
	import * as Alert from '$lib/components/ui/alert';
	import { Button } from '$lib/components/ui/button';
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';

	let { message, title = "Couldn't load this" }: { message: string; title?: string } = $props();
</script>

<Alert.Root variant="destructive">
	<CircleAlert />
	<Alert.Title>{title}</Alert.Title>
	<Alert.Description class="flex flex-col items-start gap-2">
		<p>{message}</p>
		<Button variant="outline" size="sm" onclick={() => location.reload()}><RotateCw /> Try again</Button>
	</Alert.Description>
</Alert.Root>
