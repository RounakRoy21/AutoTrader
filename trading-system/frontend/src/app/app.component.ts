import { Component, OnInit, OnDestroy } from '@angular/core';
import { RouterOutlet, RouterLink, RouterLinkActive, Router, NavigationStart, NavigationEnd, NavigationCancel, NavigationError } from '@angular/router';
import { CommonModule, AsyncPipe } from '@angular/common';
import { Subject, Observable, interval } from 'rxjs';
import { takeUntil, map, shareReplay } from 'rxjs/operators';

import { MatToolbarModule } from '@angular/material/toolbar';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatBadgeModule } from '@angular/material/badge';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { BreakpointObserver, Breakpoints } from '@angular/cdk/layout';

import { StateService } from './core/services/state.service';
import { TradingWebSocketService, WsConnectionState } from './core/services/trading-websocket.service';
import { ThemeService } from './core/services/theme.service';
import { GrowwAuthBannerComponent } from './shared/groww-auth-banner/groww-auth-banner.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    CommonModule,
    AsyncPipe,
    RouterOutlet,
    RouterLink,
    RouterLinkActive,
    MatToolbarModule,
    MatSidenavModule,
    MatListModule,
    MatIconModule,
    MatButtonModule,
    MatTooltipModule,
    MatProgressBarModule,
    MatBadgeModule,
    MatSnackBarModule,
    GrowwAuthBannerComponent
],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.scss'],
})
export class AppComponent implements OnInit, OnDestroy {
  title = 'AutoTrader Dashboard';
  private destroy$ = new Subject<void>();

  wsState: WsConnectionState = 'connecting';
  growwAuthenticated = true;
  paperTrading = false;
  routeLoading = false;
  isHandset = false;
  decisionsUnread = 0;
  currentTime$!: Observable<string>;

  /** True when viewport â‰¤ 768px drives sidenav mode + default open state. */
  isHandset$: Observable<boolean>;

  constructor(
    private state: StateService,
    private ws: TradingWebSocketService,
    private router: Router,
    private breakpointObserver: BreakpointObserver,
    public themeService: ThemeService,
    private snackBar: MatSnackBar,
  ) {
    this.isHandset$ = this.breakpointObserver
      .observe([Breakpoints.Handset, '(max-width: 768px)'])
      .pipe(
        map((result) => result.matches),
        shareReplay(1),
      );
  }

  ngOnInit(): void {
    this.isHandset$.pipe(takeUntil(this.destroy$)).subscribe((v) => (this.isHandset = v));
    this.ws.connectionState$
      .pipe(takeUntil(this.destroy$))
      .subscribe((s) => (this.wsState = s));

    this.state.growwAuthenticated$
      .pipe(takeUntil(this.destroy$))
      .subscribe((v) => (this.growwAuthenticated = v));

    this.state.paperTrading$
      .pipe(takeUntil(this.destroy$))
      .subscribe((v) => (this.paperTrading = v));

    // Live IST clock
    this.currentTime$ = interval(1000).pipe(
      map(() => new Date().toLocaleTimeString('en-IN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        timeZone: 'Asia/Kolkata', hour12: false,
      })),
      takeUntil(this.destroy$),
      shareReplay(1),
    );

    // Unread decisions badge increment on WS push, reset on navigation to /decisions
    this.ws.decisions$
      .pipe(takeUntil(this.destroy$))
      .subscribe((entry) => {
        if (!this.router.url.startsWith('/decisions')) {
          this.decisionsUnread = Math.min(this.decisionsUnread + 1, 99);
        }
        // EXECUTE toasts
        if (entry.decision === 'EXECUTE') {
          const dir = (entry as any).direction ?? '';
          const msg = `EXECUTE ${entry.stock}${dir ? ' ' + dir : ''}`;
          this.snackBar.open(msg, 'Dismiss', {
            duration: 6000,
            panelClass: ['at-snackbar-profit'],
          });
        }
      });

    // Trade event toasts
    this.ws.trades$
      .pipe(takeUntil(this.destroy$))
      .subscribe((trade: any) => {
        const pnl: number = trade?.realized_pnl ?? 0;
        const sign = pnl >= 0 ? '+' : '';
        const msg = `Trade: ${trade?.stock ?? ''} ₹${sign}${pnl.toFixed(2)}`;
        const cls = pnl >= 0 ? 'at-snackbar-profit' : 'at-snackbar-loss';
        this.snackBar.open(msg, 'OK', { duration: 5000, panelClass: [cls] });
      });

    this.router.events
      .pipe(takeUntil(this.destroy$))
      .subscribe((event) => {
        if (event instanceof NavigationEnd && event.urlAfterRedirects.startsWith('/decisions')) {
          this.decisionsUnread = 0;
        }
        if (event instanceof NavigationStart) {
          this.routeLoading = true;
        } else if (
          event instanceof NavigationEnd ||
          event instanceof NavigationCancel ||
          event instanceof NavigationError
        ) {
          this.routeLoading = false;
        }
      });
  }

  get wsIcon(): string {
    switch (this.wsState) {
      case 'connected': return 'wifi';
      case 'disconnected': return 'wifi_off';
      case 'failed': return 'signal_wifi_bad';
      default: return 'wifi_find';
    }
  }

  get wsLabel(): string {
    switch (this.wsState) {
      case 'connected': return 'LIVE';
      case 'disconnected': return 'RECONNECTING';
      case 'failed': return 'DISCONNECTED';
      default: return 'CONNECTING';
    }
  }

  reloadPage(): void {
    window.location.reload();
  }

  toggleTheme(): void {
    this.themeService.toggle();
  }

  /** Close the sidenav on mobile after navigation. */
  closeOnMobile(sidenav: { close: () => void }): void {
    if (this.isHandset) sidenav.close();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
