/** Words for the raw values the API returns (camera detection types, clip
 * states, run outcomes), so the screen never shows "dog_cat" or "partial". */

function words(raw: string): string {
	const s = raw.replaceAll('_', ' ');
	return s.charAt(0).toUpperCase() + s.slice(1);
}

// Values seen in `reolink-cli vod search` recordType (D340W fixture) plus
// the ones the schema documents; anything else is shown cleaned up.
const TYPES: Record<string, string> = {
	md: 'Motion',
	people: 'Person',
	vehicle: 'Vehicle',
	dog_cat: 'Pet',
	visitor: 'Doorbell',
	package: 'Package',
	sched: 'Scheduled'
};
export const typeLabel = (t: string) => TYPES[t] ?? words(t);

const STATES: Record<string, string> = {
	discovered: 'Not archived yet',
	downloading: 'Downloading',
	verifying: 'Verifying',
	archived: 'Archived',
	failed: 'Failed',
	quarantined: 'Quarantined',
	pruned: 'Removed by retention',
	lost: 'Lost, overwritten on the camera'
};
export const stateLabel = (s: string) => STATES[s] ?? words(s);

const OUTCOMES: Record<string, string> = {
	success: 'Success',
	partial: 'Partly failed',
	failed: 'Failed',
	aborted: 'Stopped'
};
export const outcomeLabel = (o: string | null | undefined) => (o ? (OUTCOMES[o] ?? words(o)) : 'Unknown');

const TRIGGERS: Record<string, string> = { schedule: 'Scheduled', manual: 'Manual', backfill: 'Backfill' };
export const triggerLabel = (t: string) => TRIGGERS[t] ?? words(t);
