-- 빅터홍컴퍼니 AI 투자회사 초기 스키마
-- 금액·수량은 Decimal 문자열(TEXT), 시각은 ISO-8601 UTC(TEXT)로 저장한다.

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE settings_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    settings_hash TEXT NOT NULL,
    source_file_hash TEXT
);

CREATE TABLE service_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    pid INTEGER,
    host TEXT,
    mode TEXT NOT NULL,
    code_version TEXT,
    settings_version INTEGER,
    status TEXT NOT NULL,
    stop_reason TEXT
);

-- 영속 제어 플래그(신규 매수 중지, 대사 차단 등). 재시작해도 유지된다.
CREATE TABLE control_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE control_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    scope TEXT,
    detail_json TEXT
);

CREATE TABLE control_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    command TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued / running / done / failed
    result_json TEXT,
    processed_at TEXT
);

CREATE TABLE live_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    account_id TEXT NOT NULL,
    broker TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    settings_version INTEGER NOT NULL,
    confirm_phrase TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    activated_by TEXT NOT NULL,
    deactivated_at TEXT,
    deactivated_by TEXT,
    deactivation_reason TEXT
);

CREATE TABLE live_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    account_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    items_json TEXT NOT NULL
);

CREATE TABLE selftest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    code_version TEXT,
    detail_json TEXT NOT NULL
);

-- LIVE 활성화 시점의 기존 보유분(봇 자산에서 제외). 봇은 이 수량을 매도하지 않는다.
CREATE TABLE account_baselines (
    account_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    qty TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (account_id, asset)
);

CREATE TABLE broker_health (
    account_id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    last_ok_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    auth_ok INTEGER,
    clock_skew_sec REAL,
    detail_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE books (
    book_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- operating / shadow / baseline
    setting TEXT NOT NULL,          -- A / B / C / BUY_HOLD / CASH
    principal_krw TEXT NOT NULL,
    virtual INTEGER NOT NULL,       -- 1이면 가상 원금(실사용 예산과 합산 금지)
    account_id TEXT NOT NULL,       -- 주문이 실행되는 계좌(가상 장부는 paper:<book>)
    created_at TEXT NOT NULL,
    description TEXT
);

CREATE TABLE instruments (
    instrument_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT,
    name TEXT,
    quote_ccy TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    tick_policy TEXT NOT NULL,
    fixed_tick TEXT,
    qty_step TEXT NOT NULL,
    min_notional TEXT NOT NULL,
    max_notional TEXT,
    status TEXT NOT NULL,
    meta_json TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE candles (
    instrument_id TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    value TEXT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, interval, open_time)
);

CREATE TABLE quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    bid TEXT,
    ask TEXT,
    bid_size TEXT,
    ask_size TEXT,
    last TEXT,
    turnover_24h TEXT,
    ts_exchange TEXT,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX idx_quotes_inst_time ON quotes (instrument_id, fetched_at);

CREATE TABLE fx_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    rate TEXT NOT NULL,
    as_of TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT
);
CREATE INDEX idx_fx_pair_time ON fx_rates (pair, fetched_at);

CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    created_at TEXT NOT NULL,
    candle_close_time TEXT,
    data_json TEXT NOT NULL,
    status_json TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

-- 원자료(뉴스·공시·가격사실). AI는 여기 있는 source_id만 근거로 인용할 수 있다.
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    feed TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    market TEXT,
    instruments_json TEXT,
    summary TEXT
);
CREATE INDEX idx_sources_fetched ON sources (fetched_at);

CREATE TABLE cycles (
    cycle_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    snapshot_id TEXT,
    candle_close_time TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_weight TEXT NOT NULL,
    rationale TEXT NOT NULL,
    indicators_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE ai_runs (
    run_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,             -- research / review_independent / review / strategy_review
    market TEXT,
    trigger TEXT,                   -- daily / event / manual / weekly / follow_up
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    snapshot_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,           -- ok / invalid / error / skipped_budget / skipped_disabled / timeout / refusal
    error TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_krw TEXT,
    cost_usd TEXT,
    cost_estimated INTEGER,
    budget_id INTEGER,
    request_id TEXT,
    served_models TEXT,
    input_hash TEXT,
    output_json TEXT,
    stop_reason TEXT
);

CREATE TABLE ai_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    run_id TEXT,
    reserved_krw TEXT NOT NULL,
    actual_krw TEXT,
    status TEXT NOT NULL,           -- reserved / settled / released
    estimated INTEGER NOT NULL DEFAULT 0,
    fx_rate TEXT,
    fx_source TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE INDEX idx_ai_budget_month ON ai_budget (month, status);

CREATE TABLE ai_reports (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    market TEXT NOT NULL,
    snapshot_id TEXT,
    parent_report_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    valid INTEGER NOT NULL,
    validation_errors TEXT,
    report_json TEXT NOT NULL
);

CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    market TEXT NOT NULL,
    action TEXT NOT NULL,
    target_weight TEXT NOT NULL,
    target_notional_krw TEXT,
    current_notional_krw TEXT,
    rationale TEXT NOT NULL,
    sources_json TEXT,
    counterarguments_json TEXT,
    invalidation TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,           -- proposed / adopted / rejected / expired
    decision_reason TEXT,
    ai_report_id TEXT,
    settings_version INTEGER,
    code_version TEXT,
    prompt_version TEXT
);
CREATE INDEX idx_proposals_book ON proposals (book_id, created_at);

