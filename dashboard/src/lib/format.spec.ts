import { describe, expect, it } from 'vitest';
import { addDays, bytes, duration, plural, relative } from './format';

describe('bytes', () => {
	it('uses binary units and one decimal above bytes', () => {
		expect(bytes(512)).toBe('512 B');
		expect(bytes(1536)).toBe('1.5 KB');
		expect(bytes(5 * 1024 ** 3)).toBe('5.0 GB');
		expect(bytes(null)).toBe('-');
	});
});

describe('relative', () => {
	const now = Date.parse('2026-10-07T12:00:00Z');
	it('is coarse in both directions', () => {
		expect(relative('2026-10-07T11:59:30Z', now)).toBe('just now');
		expect(relative('2026-10-07T09:00:00Z', now)).toBe('3h ago');
		expect(relative('2026-10-07T12:20:00Z', now)).toBe('in 20m');
		expect(relative('2026-10-04T12:00:00Z', now)).toBe('3d ago');
		expect(relative(null, now)).toBe('-');
	});
});

describe('addDays', () => {
	it('walks calendar dates across month, year and DST edges', () => {
		expect(addDays('2026-10-31', 1)).toBe('2026-11-01');
		expect(addDays('2027-01-01', -1)).toBe('2026-12-31');
		expect(addDays('2026-10-25', 1)).toBe('2026-10-26'); // EU clocks go back
	});
});

describe('duration and plural', () => {
	it('formats', () => {
		expect(duration(42)).toBe('42s');
		expect(duration(125)).toBe('2m 05s');
		expect(plural(1, 'clip')).toBe('1 clip');
		expect(plural(3, 'clip')).toBe('3 clips');
	});
});
