import { Component, OnInit, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '../../core/services/api.service';

/**
 * GrowwAuthCallbackComponent — landing page for Groww authentication.
 * Navigating to /auth/groww triggers a login attempt against the backend.
 * Credentials (client_id, password, totp_secret) are read from env vars server-side.
 */
@Component({
  selector: 'app-groww-auth-callback',
  standalone: true,
  imports: [CommonModule, RouterLink, MatCardModule, MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="callback-wrapper">
      <mat-card class="callback-card">
        <mat-card-content>
          <div class="callback-icon" [class.success]="success" [class.error-state]="failed">
            <mat-icon>{{ success ? 'check_circle' : failed ? 'error' : 'lock' }}</mat-icon>
          </div>
          <h2>{{ success ? 'Groww Connected' : failed ? 'Authentication Failed' : 'Groww Authentication' }}</h2>
          <p>{{ success
            ? 'Groww session token saved. You can now place live orders.'
            : failed
            ? 'Login failed — check GROWW_CLIENT_ID, GROWW_PASSWORD and GROWW_TOTP_SECRET in .env.'
            : 'Click the button below to authenticate with Groww using credentials from .env.' }}</p>
          <button mat-flat-button color="primary" (click)="login()" [disabled]="loading" *ngIf="!success">
            {{ loading ? 'Connecting...' : 'Connect to Groww' }}
          </button>
          <button mat-flat-button color="primary" routerLink="/dashboard" *ngIf="success">
            Go to Dashboard
          </button>
        </mat-card-content>
      </mat-card>
    </div>
  `,
  styles: [`
    .callback-wrapper {
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100%;
      padding: 40px;
    }
    .callback-card { max-width: 420px; text-align: center; }
    .callback-icon { font-size: 3rem; margin-bottom: 16px; }
    .callback-icon mat-icon { font-size: 4rem; width: 4rem; height: 4rem; color: #9e9e9e; }
    .callback-icon.success mat-icon { color: #4caf50; }
    .callback-icon.error-state mat-icon { color: #f44336; }
    h2 { margin: 0 0 8px; }
    p { color: #666; margin-bottom: 24px; }
    button { margin: 4px; }
  `],
})
export class GrowwAuthCallbackComponent implements OnInit {
  success = false;
  failed = false;
  loading = false;

  constructor(
    private api: ApiService,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {}

  login(): void {
    this.loading = true;
    this.failed = false;
    this.cdr.markForCheck();
    this.api.growwLogin().subscribe({
      next: () => {
        this.loading = false;
        this.success = true;
        this.cdr.markForCheck();
      },
      error: () => {
        this.loading = false;
        this.failed = true;
        this.cdr.markForCheck();
      },
    });
  }
}
