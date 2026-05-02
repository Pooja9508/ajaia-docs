/**
 * auth.js — Token storage and auth helpers.
 *
 * Tokens are stored in sessionStorage (not localStorage) so they expire
 * automatically when the browser tab closes. The token value itself is
 * never logged or written to the DOM.
 */

const Auth = (() => {
  const TOKEN_KEY = 'ajaia_token';
  const USER_KEY  = 'ajaia_user';

  function setSession(token, user) {
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(USER_KEY, JSON.stringify(user));
  }

  function clearSession() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
  }

  function getToken() {
    return sessionStorage.getItem(TOKEN_KEY);
  }

  function getUser() {
    try {
      return JSON.parse(sessionStorage.getItem(USER_KEY));
    } catch {
      return null;
    }
  }

  function isLoggedIn() {
    return !!getToken();
  }

  function authHeaders() {
    const token = getToken();
    if (!token) return {};
    return { 'Authorization': `Bearer ${token}` };
  }

  async function login(username, password) {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Login failed');
    }
    const data = await res.json();
    setSession(data.access_token, data.user);
    return data.user;
  }

  function logout() {
    clearSession();
    window.location.href = '/';
  }

  /** Wrap fetch with auth header and auto-redirect on 401. */
  async function apiFetch(url, options = {}) {
    const headers = { ...authHeaders(), ...(options.headers || {}) };
    const res = await fetch(url, { ...options, headers });
    if (res.status === 401) {
      clearSession();
      window.location.href = '/';
      throw new Error('Session expired');
    }
    return res;
  }

  return { login, logout, getToken, getUser, isLoggedIn, authHeaders, apiFetch };
})();
