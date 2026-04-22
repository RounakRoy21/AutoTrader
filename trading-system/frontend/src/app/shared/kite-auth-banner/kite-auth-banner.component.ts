import { Component, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '../../core/services/api.service';

/**
 * GrowwAuthBannerComponent — shows a dismissible alert bar when the Groww
 * session token is absent. Provides a one-click "Connect to Groww" button
 * that POSTs to the backend login endpoint (credentials read from env vars).
 *
 * The banner auto-hides when the user is already authenticated.
 */
@Component({
  selector: 'app-groww-auth-banner',
  standalone: true,
  imports: [CommonModule, MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="groww-banner" *ngIf="visible">
      <mat-icon class="banner-icon">warning</mat-icon>
      <span>{{ error ? 'Groww login failed — check GROWW_* env vars and retry.' : 'Groww token missing — orders will not be placed until you connect.' }}</span>
      <button mat-flat-button color="accent" class="connect-btn" (click)="connect()" [disabled]="connecting">
        {{ connecting ? 'Connecting...' : 'Connect to Groww' }}
      </button>
      <button mat-icon-button class="dismiss-btn" (click)="dismiss()" aria-label="Dismiss">
        <mat-icon>close</mat-icon>
      </button>
    </div>
  `,
  styles: [`
    .groww-banner {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 20px;
      background: #fff3e0;
      border-bottom: 2px solid #ff9800;
      font-size: 0.9rem;
      color: #e65100;
    }
    .banner-icon { color: #ff9800; flex-shrink: 0; }
    .connect-btn { margin-left: auto; flex-shrink: 0; }
    .dismiss-btn { flex-shrink: 0; color: #999; }
  `],
})
export class GrowwAuthBannerComponent implements OnDestroy {
  visible = true;
  connecting = false;
  error = false;

  private destroy$ = new Subject<void>();

  constructor(
    private api: ApiService,
    private cdr: ChangeDetectorRef,
  ) {}

  connect(): void {
    this.connecting = true;
    this.error = false;
    this.cdr.markForCheck();
    this.api.growwLogin().pipe(takeUntil(this.destroy$)).subscribe({
      next: () => {
        this.connecting = false;
        this.visible = false;
        this.cdr.markForCheck();
      },
      error: () => {
        this.connecting = false;
        this.error = true;
        this.cdr.markForCheck();
      },
    });
  }

  dismiss(): void {
    this.visible = false;
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}
