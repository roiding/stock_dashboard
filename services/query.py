"""通用查询封装 — 供 views / api 调用."""

from datetime import date
from typing import Optional

from sqlalchemy import func

from models import db, ModelRegistry, Signal, Trade, CircuitBreakerLog, DailyNav


def get_active_models():
    return ModelRegistry.query.filter_by(is_active=True).all()


def get_latest_signals(model_name: str, signal_date: Optional[date] = None):
    q = Signal.query.filter_by(model_name=model_name)
    if signal_date:
        q = q.filter_by(signal_date=signal_date)
    else:
        # 最新一天
        sub = db.session.query(func.max(Signal.signal_date)).filter_by(model_name=model_name).scalar()
        if sub is None:
            return []
        q = q.filter_by(signal_date=sub)
    return q.order_by(Signal.rank).all()


def get_portfolio(model_name: str):
    """当前持仓 (sell_date IS NULL)."""
    return Trade.query.filter_by(model_name=model_name, sell_date=None).all()


def get_latest_cb_status(model_name: str):
    return (
        CircuitBreakerLog.query
        .filter_by(model_name=model_name)
        .order_by(CircuitBreakerLog.check_date.desc())
        .first()
    )


def get_trade_history(
    model_name: Optional[str] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    min_pnl: Optional[float] = None,
    max_pnl: Optional[float] = None,
    page: int = 1,
    per_page: int = 50,
):
    q = Trade.query
    if model_name:
        q = q.filter_by(model_name=model_name)
    if start:
        q = q.filter(Trade.buy_date >= start)
    if end:
        q = q.filter(Trade.buy_date <= end)
    if min_pnl is not None:
        q = q.filter(Trade.pnl >= min_pnl)
    if max_pnl is not None:
        q = q.filter(Trade.pnl <= max_pnl)
    return q.order_by(Trade.buy_date.desc()).paginate(page=page, per_page=per_page, error_out=False)


def get_trade_stats(model_name: Optional[str] = None):
    """返回 {total, wins, win_rate, avg_pnl, max_loss}."""
    q = Trade.query.filter(Trade.sell_date.isnot(None))
    if model_name:
        q = q.filter_by(model_name=model_name)
    trades = q.all()
    if not trades:
        return {'total': 0, 'wins': 0, 'win_rate': 0, 'avg_pnl': 0, 'max_loss': 0}
    total = len(trades)
    wins = sum(1 for t in trades if (t.pnl or 0) > 0)
    pnls = [t.pnl or 0 for t in trades]
    return {
        'total': total,
        'wins': wins,
        'win_rate': round(wins / total * 100, 1) if total else 0,
        'avg_pnl': round(sum(pnls) / total * 100, 2) if total else 0,
        'max_loss': round(min(pnls) * 100, 2) if pnls else 0,
    }


def calc_recent_win_rate(model_name: str, window: int = 15):
    """最近 window 笔已平仓交易的胜率, 返回 (win_rate, sample_size).

    win_rate 为百分比 (如 46.7), 不足 window 笔时用实际笔数.
    无已平仓交易时返回 (None, 0).
    """
    trades = (
        Trade.query
        .filter_by(model_name=model_name)
        .filter(Trade.sell_date.isnot(None))
        .order_by(Trade.sell_date.desc(), Trade.buy_date.desc())
        .limit(window)
        .all()
    )
    if not trades:
        return (None, 0)
    wins = sum(1 for t in trades if (t.pnl or 0) > 0)
    sample = len(trades)
    return (round(wins / sample * 100, 1), sample)


def calc_rolling_win_rate(model_name: str, window: int = 15):
    """滚动窗口胜率, 返回 [{date, win_rate, sample}, ...]."""
    trades = (
        Trade.query
        .filter_by(model_name=model_name)
        .filter(Trade.sell_date.isnot(None))
        .order_by(Trade.sell_date, Trade.buy_date)
        .all()
    )
    if len(trades) < window:
        return []
    result = []
    for i in range(window, len(trades) + 1):
        chunk = trades[i - window:i]
        wins = sum(1 for t in chunk if (t.pnl or 0) > 0)
        result.append({
            'date': str(chunk[-1].sell_date),
            'win_rate': round(wins / window * 100, 1),
            'sample': window,
        })
    return result


def _get_position_weight(model_name: Optional[str]) -> float:
    """单笔仓位权重 = 1 / (daily_picks * hold_days). 如 3选10天持有 = 3.3%."""
    if model_name:
        m = ModelRegistry.query.filter_by(name=model_name).first()
        if m and m.daily_picks and m.hold_days:
            return 1.0 / (m.daily_picks * m.hold_days)
    return 1.0 / (3 * 10)  # 默认 3.3%


def calc_nav_curve(model_name: Optional[str] = None):
    """净值曲线: 从 daily_nav 表读取 (由脚本推送, mark-to-market)."""
    q = DailyNav.query
    if model_name:
        q = q.filter_by(model_name=model_name)
    rows = q.order_by(DailyNav.date).all()
    return [r.to_dict() for r in rows]


def calc_monthly_returns(model_name: Optional[str] = None):
    """月度收益汇总, 按 1/daily_picks 仓位. 返回 [{month, return_pct, count, wins}]."""
    q = Trade.query.filter(Trade.sell_date.isnot(None), Trade.pnl.isnot(None))
    if model_name:
        q = q.filter_by(model_name=model_name)
    trades = q.order_by(Trade.sell_date, Trade.buy_date).all()
    if not trades:
        return []
    weight = _get_position_weight(model_name)
    months = {}
    for t in trades:
        key = t.sell_date.strftime('%Y-%m')
        if key not in months:
            months[key] = {'pnls': [], 'wins': 0}
        months[key]['pnls'].append(t.pnl)
        if t.pnl > 0:
            months[key]['wins'] += 1
    result = []
    for month, data in sorted(months.items()):
        nav = 1.0
        for p in data['pnls']:
            nav *= (1 + p * weight)
        result.append({
            'month': month,
            'return_pct': round((nav - 1) * 100, 2),
            'count': len(data['pnls']),
            'wins': data['wins'],
        })
    return result
