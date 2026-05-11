/**
 * PnlChartComponent — line chart of daily P&L using Chart.js / ng2-charts.
 * Includes a time-range toggle (7d / 30d / 90d).
 */

import { Component, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { NgChartsModule } from 'ng2-charts';
import { ChartConfiguration, ChartType } from 'chart.js';

import { StateService } from '../../core/services/state.service';
import { ApiService } from '../../core/services/api.service';
import { ThemeService } from '../../core/services/theme.service';
import { DailyPnl } from '../../core/models';

@Component({
  selector: 'app-pnl-chart',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, MatCardModule, MatButtonToggleModule, MatProgressBarModule, NgChartsModule],
  templateUrl: './pnl-chart.component.html',
  styleUrls: ['./pnl-chart.component.scss'],
})
export class PnlChartComponent implements OnInit, OnDestroy {
  private destroy$ = new Subject<void>();
  private allPnl: DailyPnl[] = [];
  loading = true;

  selectedDays = 30;
  readonly periodOptions = [
    { label: '7D', value: 7 },
    { label: '30D', value: 30 },
    { label: '90D', value: 90 },
  ];

  chartType: ChartType = 'line';

  chartData: ChartConfiguration['data'] = {
    labels: [],
    datasets: [
      {
        label: 'Realized P&L (₹)',
        data: [],
        borderColor: '#1976d2',
        backgroundColor: 'rgba(25,118,210,0.08)',
        fill: true,
        tension: 0.3,
        pointRadius: 4,
      },
      {
        label: 'Cumulative P&L (₹)',
        data: [],
        borderColor: '#4caf50',
        borderDash: [6, 3],
        fill: false,
        tension: 0.3,
        pointRadius: 3,
      },
    ],
  };

  chartOptions: ChartConfiguration['options'] = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { position: 'top' },
      tooltip: { mode: 'index', intersect: false },
    },
    scales: {
      y: {
        title: { display: true, text: '₹' },
        grid: { color: 'rgba(0,0,0,0.06)' },
      },
      x: {
        title: { display: true, text: 'Date' },
        grid: { display: false },
      },
    },
  };

  constructor(
    private state: StateService,
    private api: ApiService,
    private cdr: ChangeDetectorRef,
    private themeService: ThemeService,
    private router: Router,
  ) {}

  onBarClick(event: any): void {
    try {
      const active = event?.active as any[];
      if (!active?.length) return;
      const idx = active[0].index;
      const labels = this.chartData.labels as string[];
      const date = labels?.[idx];
      if (date) {
        this.router.navigate(['/trades'], { queryParams: { date } });
      }
    } catch { /* ignore */ }
  }

  ngOnInit(): void {
    this.themeService.dark$.pipe(takeUntil(this.destroy$)).subscribe(dark => {
      this.chartOptions = this._buildChartOptions(dark);
      this.cdr.markForCheck();
    });

    this.state.dailyPnl$.pipe(takeUntil(this.destroy$)).subscribe((pnl) => {
      this.allPnl = pnl;
      this.loading = false;
      this.updateChart();
    });
  }

  onPeriodChange(days: number): void {
    if (days === this.selectedDays) return;
    this.selectedDays = days;
    if (this.allPnl.length < days) {
      this.loading = true;
      this.cdr.markForCheck();
      this.api.getDailyPnl(days).pipe(takeUntil(this.destroy$)).subscribe((pnl) => {
        this.allPnl = pnl;
        this.loading = false;
        this.updateChart();
      });
    } else {
      this.updateChart();
    }
  }

  private updateChart(): void {
    // Sort oldest first, then slice to selected window
    const sorted = [...this.allPnl]
      .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime())
      .slice(-this.selectedDays);

    const labels = sorted.map((d) => d.date);
    const daily = sorted.map((d) => d.realized_pnl);
    const cumulative: number[] = [];
    daily.reduce((acc, val) => {
      const c = acc + val;
      cumulative.push(c);
      return c;
    }, 0);

    this.chartData = {
      labels,
      datasets: [
        { ...this.chartData.datasets[0], data: daily },
        { ...this.chartData.datasets[1], data: cumulative },
      ],
    };
    this.cdr.markForCheck();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  private _buildChartOptions(dark: boolean): ChartConfiguration['options'] {
    const gridColor  = dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';
    const labelColor = dark ? 'rgba(255,255,255,0.7)'  : 'rgba(0,0,0,0.6)';
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { color: labelColor } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        y: {
          title: { display: true, text: '₹', color: labelColor },
          grid:  { color: gridColor },
          ticks: { color: labelColor },
        },
        x: {
          title: { display: true, text: 'Date', color: labelColor },
          grid:  { display: false },
          ticks: { color: labelColor },
        },
      },
    };
  }
}
