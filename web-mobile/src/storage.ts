/** Small persistent-settings layer. Uses Capacitor Preferences when running inside the
 * native shell (Keychain/NSUserDefaults-backed on iOS), localStorage in a plain browser —
 * so the app is fully developable with `vite dev` before the iOS shell exists. */

type PrefsPlugin = {
  get(o: { key: string }): Promise<{ value: string | null }>;
  set(o: { key: string; value: string }): Promise<void>;
  remove(o: { key: string }): Promise<void>;
};

// The plugin is kept inside a wrapper object: resolving a Capacitor plugin PROXY through
// a promise makes `await` probe its `.then()`, which the proxy rejects with
// `"Preferences.then()" is not implemented` — the blank-screen bug of 2026-07-16.
let holder: { p: PrefsPlugin } | null | undefined; // undefined = not probed yet

async function plugin(): Promise<{ p: PrefsPlugin } | null> {
  if (holder !== undefined) return holder;
  try {
    const mod = await import("@capacitor/preferences");
    holder = { p: mod.Preferences as unknown as PrefsPlugin };
  } catch {
    holder = null; // browser dev — fall back to localStorage
  }
  return holder;
}

export async function getSetting(key: string): Promise<string | null> {
  const h = await plugin();
  if (h) return (await h.p.get({ key })).value;
  return localStorage.getItem(key);
}

export async function setSetting(key: string, value: string): Promise<void> {
  const h = await plugin();
  if (h) await h.p.set({ key, value });
  else localStorage.setItem(key, value);
}

export async function removeSetting(key: string): Promise<void> {
  const h = await plugin();
  if (h) await h.p.remove({ key });
  else localStorage.removeItem(key);
}

export const KEYS = {
  backendUrl: "skas-backend-url",
  token: "skas-token", // mirrored into localStorage for the shared client's authHeaders()
};
