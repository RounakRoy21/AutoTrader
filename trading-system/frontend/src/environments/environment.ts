export const environment = {
  production: false,
  // Admin API key for trading-control endpoints (halt, resume, start, stop).
  // Set this to your ADMIN_API_KEY value if one is configured on the backend.
  // In development / paper-trading mode with no key set, leave this empty.
  // Runtime override: localStorage.setItem('adminApiKey', 'your-key') in the browser console.
  adminApiKey: '',
};