CREATE TABLE intents (
    intent_id TEXT PRIMARY KEY,
    cycle_id TEXT,
    book_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    ref_price TEXT NOT NULL,
    notional_krw TEXT NOT NULL,
    risk_increasing INTEGER NOT NULL,
    purpose TEXT NOT NULL,          -- rebalance / liquidation / baseline
    status TEXT NOT NULL,           -- approved / rejected / ordered / crossed
    risk_reasons_json TEXT,
    allocations_json TEXT NOT NULL,
    proposal_ids_json TEXT,
    created_at TEXT NOT NULL,
    order_id TEXT
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,      -- = client_order_id (멱등 키)
    book_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    broker TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    qty TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_increasing INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    intent_id TEXT,
    broker_order_id TEXT,
    broker_meta_json TEXT,
    filled_qty TEXT NOT NULL DEFAULT '0',
    filled_amount TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    submit_attempted_at TEXT,
    submitted_at TEXT,
    ttl_expires_at TEXT,
    cancel_requested_at TEXT,
    unknown_since TEXT,
    lookup_attempts INTEGER NOT NULL DEFAULT 0,
    last_update_at TEXT NOT NULL,
    last_error TEXT,
    reprice_count INTEGER NOT NULL DEFAULT 0,
    parent_order_id TEXT
);
CREATE INDEX idx_orders_status ON orders (status);
CREATE INDEX idx_orders_book ON orders (book_id, created_at);
CREATE UNIQUE INDEX idx_orders_broker_id ON orders (account_id, broker_order_id) WHERE broker_order_id IS NOT NULL;

CREATE TABLE order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX idx_order_events_order ON order_events (order_id, id);

CREATE TABLE order_allocations (
    order_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    requested_qty TEXT NOT NULL,
    PRIMARY KEY (order_id, strategy_id)
);

CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    fill_key TEXT NOT NULL,         -- 거래소 체결 ID 또는 누적수량 기반 키(중복 방지)
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    fee_estimated INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE (order_id, fill_key)
);

-- 원자적 자금/수량 예약. 동시 전략이 같은 현금을 중복 사용하지 못하게 한다.
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    book_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- cash / qty
    asset TEXT NOT NULL,            -- KRW / USD / instrument_id
    amount_initial TEXT NOT NULL,
    amount_remaining TEXT NOT NULL,
    status TEXT NOT NULL,           -- active / released
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_reservations_active ON reservations (book_id, status);

-- 원장: 모든 현금·수량 변화는 여기 기록되고, positions는 이로부터 계산된 파생 상태다.
CREATE TABLE ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,             -- principal / fill / fee / internal_transfer / adjustment
    strategy_id TEXT NOT NULL,
    asset TEXT NOT NULL,            -- KRW / USD / instrument_id
    delta TEXT NOT NULL,
    price TEXT,
    ref_type TEXT,
    ref_id TEXT,
    note TEXT
);
CREATE INDEX idx_ledger_book ON ledger_entries (book_id, asset);

CREATE TABLE positions (
    book_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    qty TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    opened_at TEXT,
    bars_held INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book_id, strategy_id, instrument_id)
);

CREATE TABLE equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    cash_krw TEXT NOT NULL,
    positions_krw TEXT NOT NULL,
    reserved_krw TEXT NOT NULL,
    equity_krw TEXT NOT NULL,
    realized_krw TEXT NOT NULL,
    unrealized_krw TEXT NOT NULL,
    fees_krw TEXT NOT NULL,
    ai_cost_krw TEXT NOT NULL,
    exposure_json TEXT,
    stale INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_equity_book_ts ON equity_snapshots (book_id, ts);

CREATE TABLE risk_state (
    book_id TEXT PRIMARY KEY,
    day_kst TEXT NOT NULL,
    day_start_equity TEXT NOT NULL,
    peak_equity TEXT NOT NULL,
    peak_at TEXT NOT NULL,
    daily_stop_at TEXT,
    drawdown_stop_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    mismatches_json TEXT,
    detail_json TEXT
);

CREATE TABLE incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    market TEXT,
    account_id TEXT,
    book_id TEXT,
    detail TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX idx_incidents_ts ON incidents (ts);

CREATE TABLE strategy_candidates (
    candidate_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,           -- ai_weekly / user
    strategy_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    base_params_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    ai_report_id TEXT,
    status TEXT NOT NULL,           -- proposed / backtested / promoted / rejected / rolled_back
    backtest_json TEXT,
    promoted_at TEXT,
    promoted_settings_version INTEGER,
    previous_settings_version INTEGER
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    channels TEXT NOT NULL,
    status TEXT NOT NULL
);

-- 내부 모의체결 '거래소' 측 상태(모의 계좌의 주문·잔고). 실제 거래소와 무관.
CREATE TABLE paper_orders (
    broker_order_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    qty TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    filled_amount TEXT NOT NULL,
    fees TEXT NOT NULL,
    status TEXT NOT NULL,           -- wait / done / cancel
    created_at TEXT NOT NULL,
    last_match_quote_id INTEGER,
    trades_json TEXT NOT NULL,
    UNIQUE (account_id, client_order_id)
);

CREATE TABLE paper_balances (
    account_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    free TEXT NOT NULL,
    locked TEXT NOT NULL,
    PRIMARY KEY (account_id, asset)
);
