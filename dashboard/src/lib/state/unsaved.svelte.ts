/** Forms with edits not yet saved. The layout asks before any navigation
 * (a link, a Settings tab, a reload) would drop them, and a sheet asks
 * before it closes. A form registers with `trackUnsaved(() => dirty)` in its
 * script; the entry goes away when the form unmounts. */
const dirty = new Set<symbol>();

export function trackUnsaved(isDirty: () => boolean) {
	const id = Symbol();
	$effect(() => {
		if (isDirty()) dirty.add(id);
		else dirty.delete(id);
		return () => dirty.delete(id);
	});
}

export function hasUnsaved(): boolean {
	return dirty.size > 0;
}

/** True when it's fine to drop the edits: none, or the user said so. */
export function confirmDiscard(): boolean {
	return !hasUnsaved() || confirm('You have unsaved changes. Discard them?');
}
