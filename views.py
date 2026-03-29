"""页面路由 Blueprint — 4 个页面."""

from datetime import date, datetime

from flask import Blueprint, render_template, request

from models import ModelRegistry
from services.query import (
    get_active_models, get_latest_signals, get_portfolio,
    get_latest_cb_status, get_trade_history, get_trade_stats,
    calc_recent_win_rate, calc_rolling_win_rate,
    calc_nav_curve, calc_monthly_returns,
)

views_bp = Blueprint('views', __name__)


@views_bp.route('/')
def overview():
    """总览页: 所有模型卡片."""
    models = get_active_models()
    cards = []
    for m in models:
        signals = get_latest_signals(m.name)
        portfolio = get_portfolio(m.name)
        cb = get_latest_cb_status(m.name)
        cb_win_rate, cb_sample = calc_recent_win_rate(m.name, m.cb_trades or 15)
        win_rate_data = calc_rolling_win_rate(m.name, window=m.cb_trades or 15)
        nav_data = calc_nav_curve(m.name)
        cards.append({
            'model': m,
            'signal_count': len(signals),
            'portfolio_count': len(portfolio),
            'cb_status': cb.status if cb else 'normal',
            'cb_win_rate': cb_win_rate,
            'win_rate_data': win_rate_data,
            'nav_data': nav_data,
        })
    return render_template('overview.html', cards=cards)


@views_bp.route('/model/<name>')
def model_detail(name):
    """模型详情页."""
    model = ModelRegistry.query.filter_by(name=name).first_or_404()
    signals = get_latest_signals(name)
    portfolio = get_portfolio(name)
    cb = get_latest_cb_status(name)
    win_rate_data = calc_rolling_win_rate(name, window=model.cb_trades or 15)
    nav_data = calc_nav_curve(name)
    monthly_data = calc_monthly_returns(name)

    # 实时行情
    quotes = {}
    codes = [t.code for t in portfolio] + [s.code for s in signals]
    if codes:
        try:
            from services.market_data import get_realtime_quotes
            quotes = get_realtime_quotes(list(set(codes)))
        except Exception:
            pass

    return render_template(
        'model.html', model=model, signals=signals,
        portfolio=portfolio, cb=cb,
        win_rate_data=win_rate_data, nav_data=nav_data,
        monthly_data=monthly_data, quotes=quotes,
    )


@views_bp.route('/trades')
def trades():
    """交易历史页."""
    model_name = request.args.get('model')
    start = request.args.get('start')
    end = request.args.get('end')
    page = request.args.get('page', 1, type=int)

    start_date = date.fromisoformat(start) if start else None
    end_date = date.fromisoformat(end) if end else None

    pagination = get_trade_history(
        model_name=model_name, start=start_date, end=end_date, page=page,
    )
    stats = get_trade_stats(model_name=model_name)
    models = get_active_models()

    return render_template(
        'trades.html', pagination=pagination, stats=stats,
        models=models, filters={
            'model': model_name or '', 'start': start or '', 'end': end or '',
        },
    )


@views_bp.route('/tasks')
def tasks():
    """定时任务管理页."""
    from models import ScheduledTask, TaskExecutionLog
    all_tasks = ScheduledTask.query.all()
    task_list = []
    for t in all_tasks:
        last_log = (
            TaskExecutionLog.query
            .filter_by(task_id=t.id)
            .order_by(TaskExecutionLog.started_at.desc())
            .first()
        )
        task_list.append({'task': t, 'last_log': last_log})
    models = get_active_models()
    return render_template('tasks.html', task_list=task_list, models=models)


@views_bp.route('/files')
def files():
    """文件管理页."""
    from pathlib import Path
    from flask import current_app

    stock_dir = Path(current_app.config['STOCK_DATA_DIR'])
    dash_dir = Path(__file__).resolve().parent

    def scan(dirpath, exts):
        if not dirpath.exists():
            return []
        result = []
        for f in sorted(dirpath.rglob('*')):
            if f.is_file() and (not exts or f.suffix in exts):
                stat = f.stat()
                result.append({
                    'name': str(f.relative_to(dirpath)),
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                })
        return result

    return render_template('files.html',
        models_files=scan(stock_dir / 'models', {'.txt', '.bin', '.model'}),
        data_files=scan(stock_dir / 'data' / '成分', {'.txt', '.csv'}),
        script_files=scan(stock_dir / 'strategies', {'.py'}),
        stock_data_dir=str(stock_dir),
    )
