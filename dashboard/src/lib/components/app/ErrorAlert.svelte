<script lang="ts">
	import * as Alert from '$lib/components/ui/alert';
	import CircleAlert from '@lucide/svelte/icons/circle-alert';

	let { message, title = "Couldn't load this" }: { message: string; title?: string } = $props();
</script>

<Alert.Root variant="destructive">
	<CircleAlert />
	<Alert.Title>{title}</Alert.Title>
	<Alert.Description>{message}</Alert.Description>
</Alert.Root>
