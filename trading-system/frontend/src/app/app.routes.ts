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
    path: 'decisions',
    loadComponent: () =>
      import('./features/decision-feed/decision-feed.component').then(m => m.DecisionFeedComponent),
  },
  {
    path: 'news-health',
    loadComponent: () =>
      import('./features/news-health/news-health.component').then(m => m.NewsHealthComponent),
  },
  {
    path: 'auth/groww',
    loadComponent: () =>
      import('./features/groww-auth/groww-auth-callback.component').then(m => m.GrowwAuthCallbackComponent),
  },
];
