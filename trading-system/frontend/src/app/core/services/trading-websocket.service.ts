/**
 * TradingWebSocketService — wraps WebSocketSubject from rxjs/webSocket.
 * Exposes typed Observables for each event type (trades$, pnl$, alerts$).
 * Implements exponential backoff reconnection logic.
 */

import { Injectable, OnDestroy } from '@angular/core';
import { webSocket, WebSocketSubject } from 'rxjs/webSocket';
import { Observable, Subject, BehaviorSubject, timer, EMPTY } from 'rxjs';
import { filter, map, takeUntil } from 'rxjs/operators';
import { WsEvent, LtpMap } from '../models';

const MAX_RETRIES = 10;
const RECONNECT_BASE_MS = 1000;

export type WsConnectionState = 'connecting' | 'connected' | 'disconnected' | 'failed';

@Injectable({ providedIn: 'root' })
export class TradingWebSocketService implements OnDestroy {
  private socket$: WebSocketSubject<WsEvent> | null = null;
  private messages$ = new Subject<WsEvent>();
  private destroy$ = new Subject<void>();
  private reconnectAttempt = 0;

  private _connectionState$ = new BehaviorSubject<WsConnectionState>('connecting');
  readonly connectionState$ = this._connectionState$.asObservable();

  /**
   * Derive WebSocket URL from the page's own origin.
   * Works on localhost (ng serve proxy), any server IP, and any domain —
   * no hardcoded hostnames needed.
   */
  private getWsUrl(): string {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/ws/live`;
  }

  constructor() {
    this.connect();
  }

  /** Establish WebSocket connection with exponential backoff. */
  private connect(): void {
    this._connectionState$.next('connecting');
    this.socket$ = webSocket<WsEvent>({
      url: this.getWsUrl(),
      openObserver: {
        next: () => {
          console.log('[WS] Connected');
          this.reconnectAttempt = 0;
          this._connectionState$.next('connected');
        },
      },
      closeObserver: {
        next: () => {
          console.warn('[WS] Disconnected — scheduling reconnect');
          this._connectionState$.next('disconnected');
          this.reconnect();
        },
      },
    });

    this.socket$
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (msg) => this.messages$.next(msg),
        error: (err) => {
          console.error('[WS] Error:', err);
          this.reconnect();
        },
      });
  }

  private reconnect(): void {
    if (this.reconnectAttempt >= MAX_RETRIES) {
      console.error('[WS] Max retries reached');
      this._connectionState$.next('failed');
      return;
    }
    const delay = RECONNECT_BASE_MS * Math.pow(2, this.reconnectAttempt);
    this.reconnectAttempt++;
    console.log(`[WS] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempt})`);
    timer(delay)
      .pipe(takeUntil(this.destroy$))
      .subscribe(() => this.connect());
  }

  /** Observable of trade events (TRADE_OPENED, TRADE_CLOSED). */
  get trades$(): Observable<Record<string, unknown>> {
    return this.messages$.pipe(
      filter((e) => e.channel === 'trade_events'),
      map((e) => e.data),
      takeUntil(this.destroy$),
    );
  }

  /** Observable of P&L updates and EOD reports. */
  get pnl$(): Observable<Record<string, unknown>> {
    return this.messages$.pipe(
      filter((e) => e.channel === 'eod_report'),
      map((e) => e.data),
      takeUntil(this.destroy$),
    );
  }

  /** Observable of system alerts. */
  get alerts$(): Observable<Record<string, unknown>> {
    return this.messages$.pipe(
      filter((e) => e.channel === 'system_alerts'),
      map((e) => e.data),
      takeUntil(this.destroy$),
    );
  }

  /** Observable of market brief updates. */
  get marketBrief$(): Observable<Record<string, unknown>> {
    return this.messages$.pipe(
      filter((e) => e.channel === 'market_brief'),
      map((e) => e.data),
      takeUntil(this.destroy$),
    );
  }

  /** Observable of live LTP snapshots from ltp_store. */
  get ltp$(): Observable<LtpMap> {
    return this.messages$.pipe(
      filter((e) => e.channel === 'ltp_update'),
      map((e) => e.data as LtpMap),
      takeUntil(this.destroy$),
    );
  }

  /** Raw message stream. */
  get allMessages$(): Observable<WsEvent> {
    return this.messages$.asObservable().pipe(takeUntil(this.destroy$));
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.socket$?.complete();
  }
}
