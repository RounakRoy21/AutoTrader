import { environment } from '../../../environments/environment';

/**
 * Resolve the admin API key shared by the HTTP interceptor (X-Api-Key header)
 * and the /ws/live WebSocket (first-message auth).
 *
 * Priority order:
 *   1. localStorage.getItem('adminApiKey')  — runtime override via browser console
 *   2. environment.adminApiKey              — build-time default
 *
 * Returns '' when no key is configured (backend open-access paper mode).
 */
export function getAdminApiKey(): string {
  let stored: string | null = null;
  try {
    stored = typeof localStorage !== 'undefined' ? localStorage.getItem('adminApiKey') : null;
  } catch {
    stored = null; // storage blocked (private mode / sandbox)
  }
  return stored ?? environment.adminApiKey ?? '';
}
