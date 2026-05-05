import { ApplicationConfig, importProvidersFrom } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { MatSnackBarModule } from '@angular/material/snack-bar';
import { MAT_DIALOG_DEFAULT_OPTIONS } from '@angular/material/dialog';
import { Overlay } from '@angular/cdk/overlay';
import { routes } from './app.routes';
import { apiKeyInterceptor } from './core/interceptors/api-key.interceptor';

export const appConfig: ApplicationConfig = {
  providers: [
    provideRouter(routes),
    provideHttpClient(withInterceptors([apiKeyInterceptor])),
    provideAnimations(),
    importProvidersFrom(MatSnackBarModule),
    // Use NoopScrollStrategy for all dialogs to prevent CDK BlockScrollStrategy
    // from adding `position:fixed; overflow-y:scroll` to <html> on dialog open.
    // That rule shifts the layout viewport width on Android Chrome, causing the
    // dashboard grid cards to expand beyond screen width (the mobile overflow bug).
    {
      provide: MAT_DIALOG_DEFAULT_OPTIONS,
      useFactory: (overlay: Overlay) => ({
        scrollStrategy: overlay.scrollStrategies.noop(),
        hasBackdrop: true,
      }),
      deps: [Overlay],
    },
  ],
};
