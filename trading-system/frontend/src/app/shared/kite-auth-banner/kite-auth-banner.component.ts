import { Component, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '../../core/services/api.service';

/**
 * KiteAuthBannerComponent — shows a dismissible alert bar when the Kite Connect
 * token is absent. Provides a one-click "Connect to Zerodha" button that fetches
 * the OAuth login URL and navigates the browser to it.
 *
 * The banner auto-hides when the user is already authenticated.
 */
@Component({
  selector: 'app-kite-auth-banner',
  standalone: true,
  imports: [CommonModule, MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="kite-banner" *ngIf="visible">
      <mat-icon class="banner-icon">warning</mat-icon>
      <span>Zerodha Kite token missing — orders will not be placed until you connect.</span>
      <button mat-flat-button color="accent" class="connect-btn" (click)="connect()" [disabled]="connecting">
        {{ connecting ? 'Opening...' : 'Connect to Zerodha' }}
      </button>
      <button mat-icon-button class="dismiss-btn" (click)="dismiss()" aria-label="Dismiss">
        <mat-icon>close</mat-icon>
      </button>
    </div>
  `,
  styles: [`
    .kite-banner {
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
export class KiteAuthBannerComponent implements OnDestroy {
  visible = true;
  connecting = false;

  private destroy$ = new Subject<void>();

  constructor(
    private api: ApiService,
    private cdr: ChangeDetectorRef,
  ) {}

  connect(): void {
    this.connecting = true;
    this.cdr.markForCheck();
    this.api.getKiteLoginUrl().pipe(takeUntil(this.destroy$)).subscribe({
      next: ({ login_url }) => {
        this.connecting = false;
        this.cdr.markForCheck();
        window.location.href = login_url;
      },
      error: () => {
        this.connecting = false;
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
