/**
 * DashboardComponent — landing page.
 * Shows live P&L ticker, agent status indicators, Market Brief summary, quick stats,
 * system health widget, VIX regime, and recommended stance.
 */

import { Component, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, race, timer, interval } from 'rxjs';
import { takeUntil, take, map, filter } from 'rxjs/operators';

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
import { SystemAlertsComponent } from '../system-alerts/system-alerts.component';

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
    SystemAlertsComponent,
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
      if (confirmed) {
        // Optimistic: flip the flag immediately so buttons update without waiting for the next poll.
        this.agentStatus = { ...this.agentStatus, trading_agent: { ...this.agentStatus.trading_agent, trading_halted: true } };
        this.cdr.markForCheck();
        this.api.haltTrading().subscribe(() => this.state.refreshAll());
      }
    });
  }

  resume(): void {
    // Optimistic: flip the flag immediately.
    this.agentStatus = { ...this.agentStatus, trading_agent: { ...this.agentStatus.trading_agent, trading_halted: false } };
    this.cdr.markForCheck();
    this.api.resumeTrading().subscribe(() => this.state.refreshAll());
  }

  startAgent(): void {
    this.agentActionInProgress = true;
    // Optimistic: mark ACTIVE so Start disables and Stop enables right away.
    this.agentStatus = { ...this.agentStatus, trading_agent: { ...this.agentStatus.trading_agent, status: 'ACTIVE' } };
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
      // Optimistic: mark INACTIVE so Stop disables and Start enables right away.
      this.agentStatus = { ...this.agentStatus, trading_agent: { ...this.agentStatus.trading_agent, status: 'INACTIVE' } };
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

  get drawdownTooltip(): string {
    const loss = this.agentStatus.risk_manager?.daily_loss ?? 0;
    const limit = this.agentStatus.config?.daily_drawdown_limit;
    const pct = this.agentStatus.config?.daily_drawdown_limit_pct;
    const pctLabel = pct != null ? `${(pct * 100).toFixed(0)}%` : '3%';
    const limitLabel = limit != null ? `₹${limit.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : 'your daily limit';
    return [
      `How close today's losses are to the auto-halt threshold.`,
      `₹${loss.toLocaleString('en-IN', { maximumFractionDigits: 0 })} lost · Limit = ${pctLabel} of capital = ${limitLabel}`,
      `Trading halts automatically at 100%.`,
    ].join('\n');
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

    // Snapshot identity of current brief and agent completion time so we can
    // detect genuinely new data vs. no-op refreshes.
    const prevDate = this.brief?.date ?? null;
    const prevGeneratedAt = this.brief?.generated_at ?? null;
    const prevLastCompleted = this.agentStatus.research_agent?.last_completed ?? null;

    this.api.runMarketBrief().subscribe({
      next: () => {
        // Background task sets ACTIVE in Redis ~ms after the 202 response.
        // Refresh status after 1s so the Research Agent card shows ACTIVE promptly.
        timer(1000)
          .pipe(takeUntil(this.destroy$))
          .subscribe(() => { this.state.refreshAll(); this.cdr.markForCheck(); });

        // Backup poll every 15s — catches WS misses and updates agentStatus$.
        const pollSub = interval(15_000)
          .pipe(takeUntil(this.destroy$))
          .subscribe(() => this.state.refreshAll());

        // Winning condition 1: new brief arrives directly in currentBrief$ (via WS or poll).
        const briefArrived$ = this.state.currentBrief$.pipe(
          filter((b) => b != null && (b.date !== prevDate || b.generated_at !== prevGeneratedAt)),
          take(1),
          map(() => true as const),
        );

        // Winning condition 2: research_agent.last_completed changes — the backend sets this
        // right after persist_and_publish(), so the brief is already in the DB when this fires.
        const agentFinished$ = this.state.agentStatus$.pipe(
          filter((s) => {
            const lc = s.research_agent?.last_completed ?? null;
            return lc !== null && lc !== prevLastCompleted;
          }),
          take(1),
          map(() => true as const),
        );

        // 5-minute hard timeout — only fires if agent hangs or crashes without updating Redis.
        race(
          briefArrived$,
          agentFinished$,
          timer(300_000).pipe(map(() => false as const)),
        )
          .pipe(take(1), takeUntil(this.destroy$))
          .subscribe((arrived) => {
            pollSub.unsubscribe();
            this.briefRunInProgress = false;
            if (arrived) {
              this.snackBar.open('Market Brief generated', 'Dismiss', { duration: 4000 });
              this.state.refreshAll();
            } else {
              this.snackBar.open(
                'Research Agent is taking longer than expected — refresh manually.',
                'Dismiss',
                { duration: 6000 },
              );
            }
            this.cdr.markForCheck();
          });
      },
      error: (err) => {
        this.briefRunInProgress = false;
        const msg = err?.error?.detail ?? 'Failed to trigger Research Agent';
        this.snackBar.open(msg, 'Dismiss', { duration: 6000 });
        this.cdr.markForCheck();
      },
    });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
