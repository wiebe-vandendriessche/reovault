<script lang="ts">
	import { Input } from '$lib/components/ui/input';

	/** A text field for an IANA timezone: suggests the browser's list and
	 * refuses a name the browser doesn't know (the server checks it too).
	 * A wrong zone silently shifts every timestamp, so it's worth a guard. */
	let { id, value = $bindable(''), disabled = false }: { id: string; value?: string; disabled?: boolean } =
		$props();

	const zones = (() => {
		try {
			return Intl.supportedValuesOf('timeZone');
		} catch {
			return [];
		}
	})();

	function valid(tz: string): boolean {
		try {
			new Intl.DateTimeFormat('en', { timeZone: tz });
			return true;
		} catch {
			return false;
		}
	}

	function check(e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		el.setCustomValidity(!el.value || valid(el.value) ? '' : 'Not a timezone name, e.g. Europe/Brussels.');
	}
</script>

<Input {id} required {disabled} list="{id}-zones" autocomplete="off" spellcheck="false" bind:value oninput={check} onblur={check} />
<datalist id="{id}-zones">
	{#each zones as z (z)}<option value={z}></option>{/each}
</datalist>
