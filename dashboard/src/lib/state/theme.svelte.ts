/** Same key as the old dashboard (`rv-theme`), so a saved choice survives the
 * upgrade. app.html applies it before first paint; this keeps it in sync. */
type Theme = 'light' | 'dark';

function current(): Theme {
	return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

export const theme = $state({ value: 'dark' as Theme });

export function initTheme() {
	theme.value = current();
}

export function toggleTheme() {
	const next: Theme = theme.value === 'dark' ? 'light' : 'dark';
	const root = document.documentElement;
	// Transitions off while the colors flip, so nothing fades into the new
	// theme on its own schedule; back on after the next frame, so hover and
	// press transitions keep working.
	root.classList.add('theme-switching');
	root.dataset.theme = next;
	void root.offsetHeight; // apply the new colors now, with transitions off
	requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('theme-switching')));
	theme.value = next;
	try {
		localStorage.setItem('rv-theme', next);
	} catch {
		/* private mode: the choice just lasts this tab */
	}
}
