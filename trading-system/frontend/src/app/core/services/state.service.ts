/**
 * StateService — manages global application state using BehaviorSubjects.
 * Exposes: currentBrief$, openPositions$, dailyPnl$, agentStatus$, systemAlerts$.
 */

import { Injectable, OnDestroy } from '@angular/core';
import { BehaviorSubject, Subject, interval, Subscription } from 'rxjs';
import { takeUntil, catchError, distinctUntilChanged } from 'rxjs/operators';
import { EMPTY } from 'rxjs';
import { MatSnackBar } from '@angular/material/snack-bar';

import { ApiService } from './api.service';
import { TradingWebSocketService } from './trading-websocket.service';
import {
  MarketBrief,
  Trade,
  DailyPnl,
  AgentStatus,
  SystemAlert,
  LtpMap,
  HealthCheck,
} from '../models';

@Injectable({ providedIn: 'root' })
export class StateService implements OnDestroy {
  private destroy$ = new Subject<void>();
  private _dataErrorShown = false;
  private _pollSub: Subscription | null = null;

  // ── State subjects ────────────────────────────
  private _currentBrief$ = new BehaviorSubject<MarketBrief | null>(null);
  private _openPositions$ = new BehaviorSubject<Trade[]>([]);
  private _dailyPnl$ = new BehaviorSubject<DailyPnl[]>([]);
  private _agentStatus$ = new BehaviorSubject<AgentStatus>({
    research_agent: { status: 'INACTIVE', step: 'IDLE', last_bias: null, last_confidence: null, last_completed: null },
    trading_agent: { status: 'INACTIVE', trading_halted: false, daily_trade_count: 0, last_signal_stock: null, last_signal_time: null },
    risk_manager: { status: 'INACTIVE', daily_loss: 0, drawdown_pct: 0 },
  });
  private _systemAlerts$ = new BehaviorSubject<SystemAlert[]>([]);
  private _ltpMap$ = new BehaviorSubject<LtpMap>({});
  private _healthCheck$ = new BehaviorSubject<HealthCheck | null>(null);
  private _growwAuthenticated$ = new BehaviorSubject<boolean>(true);
  private _paperTrading$ = new BehaviorSubject<boolean>(false);

  // ── Public observables ────────────────────
  readonly currentBrief$ = this._currentBrief$.asObservable();
  readonly openPositions$ = this._openPositions$.asObservable();
  readonly dailyPnl$ = this._dailyPnl$.asObservable();
  readonly agentStatus$ = this._agentStatus$.asObservable();
  readonly systemAlerts$ = this._systemAlerts$.asObservable();
  readonly ltpMap$ = this._ltpMap$.asObservable();
  readonly healthCheck$ = this._healthCheck$.asObservable();
  readonly growwAuthenticated$ = this._growwAuthenticated$.asObservable();
  readonly paperTrading$ = this._paperTrading$.asObservable();

  constructor(
    private api: ApiService,
    private ws: TradingWebSocketService,
    private snackBar: MatSnackBar,
  ) {
    this.initPolling();
    this.initWebSocket();
  }

  /** Poll REST endpoints every 15 seconds for state refresh.
   * When the WebSocket is live, reduces to a 60s heartbeat so the WS
   * carries the real-time load.  Polling resumes at full rate on disconnect.
   */
  private initPolling(): void {
    this.refreshAll();

    // Dynamically adjust poll interval based on WS connection state.
    // connected → 60s (just a heartbeat); anything else → 15s
    this.ws.connectionState$
      .pipe(distinctUntilChanged(), takeUntil(this.destroy$))
      .subscribe((state) => {
        if (this._pollSub) {
          this._pollSub.unsubscribe();
          this._pollSub = null;
        }
        const intervalMs = state === 'connected' ? 60_000 : 15_000;
        this._pollSub = interval(intervalMs)
          .pipe(takeUntil(this.destroy$))
          .subscribe(() => this.refreshAll());
      });

    // Poll health + groww auth every 30 seconds (independent of WS)
    this.refreshHealth();
    interval(30_000)
      .pipe(takeUntil(this.destroy$))
      .subscribe(() => this.refreshHealth());
  }

  /** Subscribe to WebSocket events for real-time updates. */
  private initWebSocket(): void {
    // Trade events → refresh open positions
    this.ws.trades$.pipe(takeUntil(this.destroy$)).subscribe(() => {
      this.api
        .getOpenTrades()
        .pipe(catchError(() => EMPTY))
        .subscribe((trades) => this._openPositions$.next(trades));
    });

    // Market brief updates
    this.ws.marketBrief$.pipe(takeUntil(this.destroy$)).subscribe((data) => {
      this._currentBrief$.next(data as unknown as MarketBrief);
    });

    // System alerts
    this.ws.alerts$.pipe(takeUntil(this.destroy$)).subscribe((data) => {
      const alert: SystemAlert = {
        type: (data['type'] as string) || 'info',
        message: (data['message'] as string) || JSON.stringify(data),
        timestamp: (data['timestamp'] as string) || new Date().toISOString(),
      };
      const current = this._systemAlerts$.getValue();
      this._systemAlerts$.next([alert, ...current].slice(0, 100));
    });

    // LTP snapshots — merge incoming prices with the current map
    this.ws.ltp$.pipe(takeUntil(this.destroy$)).subscribe((prices) => {
      this._ltpMap$.next({ ...this._ltpMap$.getValue(), ...prices });
    });
  }

  /** Refresh all data from REST endpoints. */
  refreshAll(): void {
    this.api
      .getTodayBrief()
      .pipe(catchError(() => EMPTY))
      .subscribe((brief) => this._currentBrief$.next(brief));

    this.api
      .getOpenTrades()
      .pipe(catchError(() => EMPTY))
      .subscribe((trades) => this._openPositions$.next(trades));

    this.api
      .getDailyPnl(30)
      .pipe(catchError(() => EMPTY))
      .subscribe((pnl) => this._dailyPnl$.next(pnl));

    this.api
      .getAgentStatus()
      .pipe(
        catchError(() => {
          if (!this._dataErrorShown) {
            this._dataErrorShown = true;
            this.snackBar.open('Unable to reach backend — data may be stale.', 'Dismiss', {
              duration: 6000,
              panelClass: ['snack-error'],
            });
          }
          return EMPTY;
        }),
      )
      .subscribe((status) => {
        this._dataErrorShown = false;
        this._agentStatus$.next(status);
        if (status.config !== undefined) {
          this._paperTrading$.next(status.config.paper_trading);
        }
      });
  }

  /** Refresh health check and Groww auth status. */
  refreshHealth(): void {
    this.api
      .healthCheck()
      .pipe(catchError(() => EMPTY))
      .subscribe((h) => {
        this._healthCheck$.next(h);
        this._growwAuthenticated$.next(h.groww_api === 'authenticated');
      });
  }

  ngOnDestroy(): void {
    if (this._pollSub) this._pollSub.unsubscribe();
    this.destroy$.next();
    this.destroy$.complete();
  }
}
