/** A fetch bound to reactive inputs: rerun whenever anything `load` reads
 * synchronously changes (the device id, a date, a `live` counter). A late
 * response from a superseded run is dropped, so fast clicking never shows
 * stale data. Previous data stays visible while refetching, so live updates
 * don't flash skeletons. */
export function query<T>(load: () => Promise<T> | null) {
	const state = $state({
		data: undefined as T | undefined,
		error: null as string | null,
		loading: true
	});
	let run = 0;
	$effect(() => {
		const id = ++run;
		const promise = load();
		if (promise === null) return;
		state.loading = true;
		promise.then(
			(data) => {
				if (id !== run) return;
				state.data = data;
				state.error = null;
				state.loading = false;
			},
			(e: unknown) => {
				if (id !== run) return;
				state.error = e instanceof Error ? e.message : String(e);
				state.loading = false;
			}
		);
	});
	return state;
}
