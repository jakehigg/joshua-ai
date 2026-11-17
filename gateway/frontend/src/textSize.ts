// How big the text of the viewer is on this device.
//
// The choice belongs to the device, not to the account: a phone stays at the
// normal size while an iPad is larger. So it lives in localStorage and never
// reaches the API. `--j-text-scale` on <html> carries it, and the root rule
// of `index.css` multiplies the root font by it. Every text of the two
// stylesheets is a rem of that root font, so one property moves them all, at
// once and with no reload.
//
// Every access to the storage is in try/catch, because Safari in a private
// window throws on it. Without the storage the size is the normal one and
// the page works as it always does.

export const STORAGE_KEY = "joshua.text-scale";

/** How much bigger than normal the text is. */
export type TextScale = 0.9 | 1 | 1.15 | 1.3;

export const DEFAULT_SCALE: TextScale = 1;

/** The four sizes, as the Profile page words them. */
export const TEXT_SIZE_CHOICES: { value: TextScale; label: string }[] = [
  { value: 0.9, label: "Small" },
  { value: 1, label: "Normal" },
  { value: 1.15, label: "Large" },
  { value: 1.3, label: "Larger" },
];

/** The size this device is set to, or the normal one. */
export function readTextScale(): TextScale {
  let saved: string | null = null;
  try {
    saved = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // No storage: the normal size.
  }
  const choice = TEXT_SIZE_CHOICES.find((one) => String(one.value) === saved);
  return choice === undefined ? DEFAULT_SCALE : choice.value;
}

/** Put a size on the root element, where the stylesheets read it. */
export function applyTextScale(scale: TextScale): void {
  document.documentElement.style.setProperty("--j-text-scale", String(scale));
}

/** Keep a size for this device and show it at once. */
export function setTextScale(scale: TextScale): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(scale));
  } catch {
    // The size holds for this page only.
  }
  applyTextScale(scale);
}

/**
 * The saved size, on the page. `main.tsx` calls it before React renders, so a
 * reload never shows a first paint at the wrong size.
 */
export function startTextScale(): void {
  applyTextScale(readTextScale());
}
