/**
 * ApiService — wraps Angular HttpClient for all REST API interactions.
 */

import { Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { map } from 'rxjs/operators';
import {
  ApiResponse,
  MarketBrief,
  Trade,
  DailyPnl,
  AgentStatus,
  HealthCheck,
  LtpMap,
} from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private baseUrl = '/api';

  constructor(private http: HttpClient) {}

  // ── Market Brief ──────────────────────────────

  getTodayBrief(): Observable<MarketBrief> {
    return this.http.get<MarketBrief>(`${this.baseUrl}/market-brief/today`);
  }

  // ── Trades ────────────────────────────────────

  getTrades(date?: string): Observable<Trade[]> {
    let params = new HttpParams();
    if (date) {
      params = params.set('date', date);
    }
    return this.http.get<Trade[]>(`${this.baseUrl}/trades`, { params });
  }

  getOpenTrades(): Observable<Trade[]> {
    return this.http.get<Trade[]>(`${this.baseUrl}/trades/open`);
  }

  // ── P&L ───────────────────────────────────────

  getDailyPnl(days: number = 30): Observable<DailyPnl[]> {
    const params = new HttpParams().set('days', days.toString());
    return this.http.get<DailyPnl[]>(`${this.baseUrl}/pnl/daily`, { params });
  }

  // ── System ────────────────────────────────────

  getAgentStatus(): Observable<AgentStatus> {
    return this.http
      .get<ApiResponse<AgentStatus>>(`${this.baseUrl}/agent/status`)
      .pipe(map((r) => r.data));
  }

  haltTrading(): Observable<ApiResponse<{ trading_halted: boolean }>> {
    return this.http.post<ApiResponse<{ trading_halted: boolean }>>(
      `${this.baseUrl}/trading/halt`,
      {},
    );
  }

  resumeTrading(): Observable<ApiResponse<{ trading_halted: boolean }>> {
    return this.http.post<ApiResponse<{ trading_halted: boolean }>>(
      `${this.baseUrl}/trading/resume`,
      {},
    );
  }

  startTradingAgent(): Observable<ApiResponse<{ result: string }>> {
    return this.http.post<ApiResponse<{ result: string }>>(
      `${this.baseUrl}/agent/trading/start`,
      {},
    );
  }

  stopTradingAgent(): Observable<ApiResponse<{ result: string }>> {
    return this.http.post<ApiResponse<{ result: string }>>(
      `${this.baseUrl}/agent/trading/stop`,
      {},
    );
  }

  healthCheck(): Observable<HealthCheck> {
    return this.http
      .get<ApiResponse<HealthCheck>>(`${this.baseUrl}/health`)
      .pipe(map((r) => r.data));
  }
}
