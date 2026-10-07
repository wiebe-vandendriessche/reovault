import { api, type DeviceSummary } from '$lib/api';

const KEY = 'rv-device';

/** The camera every device-scoped page shows. Remembered per browser; a
 * `?device=` in the URL (a shared link) wins once, at load. */
export const devices = $state({
	list: [] as DeviceSummary[],
	currentId: null as number | null,
	loaded: false
});

function remembered(): number | null {
	try {
		const v = Number(localStorage.getItem(KEY));
		return Number.isInteger(v) && v > 0 ? v : null;
	} catch {
		return null;
	}
}

export function selectDevice(id: number) {
	devices.currentId = id;
	try {
		localStorage.setItem(KEY, String(id));
	} catch {
		/* ignore */
	}
}

export async function loadDevices(fromUrl?: number | null) {
	devices.list = await api<DeviceSummary[]>('/devices/enabled');
	const ids = devices.list.map((d) => d.id);
	const wanted = [fromUrl, devices.currentId, remembered()].find((id) => id != null && ids.includes(id));
	devices.currentId = wanted ?? ids[0] ?? null;
	devices.loaded = true;
}

export function currentDevice(): DeviceSummary | undefined {
	return devices.list.find((d) => d.id === devices.currentId);
}

export function deviceLabel(d: { name?: string | null; alias: string }): string {
	return d.name || d.alias;
}
