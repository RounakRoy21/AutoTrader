export const environment = {
  production: true,
  // Set ADMIN_API_KEY matching the backend .env value before building for production,
  // or leave '' and set it at runtime: localStorage.setItem('adminApiKey', '<key>').
  // Required for every mutating call and for the /ws/live stream in live mode.
  adminApiKey: '',
};
