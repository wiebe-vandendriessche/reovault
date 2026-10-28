/** Words for the raw values the API returns (camera detection types, clip
 * states, run outcomes, verdicts), so the screen never shows "dog_cat". */
import type { Verdict } from '#lib/api/index.js';

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
export const typesOf = (recType: string | null | undefined) =>
	(recType ?? '').split(',').filter(Boolean);

const STATES: Record<string, string> = {
	pending: 'Waiting for backup',
	backed_up: 'Backed up',
	lost: 'Lost, overwritten on the camera',
	expired: 'Removed by retention'
};
export const stateLabel = (s: string) => STATES[s] ?? words(s);

const OUTCOMES: Record<string, string> = { ok: 'Done', error: 'Failed', stopped: 'Stopped' };
export const outcomeLabel = (o: string | null | undefined) => (o ? (OUTCOMES[o] ?? words(o)) : 'Running');

const TRIGGERS: Record<string, string> = { schedule: 'Scheduled', manual: 'Manual' };
export const triggerLabel = (t: string) => TRIGGERS[t] ?? words(t);

type Level = 'ok' | 'warn' | 'bad' | 'idle';
export const VERDICTS: Record<Verdict, { level: Level; label: string }> = {
	up_to_date: { level: 'ok', label: 'Up to date' },
	catching_up: { level: 'idle', label: 'Catching up' },
	at_risk: { level: 'warn', label: 'At risk' },
	failing: { level: 'bad', label: 'Failing' },
	not_mounted: { level: 'bad', label: 'No SD card' },
	unknown: { level: 'idle', label: 'No backup yet' }
};
