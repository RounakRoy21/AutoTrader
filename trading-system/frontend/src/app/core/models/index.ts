/**
 * Shared TypeScript interfaces for the AutoTrader dashboard.
 */

export interface ApiResponse<T> {
  success: boolean;
  data: T;
  error: string | null;
  timestamp: string;
}

export interface MarketBrief {
  id: number;
  date: string;
  generated_at: string;
  market_bias: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
  bias_confidence: number;
  sgx_nifty_signal: string | null;
  fii_signal: string | null;
  dxy_signal: string | null;
  us_markets_signal: string | null;
  watchlist: string[];
  avoid_list: string[];
  recommended_stance: string | null;
  raw_json: Record<string, unknown>;
}

export interface Trade {
  id: number;
  kite_order_id: string | null;
  stock: string;
  exchange: string;
  direction: 'BUY' | 'SELL';
  product_type: 'MIS' | 'CNC';
  quantity: number;
  entry_price: number;
  stop_loss_price: number;
  target_price: number;
  exit_price: number | null;
  exit_reason: string | null;
  realized_pnl: number | null;
  status: 'OPEN' | 'CLOSING' | 'CLOSED';
  trade_date: string;
  entry_time: string;
  exit_time: string | null;
  decision_rationale: string | null;
}

export interface DailyPnl {
  id: number;
  date: string;
  starting_capital: number;
  ending_capital: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  return_pct: number;
  trading_halted: boolean;
}

export interface AgentStatus {
  research_agent: {
    status: string;
    step: string;
    last_bias: string | null;
    last_confidence: number | null;
    last_completed: string | null;
  };
  trading_agent: {
    status: string;
    trading_halted: boolean;
    daily_trade_count: number;
    last_signal_stock: string | null;
    last_signal_time: string | null;
  };
  risk_manager: {
    status: string;
    daily_loss: number;
    drawdown_pct: number;
  };
  config?: {
    paper_trading: boolean;
    max_trades_per_day: number;
    max_open_positions: number;
  };
}

export type LtpMap = Record<string, number>;

export interface HealthCheck {
  database: string;
  redis: string;
  kite_api: string;
}

export interface KiteAuthStatus {
  authenticated: boolean;
  message: string;
}

export interface WsEvent {
  channel: string;
  data: Record<string, unknown>;
}

export interface SystemAlert {
  type: string;
  message: string;
  timestamp: string;
}
