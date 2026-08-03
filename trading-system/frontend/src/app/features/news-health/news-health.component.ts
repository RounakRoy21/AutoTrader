/**
 * NewsHealthComponent — operator visibility into the news aggregator's sources.
 *
 * Lists every RSS feed and Google-News query with its current status (OK / STALE
 * / DOWN), fresh-item count and last error, so a silently-broken feed URL is
 * caught before news quality degrades.  Auto-refreshes from the persisted
 * snapshot; an on-demand "Re-check now" button triggers a live probe.
 */

import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, interval } from 'rxjs';
import { startWith, switchMap, takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';

import { ApiService } from '../../core/services/api.service';
import { NewsHealth, NewsSourceHealth } from '../../core/models';

@Component({
  selector: 'app-news-health',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatCardModule,
    MatIconModule,
    MatTooltipModule,
    MatButtonModule,
    MatProgressBarModule,
    MatChipsModule,
    MatSnackBarModule,
  ],
  templateUrl: './news-health.component.html',
  styleUrls: ['./news-health.component.scss'],
})
export class NewsHealthComponent implements OnInit, OnDestroy {
  // Snapshot is written by the 6 AM probe / on-demand check; poll infrequently.
  private static readonly REFRESH_MS = 60_000;

  private destroy$ = new Subject<void>();

  health: NewsHealth | null = null;
  loading = true;
  rechecking = false;

  constructor(
    private api: ApiService,
    private snackBar: MatSnackBar,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    interval(NewsHealthComponent.REFRESH_MS)
      .pipe(takeUntil(this.destroy$))
      .pipe(startWith(0), switchMap(() => this.api.getNewsHealth()))
      .subscribe({
        next: (h) => {
          this.health = h;
          this.loading = false;
          this.cdr.markForCheck();
        },
        error: () => {
          this.loading = false;
          this.cdr.markForCheck();
        },
      });
  }

  recheck(): void {
    this.rechecking = true;
    this.cdr.markForCheck();
    this.api.runNewsHealthCheck().subscribe({
      next: (h) => {
        this.health = h;
        this.rechecking = false;
        this.snackBar.open(
          `Re-checked ${h.total_count} sources — ${h.healthy_count} healthy`,
          'OK',
          { duration: 4000 },
        );
        this.cdr.markForCheck();
      },
      error: (err) => {
        this.rechecking = false;
        const msg = err?.error?.detail ?? 'Re-check failed — check server logs';
        this.snackBar.open(msg, 'Dismiss', { duration: 8000, panelClass: ['snack-error'] });
        this.cdr.markForCheck();
      },
    });
  }

  /** Sources sorted worst-first so problems surface at the top. */
  get sortedSources(): NewsSourceHealth[] {
    const rank: Record<string, number> = { DOWN: 0, STALE: 1, OK: 2 };
    return [...(this.health?.sources ?? [])].sort(
      (a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3),
    );
  }

  get hasProblems(): boolean {
    return (this.health?.sources ?? []).some((s) => s.status !== 'OK');
  }

  statusIcon(status: string): string {
    return status === 'OK' ? 'check_circle' : status === 'STALE' ? 'warning' : 'error';
  }

  statusClass(status: string): string {
    return status === 'OK' ? 'ok' : status === 'STALE' ? 'stale' : 'down';
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
