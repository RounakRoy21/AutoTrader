import { HttpInterceptorFn } from '@angular/common/http';
import { environment } from '../../../environments/environment';

/**
 * Attaches the X-Api-Key header to all /api/ requests when an admin key is configured.
 *
 * Priority order:
 *   1. localStorage.getItem('adminApiKey')  — runtime override via browser console
 *   2. environment.adminApiKey              — build-time default
 *
 * In development / paper-trading mode, leave the key blank and the header is omitted,
 * matching the backend's open-access behaviour when ADMIN_API_KEY is empty.
 */
export const apiKeyInterceptor: HttpInterceptorFn = (req, next) => {
  const apiKey =
    (typeof localStorage !== 'undefined' ? localStorage.getItem('adminApiKey') : null) ??
    environment.adminApiKey ??
    '';

  if (apiKey && req.url.startsWith('/api/')) {
    return next(req.clone({ headers: req.headers.set('X-Api-Key', apiKey) }));
  }
  return next(req);
};
