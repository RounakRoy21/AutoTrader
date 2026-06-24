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
  profit_factor: number | null;
  sharpe_ratio: number | null;
  avg_trade_duration_min: number | null;
  max_consecutive_losses: number | null;
  avg_realised_rr: number | null;
  losses_before_1030: number | null;
  losses_1030_to_1330: number | null;
  losses_after_1330: number | null;
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
  anthropic?: {
    calls_research_today: number;
    calls_decision_today: number;
    calls_total_today: number;
  };
  market_status?: {
    status: 'OPEN' | 'PRE_OPEN' | 'CLOSED' | 'HOLIDAY' | 'WEEKEND';
    is_open: boolean;
    label: string;
  };
  data_api?: {
    status: 'OK' | 'DEGRADED' | 'FORBIDDEN' | 'UNKNOWN';
    detail: string | null;
  };
  scanner_warmup?: {
    complete: boolean;
    pct: number;
    elapsed_min: number;
    remaining_min: number;
    required_candles: number;
  };
  config?: {
    paper_trading: boolean;
    max_trades_per_day: number;
    max_open_positions: number;
    daily_drawdown_limit_pct: number;
    daily_drawdown_limit: number;
  };
}

export type LtpMap = Record<string, number>;

export interface DecisionEntry {
  ts: string;           // HH:MM:SS
  date: string;         // YYYY-MM-DD
  stock: string;
  ltp: number;
  rsi: number;
  volume_ratio: number;
  vwap: number | null;
  stage: 'PRE_CHECK' | 'LLM';
  decision: 'EXECUTE' | 'REDUCE' | 'REJECT';
  confidence: number | null;
  rationale: string;
  qty: number | null;
  sl: number | null;
  target: number | null;
}

export interface HealthCheck {
  database: string;
  redis: string;
  groww_api: string;
  market_data?: string;
}

export interface GrowwAuthStatus {
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
