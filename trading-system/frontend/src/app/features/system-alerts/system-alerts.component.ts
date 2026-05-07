/**
 * SystemAlertsComponent — live feed of system alerts (Telegram-style).
 * Auto-scrolls to newest; capped at 100 entries.
 * Level filter toggles: info / warning / error+critical / success.
 */

import {
  Component,
  OnInit,
  OnDestroy,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatBadgeModule } from '@angular/material/badge';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatButtonToggleModule } from '@angular/material/button-toggle';

import { StateService } from '../../core/services/state.service';
import { SystemAlert } from '../../core/models';

/** Normalise the many backend type strings into four UI-level buckets. */
function alertLevel(type: string): 'info' | 'warning' | 'error' | 'success' {
  switch (type) {
    case 'critical':
    case 'danger':
    case 'error':
    case 'SL_HIT':
    case 'ERROR':
      return 'error';
    case 'warning':
    case 'TRADE_HALTED':
    case 'HALT':
      return 'warning';
    case 'success':
    case 'TARGET_HIT':
    case 'TRADE_OPENED':
      return 'success';
    default:
      return 'info';
  }
}

@Component({
  selector: 'app-system-alerts',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    MatCardModule,
    MatListModule,
    MatIconModule,
    MatBadgeModule,
    MatProgressBarModule,
    MatButtonToggleModule,
  ],
  templateUrl: './system-alerts.component.html',
  styleUrls: ['./system-alerts.component.scss'],
})
export class SystemAlertsComponent implements OnInit, OnDestroy, AfterViewChecked {
  @ViewChild('scrollContainer') private scrollContainer!: ElementRef;

  private destroy$ = new Subject<void>();
  private shouldScroll = false;

  alerts: SystemAlert[] = [];
  loading = true;

  /** Which levels are currently visible (all on by default). */
  activeLevels = new Set<string>(['info', 'warning', 'error', 'success']);

  /** Count of alerts per level for the badge numbers. */
  levelCounts: Record<string, number> = { info: 0, warning: 0, error: 0, success: 0 };

  constructor(private state: StateService, private cdr: ChangeDetectorRef) {}

  ngOnInit(): void {
    this.state.systemAlerts$
      .pipe(takeUntil(this.destroy$))
      .subscribe((a) => {
        this.alerts = a;
        this.loading = false;
        this.shouldScroll = true;
        this._recount();
        this.cdr.markForCheck();
      });
  }

  ngAfterViewChecked(): void {
    if (this.shouldScroll && this.scrollContainer) {
      this.scrollContainer.nativeElement.scrollTop = 0;
      this.shouldScroll = false;
    }
  }

  /** Alerts filtered by the active level toggles. */
  get filteredAlerts(): SystemAlert[] {
    return this.alerts.filter((a) => this.activeLevels.has(alertLevel(a.type)));
  }

  /** Toggle a level on/off. */
  toggleLevel(level: string): void {
    if (this.activeLevels.has(level)) {
      this.activeLevels.delete(level);
    } else {
      this.activeLevels.add(level);
    }
    this.cdr.markForCheck();
  }

  isLevelActive(level: string): boolean {
    return this.activeLevels.has(level);
  }

  iconName(type: string): string {
    switch (alertLevel(type)) {
      case 'error':
        return 'error';
      case 'warning':
        return 'warning';
      case 'success':
        return 'check_circle';
      default:
        return 'info';
    }
  }

  iconClass(type: string): string {
    return `icon-${alertLevel(type)}`;
  }

  private _recount(): void {
    const c: Record<string, number> = { info: 0, warning: 0, error: 0, success: 0 };
    for (const a of this.alerts) {
      c[alertLevel(a.type)]++;
    }
    this.levelCounts = c;
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
