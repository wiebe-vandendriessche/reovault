import { toast } from 'svelte-sonner';
import { api } from '$lib/api';

/** "Queued for the next run" with a Run now button, so a retry doesn't
 * have to wait for the schedule or a trip to Health. */
export function toastQueued(message: string, device: number) {
	toast.success(message, {
		action: {
			label: 'Run now',
			onClick: () =>
				api('/actions/run', { method: 'POST', query: { device } }).then(
					() => toast.success('Archive run started.'),
					(e: unknown) => toast.error(e instanceof Error ? e.message : String(e))
				)
		}
	});
}
