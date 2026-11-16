export const environment = {
  production: false,
  // Admin API key: sent as X-Api-Key on mutating /api/ calls (trade close, broker
  // login/logout, market-brief run, halt/resume, start/stop) and as the first
  // message on /ws/live.  Must match the backend ADMIN_API_KEY when one is set
  // (always the case in live mode — the backend refuses to boot without it).
  // In paper-trading mode with no backend key set, leave this empty.
  // Runtime override: localStorage.setItem('adminApiKey', 'your-key') in the browser console.
  adminApiKey: '',
};
