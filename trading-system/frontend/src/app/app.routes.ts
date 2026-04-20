import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  {
    path: 'dashboard',
    loadComponent: () =>
      import('./features/dashboard/dashboard.component').then(m => m.DashboardComponent),
  },
  {
    path: 'positions',
    loadComponent: () =>
      import('./features/open-positions/open-positions.component').then(m => m.OpenPositionsComponent),
  },
  {
    path: 'trades',
    loadComponent: () =>
      import('./features/trade-log/trade-log.component').then(m => m.TradeLogComponent),
  },
  {
    path: 'pnl',
    loadComponent: () =>
      import('./features/pnl-chart/pnl-chart.component').then(m => m.PnlChartComponent),
  },
  {
    path: 'alerts',
    loadComponent: () =>
      import('./features/system-alerts/system-alerts.component').then(m => m.SystemAlertsComponent),
  },
  {
    path: 'auth/kite',
    loadComponent: () =>
      import('./features/kite-auth/kite-auth-callback.component').then(m => m.KiteAuthCallbackComponent),
  },
];
