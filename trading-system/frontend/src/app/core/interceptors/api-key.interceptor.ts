import { HttpInterceptorFn } from '@angular/common/http';
import { getAdminApiKey } from '../auth/admin-api-key';

/**
 * Attaches the X-Api-Key header to all /api/ requests when an admin key is configured.
 *
 * The backend requires the key on every mutating route (trade close, broker
 * login/logout, market-brief run, halt/resume, trading start/stop, on-demand
 * checks) and refuses to boot in live mode without one.  Read-only GETs stay open.
 *
 * Key resolution (localStorage override → environment) lives in getAdminApiKey().
 * In paper-trading mode with no key set, the header is omitted, matching the
 * backend's open-access behaviour when ADMIN_API_KEY is empty.
 */
export const apiKeyInterceptor: HttpInterceptorFn = (req, next) => {
  const apiKey = getAdminApiKey();

  if (apiKey && req.url.startsWith('/api/')) {
    return next(req.clone({ headers: req.headers.set('X-Api-Key', apiKey) }));
  }
  return next(req);
};
