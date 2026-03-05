/**
 * StateService — manages global application state using BehaviorSubjects.
 * Exposes: currentBrief$, openPositions$, dailyPnl$, agentStatus$, systemAlerts$.
 */

import { Injectable, OnDestroy } from '@angular/core';
import { BehaviorSubject, Subject, interval } from 'rxjs';
import { takeUntil, switchMap, catchError } from 'rxjs/operators';
import { EMPTY } from 'rxjs';

import { ApiService } from './api.service';
import { TradingWebSocketService } from './trading-websocket.service';
import {
  MarketBrief,
  Trade,
  DailyPnl,
  AgentStatus,
  SystemAlert,
  LtpMap,
} from '../models';

@Injectable({ providedIn: 'root' })
export class StateService implements OnDestroy {
  private destroy$ = new Subject<void>();

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

  // ── Public observables ────────────────────
  readonly currentBrief$ = this._currentBrief$.asObservable();
  readonly openPositions$ = this._openPositions$.asObservable();
  readonly dailyPnl$ = this._dailyPnl$.asObservable();
  readonly agentStatus$ = this._agentStatus$.asObservable();
  readonly systemAlerts$ = this._systemAlerts$.asObservable();
  readonly ltpMap$ = this._ltpMap$.asObservable();

  constructor(
    private api: ApiService,
    private ws: TradingWebSocketService,
  ) {
    this.initPolling();
    this.initWebSocket();
  }

  /** Poll REST endpoints every 15 seconds for state refresh. */
  private initPolling(): void {
    // Load initial data
    this.refreshAll();

    interval(15_000)
      .pipe(takeUntil(this.destroy$))
      .subscribe(() => this.refreshAll());
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
      .pipe(catchError(() => EMPTY))
      .subscribe((status) => this._agentStatus$.next(status));
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
