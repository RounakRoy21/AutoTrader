/**
 * PnlChartComponent — line chart of daily P&L using Chart.js / ng2-charts.
 */

import { Component, OnInit, OnDestroy, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { NgChartsModule } from 'ng2-charts';
import { ChartConfiguration, ChartType } from 'chart.js';

import { StateService } from '../../core/services/state.service';
import { DailyPnl } from '../../core/models';

@Component({
  selector: 'app-pnl-chart',
  standalone: true,
  imports: [CommonModule, MatCardModule, NgChartsModule],
  templateUrl: './pnl-chart.component.html',
  styleUrls: ['./pnl-chart.component.scss'],
})
export class PnlChartComponent implements OnInit, OnDestroy {
  private destroy$ = new Subject<void>();

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

  constructor(private state: StateService) {}

  ngOnInit(): void {
    this.state.dailyPnl$.pipe(takeUntil(this.destroy$)).subscribe((pnl) => {
      this.updateChart(pnl);
    });
  }

  private updateChart(pnl: DailyPnl[]): void {
    // Sort oldest first
    const sorted = [...pnl].sort(
      (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime(),
    );

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
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
