import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, interval } from 'rxjs';
import { startWith, switchMap, takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { MatTabsModule } from '@angular/material/tabs';
import { MatExpansionModule } from '@angular/material/expansion';

import { ApiService } from '../../core/services/api.service';
import { DecisionEntry } from '../../core/models';
import { TradingWebSocketService } from '../../core/services/trading-websocket.service';

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
    MatExpansionModule,
  ],
  templateUrl: './decision-feed.component.html',
  styleUrls: ['./decision-feed.component.scss'],
})
export class DecisionFeedComponent implements OnInit, OnDestroy {
  // Safety-net sync only; real-time updates come from WebSocket decision_feed.
  private static readonly FALLBACK_SYNC_MS = 300_000;

  private destroy$ = new Subject<void>();

  /** 'page' = full routed view; 'widget' = compact embedded card */
  @Input() mode: 'page' | 'widget' = 'page';

  decisions: DecisionEntry[] = [];
  loading = true;
  lastUpdated: Date | null = null;

  constructor(
    private api: ApiService,
    private ws: TradingWebSocketService,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    // Initial fetch + infrequent reconciliation in case any WS event is missed.
    interval(DecisionFeedComponent.FALLBACK_SYNC_MS)
      .pipe(takeUntil(this.destroy$))
      .pipe(startWith(0), switchMap(() => this.api.getDecisionFeed(50)))
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

    // Real-time decision events via WebSocket push.
    this.ws.decisions$
      .pipe(takeUntil(this.destroy$))
      .subscribe((entry) => {
        const deduped = this.decisions.filter((d) => this._entryKey(d) !== this._entryKey(entry));
        this.decisions = [entry, ...deduped].slice(0, 100);
        this.loading = false;
        this.lastUpdated = new Date();
        this.cdr.markForCheck();
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  private _entryKey(d: DecisionEntry): string {
    return `${d.date}|${d.ts}|${d.stock}|${d.stage}|${d.decision}|${d.rationale}`;
  }

  get traded(): DecisionEntry[] {
    return this.decisions.filter(d => d.decision === 'EXECUTE' || d.decision === 'REDUCE');
  }
  get rejected(): DecisionEntry[] {
    return this.decisions.filter(d => d.decision === 'REJECT');
  }

  get rejectedPreCheck(): DecisionEntry[] {
    return this.rejected.filter((d) => d.stage === 'PRE_CHECK');
  }

  get rejectedLlm(): DecisionEntry[] {
    return this.rejected.filter((d) => d.stage === 'LLM');
  }

  get rejectedPreCheckCount(): number {
    return this.rejectedPreCheck.length;
  }

  get rejectedLlmCount(): number {
    return this.rejectedLlm.length;
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
