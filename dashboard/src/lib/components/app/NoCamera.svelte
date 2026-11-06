<script lang="ts">
	import * as Empty from '#lib/components/ui/empty/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import Cctv from '@lucide/svelte/icons/cctv';
</script>

<Empty.Root class="border">
	<Empty.Header>
		<Empty.Media variant="icon"><Cctv /></Empty.Media>
		<Empty.Title>No camera yet</Empty.Title>
		<Empty.Description>
			Add a camera, or turn one back on, to start backing up its SD card.
		</Empty.Description>
	</Empty.Header>
	<Empty.Content>
		<Button href="/cameras">Go to Cameras</Button>
	</Empty.Content>
</Empty.Root>
