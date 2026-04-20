import { Component, OnInit, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ActivatedRoute } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';

/**
 * KiteAuthCallbackComponent — shown after the backend OAuth callback completes.
 * The user is redirected here with a query param status=success|error.
 */
@Component({
  selector: 'app-kite-auth-callback',
  standalone: true,
  imports: [CommonModule, RouterLink, MatCardModule, MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="callback-wrapper">
      <mat-card class="callback-card">
        <mat-card-content>
          <div class="callback-icon" [class.success]="success">
            <mat-icon>{{ success ? 'check_circle' : 'error' }}</mat-icon>
          </div>
          <h2>{{ success ? 'Zerodha Connected' : 'Authentication Failed' }}</h2>
          <p>{{ success
            ? 'Your Kite access token is saved. You can now place live orders.'
            : 'Kite authentication failed. Please try again from the dashboard.' }}</p>
          <button mat-flat-button color="primary" routerLink="/dashboard">
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
    h2 { margin: 0 0 8px; }
    p { color: #666; margin-bottom: 24px; }
  `],
})
export class KiteAuthCallbackComponent implements OnInit {
  success = false;

  constructor(private route: ActivatedRoute) {}

  ngOnInit(): void {
    this.success = this.route.snapshot.queryParamMap.get('status') === 'success';
  }
}
