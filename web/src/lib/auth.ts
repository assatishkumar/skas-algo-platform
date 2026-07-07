// App authentication token (JWT bearer). Stored in localStorage like the theme choice.
// The backend fails OPEN when auth is unconfigured, so an absent token simply means the
// (localhost/dev) API is open — the login gate only bites once the server enforces auth.

const KEY = "skas-token";

export function getToken(): string | null {
  return localStorage.getItem(KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(KEY);
}

export function isLoggedIn(): boolean {
  return !!getToken();
}
