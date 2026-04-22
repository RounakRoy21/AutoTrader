import { Component, OnInit, OnDestroy } from '@angular/core';
import { RouterOutlet, RouterLink, RouterLinkActive, Router, NavigationStart, NavigationEnd, NavigationCancel, NavigationError } from '@angular/router';
import { CommonModule, AsyncPipe } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatToolbarModule } from '@angular/material/toolbar';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { BreakpointObserver, Breakpoints } from '@angular/cdk/layout';
import { Observable } from 'rxjs';
import { map, shareReplay } from 'rxjs/operators';

import { StateService } from './core/services/state.service';
import { TradingWebSocketService, WsConnectionState } from './core/services/trading-websocket.service';
import { GrowwAuthBannerComponent } from './shared/kite-auth-banner/kite-auth-banner.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    CommonModule,
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

  /** True when viewport ≤ 768px — drives sidenav mode + default open state. */
  isHandset$: Observable<boolean>;

  constructor(
    private state: StateService,
    private ws: TradingWebSocketService,
    private router: Router,
    private breakpointObserver: BreakpointObserver,
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

    // Route-level loading indicator
    this.router.events
      .pipe(takeUntil(this.destroy$))
      .subscribe((event) => {
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

  /** Close the sidenav on mobile after navigation. */
  closeOnMobile(sidenav: { close: () => void }): void {
    if (this.isHandset) sidenav.close();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
