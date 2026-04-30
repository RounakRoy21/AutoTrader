/**
 * DashboardComponent — landing page.
 * Shows live P&L ticker, agent status indicators, Market Brief summary, quick stats,
 * system health widget, VIX regime, and recommended stance.
 */

import { Component, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, interval } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatDialogModule, MatDialog } from '@angular/material/dialog';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';

import { StateService } from '../../core/services/state.service';
import { ApiService } from '../../core/services/api.service';
import { MarketBrief, Trade, AgentStatus, HealthCheck } from '../../core/models';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog/confirm-dialog.component';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatCardModule,
    MatChipsModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    MatProgressBarModule,
    MatDialogModule,
    MatTooltipModule,
    MatSnackBarModule,
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
  health: HealthCheck | null = null;
  todayPnl = 0;
  todayTrades = 0;
  unrealizedPnl = 0;
  winRate = 0;
  agentActionInProgress = false;
  briefRunInProgress = false;
  ltpMap: Record<string, number> = {};

  constructor(
    private state: StateService,
    private api: ApiService,
    private dialog: MatDialog,
    private snackBar: MatSnackBar,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.state.currentBrief$
      .pipe(takeUntil(this.destroy$))
      .subscribe((b) => { this.brief = b; this.cdr.markForCheck(); });

    this.state.openPositions$
      .pipe(takeUntil(this.destroy$))
      .subscribe((pos) => {
        this.openPositions = pos;
        this.cdr.markForCheck();
      });

    this.state.ltpMap$
      .pipe(takeUntil(this.destroy$))
      .subscribe((map) => {
        this.ltpMap = map;
        this.recalcUnrealized();
        this.cdr.markForCheck();
      });

    this.state.agentStatus$
      .pipe(takeUntil(this.destroy$))
      .subscribe((s) => { this.agentStatus = s; this.cdr.markForCheck(); });

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
        this.cdr.markForCheck();
      });

    this.state.healthCheck$
      .pipe(takeUntil(this.destroy$))
      .subscribe((h) => { this.health = h; this.cdr.markForCheck(); });
  }

  private recalcUnrealized(): void {
    this.unrealizedPnl = this.openPositions.reduce((sum, t) => {
      const ltp = this.ltpMap[t.stock];
      if (ltp == null) return sum;
      return sum + (ltp - t.entry_price) * t.quantity;
    }, 0);
  }

  halt(): void {
    this.dialog.open(ConfirmDialogComponent, {
      data: { title: 'Halt Trading', message: 'Stop all new trade entries until manually resumed?', confirmLabel: 'Halt', confirmColor: 'warn' },
    }).afterClosed().subscribe((confirmed) => {
      if (confirmed) this.api.haltTrading().subscribe(() => this.state.refreshAll());
    });
  }

  resume(): void {
    this.api.resumeTrading().subscribe(() => this.state.refreshAll());
  }

  startAgent(): void {
    this.agentActionInProgress = true;
    this.cdr.markForCheck();
    this.api.startTradingAgent().subscribe({
      next: () => { this.agentActionInProgress = false; this.state.refreshAll(); this.cdr.markForCheck(); },
      error: () => { this.agentActionInProgress = false; this.cdr.markForCheck(); },
    });
  }

  stopAgent(): void {
    this.dialog.open(ConfirmDialogComponent, {
      data: { title: 'Stop Trading Agent', message: 'Stop the trading agent? Open positions will NOT be closed automatically.', confirmLabel: 'Stop Agent', confirmColor: 'warn' },
    }).afterClosed().subscribe((confirmed) => {
      if (!confirmed) return;
      this.agentActionInProgress = true;
      this.cdr.markForCheck();
      this.api.stopTradingAgent().subscribe({
        next: () => { this.agentActionInProgress = false; this.state.refreshAll(); this.cdr.markForCheck(); },
        error: () => { this.agentActionInProgress = false; this.cdr.markForCheck(); },
      });
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

  get marketStatusLabel(): string {
    return this.agentStatus.market_status?.label ?? '—';
  }

  get marketStatusClass(): string {
    switch (this.agentStatus.market_status?.status) {
      case 'OPEN':     return 'market-open';
      case 'PRE_OPEN': return 'market-pre-open';
      default:         return 'market-closed';
    }
  }

  get totalPnl(): number {
    return this.todayPnl + this.unrealizedPnl;
  }

  get maxTrades(): number {
    return this.agentStatus.config?.max_trades_per_day ?? 6;
  }

  get maxPositions(): number {
    return this.agentStatus.config?.max_open_positions ?? 3;
  }

  buildAnthropicTooltip(a: AgentStatus['anthropic']): string {
    const r = a?.calls_research_today ?? 0;
    const d = a?.calls_decision_today ?? 0;
    return `Research (Sonnet): ${r} calls · Decision (Haiku): ${d} calls`;
  }

  get vixValue(): number | null {
    return (this.brief?.raw_json?.['india_vix'] as Record<string, number>)?.['value'] ?? null;
  }

  get vixRegime(): string {
    return ((this.brief?.raw_json?.['india_vix'] as Record<string, string>)?.['regime'] ?? 'UNKNOWN');
  }

  get vixRegimeClass(): string {
    switch (this.vixRegime) {
      case 'STRESS': return 'regime-stress';
      case 'ELEVATED': return 'regime-elevated';
      case 'NORMAL': return 'regime-normal';
      default: return '';
    }
  }

  healthStatusClass(status: string | undefined): string {
    if (!status) return 'health-unknown';
    return status === 'healthy' || status === 'authenticated' ? 'health-ok' : 'health-bad';
  }

  healthIcon(status: string | undefined): string {
    if (!status) return 'help';
    return status === 'healthy' || status === 'authenticated' ? 'check_circle' : 'cancel';
  }

  get stanceLabel(): string {
    const s = this.brief?.recommended_stance;
    if (!s) return '';
    return s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  runBrief(): void {
    this.briefRunInProgress = true;
    this.cdr.markForCheck();
    this.api.runMarketBrief().subscribe({
      next: () => this.pollForBrief(),
      error: (err) => {
        this.briefRunInProgress = false;
        const msg = err?.error?.detail ?? 'Failed to trigger Research Agent';
        this.snackBar.open(msg, 'Dismiss', { duration: 6000 });
        this.cdr.markForCheck();
      },
    });
  }

  private pollForBrief(): void {
    const maxAttempts = 30; // 30 × 3s = 90s
    let attempts = 0;
    const poll = interval(3000)
      .pipe(takeUntil(this.destroy$))
      .subscribe(() => {
        attempts++;
        this.api.getTodayBrief().subscribe({
          next: () => {
            poll.unsubscribe();
            this.briefRunInProgress = false;
            this.snackBar.open('Market Brief generated', 'Dismiss', { duration: 4000 });
            this.state.refreshAll();
            this.cdr.markForCheck();
          },
          error: () => {
            if (attempts >= maxAttempts) {
              poll.unsubscribe();
              this.briefRunInProgress = false;
              this.snackBar.open(
                'Research Agent is taking longer than expected — refresh manually.',
                'Dismiss',
                { duration: 6000 },
              );
              this.cdr.markForCheck();
            }
          },
        });
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
