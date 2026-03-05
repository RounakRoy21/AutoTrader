/**
 * SystemAlertsComponent — live feed of system alerts (Telegram-style).
 * Auto-scrolls to newest; capped at 100 entries.
 */

import {
  Component,
  OnInit,
  OnDestroy,
  ViewChild,
  ElementRef,
  AfterViewChecked,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { MatCardModule } from '@angular/material/card';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatBadgeModule } from '@angular/material/badge';

import { StateService } from '../../core/services/state.service';
import { SystemAlert } from '../../core/models';

@Component({
  selector: 'app-system-alerts',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatListModule,
    MatIconModule,
    MatBadgeModule,
  ],
  templateUrl: './system-alerts.component.html',
  styleUrls: ['./system-alerts.component.scss'],
})
export class SystemAlertsComponent implements OnInit, OnDestroy, AfterViewChecked {
  @ViewChild('scrollContainer') private scrollContainer!: ElementRef;

  private destroy$ = new Subject<void>();
  private shouldScroll = false;

  alerts: SystemAlert[] = [];

  constructor(private state: StateService) {}

  ngOnInit(): void {
    this.state.systemAlerts$
      .pipe(takeUntil(this.destroy$))
      .subscribe((a) => {
        this.alerts = a;
        this.shouldScroll = true;
      });
  }

  ngAfterViewChecked(): void {
    if (this.shouldScroll && this.scrollContainer) {
      this.scrollContainer.nativeElement.scrollTop = 0;
      this.shouldScroll = false;
    }
  }

  iconName(type: string): string {
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
        return 'check_circle';
      default:
        return 'info';
    }
  }

  iconClass(type: string): string {
    switch (type) {
      case 'critical':
      case 'danger':
      case 'error':
      case 'SL_HIT':
      case 'ERROR':
        return 'icon-error';
      case 'warning':
      case 'TRADE_HALTED':
      case 'HALT':
        return 'icon-warn';
      case 'success':
      case 'TARGET_HIT':
      case 'TRADE_OPENED':
        return 'icon-success';
      default:
        return 'icon-info';
    }
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
