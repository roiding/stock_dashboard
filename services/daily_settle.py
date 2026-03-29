"""每日结算 — 系统级定时任务, 收盘后对所有 active 模型执行:

1. T+1 买入: 前一交易日信号 → 今日开盘价买入 (熔断中跳过)
2. 止盈/到期: 检查持仓 → 满足条件则卖出
3. 逐日净值: mark-to-market, 写入 daily_nav 表

熔断判定 + 清仓由 circuit_breaker.py 在盘中(~14:50)执行, 本模块仅读取状态.

由 APScheduler 在每个交易日 15:10 自动触发, 也可通过 POST /api/settle 手动触发.
"""

import logging
from datetime import date

from models import db, ModelRegistry, Signal, Trade, CircuitBreakerLog, DailyNav
from services.market_data import get_realtime_quotes, get_recent_trading_days

log = logging.getLogger('daily_settle')


def run_daily_settle():
    """入口: 对所有 active 模型执行每日结算."""
    today = date.today()
    trading_days = get_recent_trading_days(120)

    if not trading_days:
        log.warning('无法获取交易日历, 跳过结算')
        return

    if today not in trading_days:
        log.info(f'{today} 不是交易日, 跳过结算')
        return

    models = ModelRegistry.query.filter_by(is_active=True).all()
    log.info(f'每日结算开始: {len(models)} 个模型, 日期 {today}')

    results = []
    for model in models:
        try:
            r = _settle_model(model, today, trading_days)
            results.append(r)
            log.info(f'  {model.name}: 买{r["bought"]} 卖{r["sold"]} '
                     f'NAV={r["nav"]:.4f} 持仓{r["open_positions"]}')
        except Exception as e:
            log.error(f'  {model.name}: 结算失败 - {e}')
            db.session.rollback()
            results.append({'model': model.name, 'error': str(e)})

    log.info('每日结算完成')
    return results


def _settle_model(model, today, trading_days):
    """单个模型的每日结算, 返回结果摘要."""
    # 读取熔断状态
    cb_status = _get_current_cb_status(model)       # 今天的 (用于 NAV 展示)
    cb_prev = _get_previous_cb_status(model, today)  # 昨天的 (用于买入判断)

    # 0. 熔断 T+1 清仓: 昨天 CB 触发时因 T+1 无法卖的实仓, 今天以开盘价清掉
    cb_sold = 0
    if cb_prev == 'circuit_break':
        cb_sold = _force_close_t1(model, today)
        if cb_sold > 0:
            log.info(f'  {model.name}: T+1 熔断清仓 {cb_sold} 笔')

    # 1. 买入 (早盘已发生, 参考昨天的 CB 状态)
    virtual = (cb_prev == 'circuit_break')
    bought = _execute_buys(model, today, trading_days, virtual=virtual)
    if virtual and bought > 0:
        log.info(f'  {model.name}: 熔断中, {bought} 笔虚拟买入')

    # 2. 止盈/到期卖出
    sold = _check_sells(model, today, trading_days)

    # 3. 净值
    nav, n_open = _compute_daily_nav(model, today)

    return {
        'model': model.name,
        'bought': bought,
        'sold': sold,
        'cb_status': cb_status,
        'nav': nav,
        'open_positions': n_open,
    }


def _get_current_cb_status(model):
    """读取最新熔断状态 (来自 circuit_breaker_log 表)."""
    latest = (
        CircuitBreakerLog.query
        .filter_by(model_name=model.name)
        .order_by(CircuitBreakerLog.check_date.desc())
        .first()
    )
    return latest.status if latest else 'normal'


def _get_previous_cb_status(model, today):
    """读取前一个交易日的熔断状态.

    买入发生在早盘, CB 在盘中(~14:50)评估, 所以买入的 is_virtual
    应参考昨天的 CB 状态, 而非今天刚推送的.
    """
    prev = (
        CircuitBreakerLog.query
        .filter_by(model_name=model.name)
        .filter(CircuitBreakerLog.check_date < today)
        .order_by(CircuitBreakerLog.check_date.desc())
        .first()
    )
    return prev.status if prev else 'normal'


# ================================================================
# 1. 买入
# ================================================================

def _execute_buys(model, today, trading_days, virtual=False):
    """T+1 买入: 前一交易日的信号 → 今日开盘价买入.

    规则:
    - 信号日 = today 的前一个交易日
    - 按 rank 取 daily_picks 只
    - 过滤: pred >= threshold, 开盘未涨停
    """
    today_idx = trading_days.index(today)
    if today_idx == 0:
        return 0
    signal_date = trading_days[today_idx - 1]

    # 检查是否已有该信号日的买入
    existing = Trade.query.filter_by(
        model_name=model.name, signal_date=signal_date,
    ).count()
    if existing > 0:
        return 0

    signals = (
        Signal.query
        .filter_by(model_name=model.name, signal_date=signal_date)
        .order_by(Signal.rank)
        .all()
    )
    if not signals:
        return 0

    daily_picks = model.daily_picks or 3
    threshold = model.pred_threshold or 2.0

    codes = [s.code for s in signals]
    quotes = get_realtime_quotes(codes)

    bought = 0
    for sig in signals:
        if bought >= daily_picks:
            break
        if (sig.pred or 0) < threshold:
            break

        q = quotes.get(sig.code)
        if not q or not q.get('open') or q['open'] <= 0:
            continue

        # 涨停检查: 开盘价相对信号日收盘涨幅 >= 9.8%
        last_close = sig.close or q.get('last_close', 0)
        if last_close > 0 and (q['open'] / last_close - 1) >= 0.098:
            continue

        trade = Trade(
            model_name=model.name,
            code=sig.code,
            signal_date=signal_date,
            buy_date=today,
            buy_price=round(q['open'], 2),
            is_virtual=virtual,
        )
        db.session.add(trade)
        bought += 1

    if bought > 0:
        db.session.commit()
    return bought


