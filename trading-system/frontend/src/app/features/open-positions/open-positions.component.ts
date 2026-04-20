/**
 * OpenPositionsComponent — real-time table of currently open positions.
 * Rows color-coded by unrealized P&L direction.
 */

import { Component, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, combineLatest } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatTableModule } from '@angular/material/table';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDialogModule, MatDialog } from '@angular/material/dialog';
import { MatSnackBar } from '@angular/material/snack-bar';
import { MatSnackBarModule } from '@angular/material/snack-bar';

import { StateService } from '../../core/services/state.service';
import { ApiService } from '../../core/services/api.service';
import { Trade, LtpMap, AgentStatus } from '../../core/models';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog/confirm-dialog.component';

export interface PositionRow extends Trade {
  ltp: number | null;
  unrealized_pnl: number | null;
  pnl_pct: number | null;
}

@Component({
  selector: 'app-open-positions',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, MatTableModule, MatCardModule, MatChipsModule, MatIconModule,
            MatProgressBarModule, MatButtonModule, MatTooltipModule, MatDialogModule, MatSnackBarModule],
  templateUrl: './open-positions.component.html',
  styleUrls: ['./open-positions.component.scss'],
})
export class OpenPositionsComponent implements OnInit, OnDestroy {
  private destroy$ = new Subject<void>();
  positions: PositionRow[] = [];
  maxPositions = 3;
  loading = true;
  closingId: number | null = null;
  displayedColumns = [
    'stock',
    'direction',
    'quantity',
    'entry_price',
    'ltp',
    'unrealized_pnl',
    'stop_loss_price',
    'target_price',
    'entry_time',
    'close',
  ];

  constructor(
    private state: StateService,
    private api: ApiService,
    private dialog: MatDialog,
    private snackBar: MatSnackBar,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    combineLatest([
      this.state.openPositions$,
      this.state.ltpMap$,
    ])
      .pipe(takeUntil(this.destroy$))
      .subscribe(([trades, ltpMap]) => {
        this.positions = trades.map((t) => this.toRow(t, ltpMap));
        this.loading = false;
        this.cdr.markForCheck();
      });

    this.state.agentStatus$
      .pipe(takeUntil(this.destroy$))
      .subscribe((s) => {
        this.maxPositions = s.config?.max_open_positions ?? 3;
        this.cdr.markForCheck();
      });
  }

  closePosition(row: PositionRow): void {
    const ref = this.dialog.open(ConfirmDialogComponent, {
      data: {
        title: 'Close Position',
        message: `Close ${row.stock} (${row.quantity} × ₹${row.entry_price.toFixed(2)}) at market price?`,
        confirmLabel: 'Close Position',
        confirmColor: 'warn',
      },
    });
    ref.afterClosed().subscribe((confirmed) => {
      if (!confirmed) return;
      this.closingId = row.id;
      this.cdr.markForCheck();
      this.api.closeTrade(row.id).subscribe({
        next: () => {
          this.snackBar.open(`${row.stock} closed at market price`, 'OK', { duration: 4000 });
          this.closingId = null;
          this.cdr.markForCheck();
        },
        error: (err) => {
          const msg = err?.error?.detail ?? 'Close failed — check server logs';
          this.snackBar.open(msg, 'Dismiss', { duration: 8000, panelClass: ['snack-error'] });
          this.closingId = null;
          this.cdr.markForCheck();
        },
      });
    });
  }

  private toRow(t: Trade, ltpMap: LtpMap): PositionRow {
    const ltp = ltpMap[t.stock] ?? null;
    const unrealized_pnl =
      ltp !== null ? (ltp - t.entry_price) * t.quantity : null;
    const pnl_pct =
      ltp !== null && t.entry_price > 0
        ? ((ltp - t.entry_price) / t.entry_price) * 100
        : null;
    return { ...t, ltp, unrealized_pnl, pnl_pct };
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
