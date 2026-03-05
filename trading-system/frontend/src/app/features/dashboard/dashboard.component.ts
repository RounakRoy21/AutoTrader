/**
 * DashboardComponent — landing page.
 * Shows live P&L ticker, agent status indicators, Market Brief summary, quick stats.
 */

import { Component, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatProgressBarModule } from '@angular/material/progress-bar';

import { StateService } from '../../core/services/state.service';
import { ApiService } from '../../core/services/api.service';
import { MarketBrief, Trade, AgentStatus } from '../../core/models';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatChipsModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    MatProgressBarModule,
  ],
  templateUrl: './dashboard.component.html',
  styleUrls: ['./dashboard.component.scss'],
})
export class DashboardComponent implements OnInit, OnDestroy {
  private destroy$ = new Subject<void>();

  brief: MarketBrief | null = null;
  openPositions: Trade[] = [];
  agentStatus: AgentStatus = {
    research_agent: { status: 'INACTIVE', step: 'IDLE', last_bias: null, last_confidence: null, last_completed: null },
    trading_agent: { status: 'INACTIVE', trading_halted: false, daily_trade_count: 0, last_signal_stock: null, last_signal_time: null },
    risk_manager: { status: 'INACTIVE', daily_loss: 0, drawdown_pct: 0 },
  };
  todayPnl = 0;
  todayTrades = 0;
  winRate = 0;
  agentActionInProgress = false;

  constructor(
    private state: StateService,
    private api: ApiService,
  ) {}

  ngOnInit(): void {
    this.state.currentBrief$
      .pipe(takeUntil(this.destroy$))
      .subscribe((b) => (this.brief = b));

    this.state.openPositions$
      .pipe(takeUntil(this.destroy$))
      .subscribe((pos) => {
        this.openPositions = pos;
      });

    this.state.agentStatus$
      .pipe(takeUntil(this.destroy$))
      .subscribe((s) => (this.agentStatus = s));

    this.state.dailyPnl$
      .pipe(takeUntil(this.destroy$))
      .subscribe((pnl) => {
        const totalWins = pnl.reduce((s, d) => s + d.winning_trades, 0);
        const total = pnl.reduce((s, d) => s + d.total_trades, 0);
        this.winRate = total > 0 ? (totalWins / total) * 100 : 0;
        if (pnl.length) {
          this.todayTrades = pnl[0].total_trades;
          this.todayPnl = pnl[0].realized_pnl;
        }
      });
  }

  halt(): void {
    this.api.haltTrading().subscribe(() => this.state.refreshAll());
  }

  resume(): void {
    this.api.resumeTrading().subscribe(() => this.state.refreshAll());
  }

  startAgent(): void {
    this.agentActionInProgress = true;
    this.api.startTradingAgent().subscribe({
      next: () => {
        this.agentActionInProgress = false;
        this.state.refreshAll();
      },
      error: () => (this.agentActionInProgress = false),
    });
  }

  stopAgent(): void {
    this.agentActionInProgress = true;
    this.api.stopTradingAgent().subscribe({
      next: () => {
        this.agentActionInProgress = false;
        this.state.refreshAll();
      },
      error: () => (this.agentActionInProgress = false),
    });
  }

  get drawdownPct(): number {
    return this.agentStatus.risk_manager?.drawdown_pct ?? 0;
  }

  get drawdownColor(): string {
    if (this.drawdownPct >= 80) return 'warn';
    if (this.drawdownPct >= 50) return 'accent';
    return 'primary';
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