# ================================================================
# 1b. 熔断 T+1 遗留清仓
# ================================================================

def _force_close_t1(model, today):
    """昨天 CB 触发时因 T+1 无法卖出的实仓, 今天以开盘价清掉.

    昨天 circuit_breaker 已清仓所有可卖实仓, 但当天买入的受 T+1 保护.
    这些遗留仓位今天以开盘价强制卖出.
    """
    open_trades = Trade.query.filter_by(
        model_name=model.name, sell_date=None, is_virtual=False,
    ).all()
    if not open_trades:
        return 0

    codes = list(set(t.code for t in open_trades))
    quotes = get_realtime_quotes(codes)

    sold = 0
    for trade in open_trades:
        q = quotes.get(trade.code)
        if not q or not q.get('open') or q['open'] <= 0:
            continue
        open_price = q['open']
        trade.sell_date = today
        trade.sell_price = round(open_price, 2)
        trade.sell_reason = 'circuit_breaker'
        trade.pnl = round(open_price / trade.buy_price - 1, 6)
        sold += 1

    if sold > 0:
        db.session.commit()
    return sold


# ================================================================
# 2. 卖出 (止盈 / 到期)
# ================================================================

def _check_sells(model, today, trading_days):
    """检查所有持仓: 止盈或到期则卖出.

    规则:
    - TP: 当日最高价 >= buy_price * (1 + tp_pct) → 以止盈价卖出
    - 到期: 信号日起经过 hold_days 个交易日 → 以收盘价卖出
    - T+1: 买入日不能卖
    - TP 优先于到期
    """
    open_trades = Trade.query.filter_by(
        model_name=model.name, sell_date=None,
    ).all()
    if not open_trades:
        return 0

    hold_days = model.hold_days or 10
    tp_pct = (model.tp_pct or 10.0) / 100.0
    today_idx = trading_days.index(today)

    codes = list(set(t.code for t in open_trades))
    quotes = get_realtime_quotes(codes)

    sold = 0
    for trade in open_trades:
        # T+1: 买入日不能卖
        if trade.buy_date == today:
            continue

        q = quotes.get(trade.code)
        if not q:
            continue

        tp_price = trade.buy_price * (1 + tp_pct)

        # 计算是否到期: signal_date + hold_days 个交易日
        try:
            sig_idx = trading_days.index(trade.signal_date)
            should_expire = (today_idx - sig_idx) >= hold_days
        except ValueError:
            should_expire = True  # signal_date 超出范围, 视为到期

        # TP 检查 (优先)
        if q.get('high', 0) >= tp_price:
            trade.sell_date = today
            trade.sell_price = round(tp_price, 2)
            trade.sell_reason = 'tp'
            trade.pnl = round(tp_price / trade.buy_price - 1, 6)
            sold += 1
        elif should_expire:
            close_price = q.get('price') or trade.buy_price
            trade.sell_date = today
            trade.sell_price = round(close_price, 2)
            trade.sell_reason = 'expire'
            trade.pnl = round(close_price / trade.buy_price - 1, 6)
            sold += 1

    if sold > 0:
        db.session.commit()
    return sold


# ================================================================
# 3. 净值
# ================================================================

def _compute_daily_nav(model, today):
    """计算当日净值 (mark-to-market) 并写入 daily_nav 表.

    NAV = 1.0 + 累计已实现收益 + 当前浮动收益
    weight = 1 / (daily_picks * hold_days)

    Returns (nav, n_open_positions)
    """
    daily_picks = model.daily_picks or 3
    hold_days_val = model.hold_days or 10
    weight = 1.0 / (daily_picks * hold_days_val)

    # 已实现: 所有已平仓的实际交易 (排除虚拟)
    closed_trades = (
        Trade.query
        .filter_by(model_name=model.name, is_virtual=False)
        .filter(Trade.sell_date.isnot(None))
        .all()
    )
    realized_pnl = sum((t.pnl or 0) * weight for t in closed_trades)

    # 浮动: 所有未平仓的实际交易 (排除虚拟)
    open_trades = Trade.query.filter_by(
        model_name=model.name, sell_date=None, is_virtual=False,
    ).all()
    n_open = len(open_trades)

    floating_pnl = 0.0
    if open_trades:
        codes = list(set(t.code for t in open_trades))
        quotes = get_realtime_quotes(codes)
        for t in open_trades:
            q = quotes.get(t.code)
            cur_price = q['price'] if q and q.get('price') else t.buy_price
            floating_pnl += (cur_price / t.buy_price - 1) * weight

    nav = 1.0 + realized_pnl + floating_pnl

    # 日收益率
    prev = (
        DailyNav.query
        .filter_by(model_name=model.name)
        .filter(DailyNav.date < today)
        .order_by(DailyNav.date.desc())
        .first()
    )
    prev_nav = prev.nav if prev else 1.0
    daily_return = (nav / prev_nav - 1) if prev_nav else 0

    # Upsert
    existing = DailyNav.query.filter_by(model_name=model.name, date=today).first()
    if existing:
        existing.nav = round(nav, 6)
        existing.open_positions = n_open
        existing.daily_return = round(daily_return, 6)
    else:
        db.session.add(DailyNav(
            model_name=model.name, date=today,
            nav=round(nav, 6),
            open_positions=n_open,
            daily_return=round(daily_return, 6),
        ))
    db.session.commit()
    return nav, n_open
