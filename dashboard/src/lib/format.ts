export function bytes(n: number | null | undefined): string {
	if (n == null) return '-';
	let v = n;
	for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
		if (v < 1024 || unit === 'TB') return unit === 'B' ? `${v.toFixed(0)} B` : `${v.toFixed(1)} ${unit}`;
		v /= 1024;
	}
	return `${v.toFixed(1)} TB`;
}

/** "3h ago" / "in 12m": coarse on purpose, it sits next to an exact time. */
export function relative(iso: string | null | undefined, now = Date.now()): string {
	if (!iso) return '-';
	const secs = (Date.parse(iso) - now) / 1000;
	const abs = Math.abs(secs);
	if (abs < 60) return secs <= 0 ? 'just now' : 'in under a minute';
	const days = Math.floor(abs / 86400);
	const hours = Math.floor((abs % 86400) / 3600);
	const mins = Math.floor((abs % 3600) / 60);
	const span = days ? `${days}d` : hours ? `${hours}h` : `${mins}m`;
	return secs < 0 ? `${span} ago` : `in ${span}`;
}

/** A timestamp in the camera's own timezone, e.g. "20 Sep 2026, 21:00 CEST". */
export function localTime(iso: string | null | undefined, tz: string): string {
	if (!iso) return '-';
	try {
		return new Intl.DateTimeFormat('en-GB', {
			day: '2-digit',
			month: 'short',
			year: 'numeric',
			hour: '2-digit',
			minute: '2-digit',
			timeZone: tz,
			timeZoneName: 'short'
		}).format(new Date(iso));
	} catch {
		return new Date(iso).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
	}
}

export function duration(secs: number | null | undefined): string {
	if (secs == null) return '-';
	const s = Math.round(secs);
	return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

export function plural(n: number, word: string): string {
	return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/** YYYY-MM-DD arithmetic on calendar dates, never through local timezones. */
export function addDays(isoDate: string, days: number): string {
	const d = new Date(`${isoDate}T00:00:00Z`);
	d.setUTCDate(d.getUTCDate() + days);
	return d.toISOString().slice(0, 10);
}
