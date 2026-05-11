import { Injectable, Renderer2, RendererFactory2 } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  private renderer: Renderer2;
  private _dark$ = new BehaviorSubject<boolean>(false);

  readonly dark$: Observable<boolean> = this._dark$.asObservable();

  get isDark(): boolean {
    return this._dark$.value;
  }

  constructor(rendererFactory: RendererFactory2) {
    this.renderer = rendererFactory.createRenderer(null, null);
    const saved = localStorage.getItem('at-theme');
    // Default to dark; only go light when the user has explicitly saved 'light'.
    const isDark = saved !== 'light';
    this._apply(isDark);
  }

  toggle(): void {
    this._apply(!this.isDark);
  }

  private _apply(dark: boolean): void {
    if (dark) {
      this.renderer.addClass(document.body, 'dark-theme');
    } else {
      this.renderer.removeClass(document.body, 'dark-theme');
    }
    this._dark$.next(dark);
    localStorage.setItem('at-theme', dark ? 'dark' : 'light');
  }
}
