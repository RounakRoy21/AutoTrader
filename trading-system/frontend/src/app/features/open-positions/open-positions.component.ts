/**
 * OpenPositionsComponent — real-time table of currently open positions.
 * Rows color-coded by unrealized P&L direction.
 */

import { Component, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, combineLatest } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatTableModule } from '@angular/material/table';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';

import { StateService } from '../../core/services/state.service';
import { Trade, LtpMap } from '../../core/models';

export interface PositionRow extends Trade {
  ltp: number | null;
  unrealized_pnl: number | null;
  pnl_pct: number | null;
}

@Component({
  selector: 'app-open-positions',
  standalone: true,
  imports: [CommonModule, MatTableModule, MatCardModule, MatChipsModule, MatIconModule],
  templateUrl: './open-positions.component.html',
  styleUrls: ['./open-positions.component.scss'],
})
export class OpenPositionsComponent implements OnInit, OnDestroy {
  private destroy$ = new Subject<void>();
  positions: PositionRow[] = [];
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
  ];

  constructor(private state: StateService) {}

  ngOnInit(): void {
    combineLatest([
      this.state.openPositions$,
      this.state.ltpMap$,
    ])
      .pipe(takeUntil(this.destroy$))
      .subscribe(([trades, ltpMap]) => {
        this.positions = trades.map((t) => this.toRow(t, ltpMap));
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
