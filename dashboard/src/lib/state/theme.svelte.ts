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
	document.documentElement.dataset.theme = next;
	theme.value = next;
	try {
		localStorage.setItem('rv-theme', next);
	} catch {
		/* private mode: the choice just lasts this tab */
	}
}
