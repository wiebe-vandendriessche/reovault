<script lang="ts">
	import * as Empty from '$lib/components/ui/empty';
	import { Button } from '$lib/components/ui/button';
	import Cctv from '@lucide/svelte/icons/cctv';
</script>

<Empty.Root class="border">
	<Empty.Header>
		<Empty.Media variant="icon"><Cctv /></Empty.Media>
		<Empty.Title>No camera is enabled</Empty.Title>
		<Empty.Description>
			Add a camera, or turn one back on, to start archiving its recordings.
		</Empty.Description>
	</Empty.Header>
	<Empty.Content>
		<Button href="/devices">Go to Devices</Button>
	</Empty.Content>
</Empty.Root>
