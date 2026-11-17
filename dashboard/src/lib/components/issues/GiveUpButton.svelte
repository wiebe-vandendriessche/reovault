<script lang="ts">
	import { api } from '#lib/api/index.js';
	import * as AlertDialog from '#lib/components/ui/alert-dialog/index.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import { toast } from 'svelte-sonner';

	/** Stop trying clips that keep failing: they count as lost and are never
	 * downloaded again. `ids` omitted: every failing clip of the camera. */
	let { camera, ids, label = 'Give up' }: { camera: string; ids?: number[]; label?: string } = $props();
	let open = $state(false);
	let busy = $state(false);

	async function giveUp() {
		busy = true;
		try {
			const r = await api<{ changed: number }>(`/cameras/${encodeURIComponent(camera)}/clips/give-up`, {
				method: 'POST',
				body: { ids: ids ?? null }
			});
			toast.success(r.changed === 1 ? 'Gave up on 1 clip.' : `Gave up on ${r.changed} clips.`);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			busy = false;
			open = false;
		}
	}
</script>

<Button size="sm" variant="outline" disabled={busy} onclick={() => (open = true)}>{label}</Button>
<AlertDialog.Root bind:open>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>Give up on {ids?.length === 1 ? 'this clip' : 'these clips'}?</AlertDialog.Title>
			<AlertDialog.Description>
				They count as lost and are never downloaded again. Do this for clips the camera can't hand out
				whole; they would otherwise be retried once a day until the SD card overwrites them.
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel>Keep trying</AlertDialog.Cancel>
			<AlertDialog.Action onclick={giveUp}>Give up</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
