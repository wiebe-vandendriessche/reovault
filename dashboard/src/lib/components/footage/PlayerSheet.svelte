<script lang="ts">
	/** Phone player: the clip in a bottom sheet, with previous/next to watch
	 * an hour through without closing it. Desktop keeps the inline player. */
	import { apiUrl, type Recording } from '$lib/api';
	import * as Sheet from '$lib/components/ui/sheet';
	import * as AspectRatio from '$lib/components/ui/aspect-ratio';
	import { ButtonGroup } from '$lib/components/ui/button-group';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { bytes, duration } from '$lib/format';
	import ChevronLeft from '@lucide/svelte/icons/chevron-left';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import Download from '@lucide/svelte/icons/download';

	let { clips, index = $bindable() }: { clips: Recording[]; index: number | null } = $props();

	// Only archived clips are playable; prev/next skip the rest.
	const playable = $derived(clips.filter((c) => c.state === 'archived'));
	const pos = $derived(index == null ? -1 : playable.findIndex((c) => c.id === clips[index!]?.id));
	const clip = $derived(pos >= 0 ? playable[pos] : null);
	let unsupported = $state(false);

	function step(delta: number) {
		const next = playable[pos + delta];
		if (!next) return;
		unsupported = false;
		index = clips.findIndex((c) => c.id === next.id);
	}
</script>

<Sheet.Root open={clip !== null} onOpenChange={(o) => !o && (index = null)}>
	<Sheet.Content side="bottom" class="max-h-[92dvh] gap-3 rounded-t-xl pb-[max(1rem,env(safe-area-inset-bottom))]">
		{#if clip}
			<Sheet.Header class="pb-0">
				<Sheet.Title class="tabular-nums">{clip.local_time}</Sheet.Title>
				<Sheet.Description class="flex flex-wrap items-center gap-1">
					{#each clip.types as t (t)}<Badge variant="secondary">{t}</Badge>{/each}
					<span class="ml-1 flex gap-x-3 tabular-nums">
						<span>{duration(clip.duration_s)}</span>
						<span>{bytes(clip.plaintext_size)}</span>
					</span>
				</Sheet.Description>
			</Sheet.Header>
			<div class="px-4">
				{#if unsupported}
					<div class="bg-muted flex flex-col items-start gap-3 rounded-md p-4 text-sm">
						<p>This browser can't play this clip's video format (often H.265).</p>
					</div>
				{:else}
					<AspectRatio.Root ratio={16 / 9} class="overflow-hidden rounded-md bg-black">
						{#key clip.id}
							<!-- svelte-ignore a11y_media_has_caption -->
							<video
								class="size-full"
								src={apiUrl(`/recordings/${clip.id}/stream`)}
								controls
								autoplay
								playsinline
								preload="metadata"
								onerror={() => (unsupported = true)}
							></video>
						{/key}
					</AspectRatio.Root>
				{/if}
			</div>
			<div class="flex items-center justify-between gap-2 px-4">
				<ButtonGroup>
					<Button variant="outline" onclick={() => step(-1)} disabled={pos <= 0} aria-label="Previous clip">
						<ChevronLeft /> Previous
					</Button>
					<Button
						variant="outline"
						onclick={() => step(1)}
						disabled={pos >= playable.length - 1}
						aria-label="Next clip"
					>
						Next <ChevronRight />
					</Button>
				</ButtonGroup>
				<Button variant="ghost" href={apiUrl(`/recordings/${clip.id}/stream`, { download: 1 })}>
					<Download /> Download
				</Button>
			</div>
		{/if}
	</Sheet.Content>
</Sheet.Root>
