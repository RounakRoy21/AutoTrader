/**
 * TradeLogComponent — paginated, filterable table of closed trades.
 * Shows win-rate summary above the table.
 */

import { Component, OnInit, AfterViewInit, OnDestroy, ViewChild, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatTableModule, MatTableDataSource } from '@angular/material/table';
import { MatPaginatorModule, MatPaginator } from '@angular/material/paginator';
import { MatSortModule, MatSort } from '@angular/material/sort';
import { MatCardModule } from '@angular/material/card';
import { MatInputModule } from '@angular/material/input';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { BreakpointObserver } from '@angular/cdk/layout';
import { ActivatedRoute } from '@angular/router';
import { animate, state, style, transition, trigger } from '@angular/animations';

import { ApiService } from '../../core/services/api.service';
import { Trade } from '../../core/models';

@Component({
  selector: 'app-trade-log',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  animations: [
    trigger('detailExpand', [
      state('collapsed', style({ height: '0px', minHeight: '0' })),
      state('expanded', style({ height: '*' })),
      transition('expanded <=> collapsed', animate('200ms cubic-bezier(0.4, 0.0, 0.2, 1)')),
    ]),
  ],
  imports: [
    CommonModule,
    FormsModule,
    MatTableModule,
    MatPaginatorModule,
    MatSortModule,
    MatCardModule,
    MatInputModule,
    MatFormFieldModule,
    MatIconModule,
    MatProgressBarModule,
    MatTooltipModule,
  ],
  templateUrl: './trade-log.component.html',
  styleUrls: ['./trade-log.component.scss'],
})
export class TradeLogComponent implements OnInit, AfterViewInit, OnDestroy {
  @ViewChild(MatPaginator) paginator!: MatPaginator;
  @ViewChild(MatSort) sort!: MatSort;

  private destroy$ = new Subject<void>();

  dataSource = new MatTableDataSource<Trade>([]);

  private readonly ALL_COLUMNS = [
    'expand', 'trade_date', 'stock', 'quantity',
    'entry_price', 'exit_price', 'exit_reason', 'realized_pnl',
  ];
  private readonly MOBILE_COLUMNS = ['expand', 'trade_date', 'stock', 'realized_pnl'];
  displayedColumns = this.ALL_COLUMNS;

  totalTrades = 0;
  wins = 0;
  losses = 0;
  winRate = 0;
  totalPnl = 0;
  loading = true;
  expandedRow: Trade | null = null;

  constructor(private api: ApiService, private cdr: ChangeDetectorRef, private bp: BreakpointObserver, private route: ActivatedRoute) {}

  ngOnInit(): void {
    this.bp.observe(['(max-width: 768px)'])
      .pipe(takeUntil(this.destroy$))
      .subscribe(r => {
        this.displayedColumns = r.matches ? this.MOBILE_COLUMNS : this.ALL_COLUMNS;
        this.cdr.markForCheck();
      });

    // Apply date filter from query param (navigated from PnlChart click)
    this.route.queryParams
      .pipe(takeUntil(this.destroy$))
      .subscribe(params => {
        if (params['date']) {
          this.dataSource.filter = params['date'].trim().toLowerCase();
        }
      });

    this.loadTrades();
  }

  ngAfterViewInit(): void {
    this.dataSource.paginator = this.paginator;
    this.dataSource.sort = this.sort;
  }

  loadTrades(): void {
    this.loading = true;
    this.api
      .getTrades()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (trades) => {
          const closed = trades.filter((t) => t.status === 'CLOSED');
          this.dataSource.data = closed;
          this.totalTrades = closed.length;
          this.wins = closed.filter((t) => (t.realized_pnl || 0) > 0).length;
          this.losses = closed.filter((t) => (t.realized_pnl || 0) < 0).length;
          this.winRate =
            this.totalTrades > 0 ? (this.wins / this.totalTrades) * 100 : 0;
          this.totalPnl = closed.reduce(
            (s, t) => s + (t.realized_pnl || 0),
            0,
          );
          this.loading = false;
          this.cdr.markForCheck();
        },
        error: () => {
          this.loading = false;
          this.cdr.markForCheck();
        },
      });
  }

  toggleRow(row: Trade): void {
    this.expandedRow = this.expandedRow === row ? null : row;
  }

  applyFilter(event: Event): void {
    const val = (event.target as HTMLInputElement).value;
    this.dataSource.filter = val.trim().toLowerCase();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
