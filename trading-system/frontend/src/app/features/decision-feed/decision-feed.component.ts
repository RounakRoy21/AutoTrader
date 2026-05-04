import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { interval, Subject } from 'rxjs';
import { startWith, switchMap, takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { MatTabsModule } from '@angular/material/tabs';

import { ApiService } from '../../core/services/api.service';
import { DecisionEntry } from '../../core/models';

@Component({
  selector: 'app-decision-feed',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatCardModule,
    MatIconModule,
    MatTooltipModule,
    MatProgressSpinnerModule,
    MatChipsModule,
    MatTabsModule,
  ],
  templateUrl: './decision-feed.component.html',
  styleUrls: ['./decision-feed.component.scss'],
})
export class DecisionFeedComponent implements OnInit, OnDestroy {
  private destroy$ = new Subject<void>();

  decisions: DecisionEntry[] = [];
  loading = true;
  lastUpdated: Date | null = null;

  constructor(private api: ApiService, private cdr: ChangeDetectorRef) {}

  ngOnInit(): void {
    // Poll every 5 seconds for new decisions
    interval(5000)
      .pipe(
        startWith(0),
        switchMap(() => this.api.getDecisionFeed(50)),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (entries) => {
          this.decisions = entries;
          this.loading = false;
          this.lastUpdated = new Date();
          this.cdr.markForCheck();
        },
        error: () => {
          this.loading = false;
          this.cdr.markForCheck();
        },
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get traded(): DecisionEntry[] {
    return this.decisions.filter(d => d.decision === 'EXECUTE' || d.decision === 'REDUCE');
  }
  get rejected(): DecisionEntry[] {
    return this.decisions.filter(d => d.decision === 'REJECT');
  }
  get rejectedPreCheckCount(): number {
    return this.decisions.filter(d => d.decision === 'REJECT' && d.stage === 'PRE_CHECK').length;
  }
  get rejectedLlmCount(): number {
    return this.decisions.filter(d => d.decision === 'REJECT' && d.stage === 'LLM').length;
  }

  decisionClass(d: DecisionEntry): string {
    if (d.decision === 'EXECUTE') return 'decision-execute';
    if (d.decision === 'REDUCE') return 'decision-reduce';
    return 'decision-reject';
  }

  decisionIcon(d: DecisionEntry): string {
    if (d.decision === 'EXECUTE') return 'check_circle';
    if (d.decision === 'REDUCE') return 'remove_circle';
    return 'cancel';
  }

  stageLabel(d: DecisionEntry): string {
    return d.stage === 'PRE_CHECK' ? 'Pre-check' : 'LLM';
  }

  rrRatio(d: DecisionEntry): string | null {
    if (d.sl == null || d.target == null || d.ltp === 0) return null;
    const slDist = Math.abs(d.ltp - d.sl);
    const tgtDist = Math.abs(d.target - d.ltp);
    if (slDist === 0) return null;
    return (tgtDist / slDist).toFixed(1);
  }
}
