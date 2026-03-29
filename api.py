"""REST API Blueprint — 数据推送/查询 + 任务管理 + 文件管理.

前缀 /api, 通用响应: {"status": "ok/error", ...}
"""

import os
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from models import db, Signal, Trade, CircuitBreakerLog, ScheduledTask, TaskExecutionLog, DailyNav

api_bp = Blueprint('api', __name__)


def ok(data=None, **extra):
    body = {'status': 'ok'}
    if data is not None:
        body['data'] = data
    body.update(extra)
    return jsonify(body)


def err(message, code=400):
    return jsonify({'status': 'error', 'message': message}), code


# ================================================================
# 推送接口
# ================================================================

@api_bp.route('/signals', methods=['POST'])
def push_signals():
    """推送每日预测, 自动计算 rank."""
    body = request.get_json(force=True)
    model_name = body.get('model')
    date_str = body.get('date')
    picks = body.get('picks', [])
    if not model_name or not date_str or not picks:
        return err('model, date, picks are required')

    signal_date = date.fromisoformat(date_str)
    # 按 pred 降序排名
    picks_sorted = sorted(picks, key=lambda p: p.get('pred', 0), reverse=True)

    count = 0
    for rank, p in enumerate(picks_sorted, start=1):
        sig = Signal.query.filter_by(
            model_name=model_name, signal_date=signal_date, code=p['code'],
        ).first()
        if sig:
            sig.close = p.get('close')
            sig.pred = p.get('pred')
            sig.rank = rank
        else:
            sig = Signal(
                model_name=model_name, signal_date=signal_date,
                code=p['code'], close=p.get('close'),
                pred=p.get('pred'), rank=rank,
            )
            db.session.add(sig)
        count += 1
    db.session.commit()
    return ok(count=count)


@api_bp.route('/trades/buy', methods=['POST'])
def trade_buy():
    """记录买入."""
    body = request.get_json(force=True)
    model_name = body.get('model')
    code = body.get('code')
    date_str = body.get('date')
    price = body.get('price')
    if not all([model_name, code, date_str, price]):
        return err('model, code, date, price are required')

    buy_date = date.fromisoformat(date_str)
    # signal_date: 从 signals 表查找最近的信号日
    signal_date_val = body.get('signal_date')
    if signal_date_val:
        signal_date = date.fromisoformat(signal_date_val)
    else:
        sig = (
            Signal.query
            .filter_by(model_name=model_name, code=code)
            .filter(Signal.signal_date <= buy_date)
            .order_by(Signal.signal_date.desc())
            .first()
        )
        signal_date = sig.signal_date if sig else buy_date

    trade = Trade(
        model_name=model_name, code=code,
        signal_date=signal_date, buy_date=buy_date,
        buy_price=price,
        is_virtual=bool(body.get('is_virtual', False)),
    )
    db.session.add(trade)
    db.session.commit()
    return ok(trade.to_dict())


@api_bp.route('/trades/sell', methods=['POST'])
def trade_sell():
    """记录卖出, 自动算 pnl."""
    body = request.get_json(force=True)
    trade_id = body.get('trade_id')
    date_str = body.get('date')
    price = body.get('price')
    reason = body.get('reason')
    if not all([trade_id, date_str, price]):
        return err('trade_id, date, price are required')

    trade = Trade.query.get(trade_id)
    if not trade:
        return err('trade not found', 404)
    if trade.sell_date:
        return err('trade already sold')

    trade.sell_date = date.fromisoformat(date_str)
    trade.sell_price = price
    trade.sell_reason = reason
    trade.pnl = round(price / trade.buy_price - 1, 6) if trade.buy_price else None
    db.session.commit()
    return ok(trade.to_dict())


@api_bp.route('/circuit-breaker', methods=['POST'])
def push_circuit_breaker():
    """推送熔断检查结果."""
    body = request.get_json(force=True)
    model_name = body.get('model')
    date_str = body.get('date')
    if not model_name or not date_str:
        return err('model, date are required')

    log = CircuitBreakerLog(
        model_name=model_name,
        check_date=date.fromisoformat(date_str),
        win_rate=body.get('win_rate'),
        sample_size=body.get('sample_size'),
        status=body.get('status', 'normal'),
        message=body.get('message'),
    )
    db.session.add(log)
    db.session.commit()
    return ok(log.to_dict())


@api_bp.route('/circuit-breaker/<model>/latest', methods=['GET'])
def get_circuit_breaker_latest(model):
    """获取最新一条熔断状态."""
    latest = (CircuitBreakerLog.query
              .filter_by(model_name=model)
              .order_by(CircuitBreakerLog.check_date.desc())
              .first())
    if not latest:
        return ok({'status': 'normal', 'message': '无记录'})
    return ok(latest.to_dict())


@api_bp.route('/trading-days', methods=['GET'])
def get_trading_days():
    """返回最近 N 个交易日 (供 circuit_breaker 等外部脚本调用)."""
    count = request.args.get('count', 120, type=int)
    from services.market_data import get_recent_trading_days
    days = get_recent_trading_days(count)
    return ok([d.isoformat() for d in days])


# ================================================================
# 查询接口
# ================================================================

@api_bp.route('/settle', methods=['POST'])
def trigger_settle():
    """手动触发每日结算 (系统级: 对所有 active 模型执行买卖+NAV更新)."""
    from services.daily_settle import run_daily_settle
    results = run_daily_settle()
    return ok(results)


@api_bp.route('/nav/<model>', methods=['POST'])
def push_nav(model):
    """批量推送每日净值. body: {data: [{date, nav, open_positions, daily_return}, ...]}"""
    body = request.get_json(force=True)
    rows = body.get('data', [])
    if not rows:
        return err('data array is required')
    count = 0
    for r in rows:
        dt = date.fromisoformat(r['date'])
        existing = DailyNav.query.filter_by(model_name=model, date=dt).first()
        if existing:
            existing.nav = r['nav']
            existing.open_positions = r.get('open_positions', 0)
            existing.daily_return = r.get('daily_return')
        else:
            db.session.add(DailyNav(
                model_name=model, date=dt,
                nav=r['nav'],
                open_positions=r.get('open_positions', 0),
                daily_return=r.get('daily_return'),
            ))
        count += 1
    db.session.commit()
    return ok({'count': count})


@api_bp.route('/nav/<model>', methods=['GET'])
def get_nav(model):
    """查询净值曲线."""
    rows = DailyNav.query.filter_by(model_name=model).order_by(DailyNav.date).all()
    return ok([r.to_dict() for r in rows])

@api_bp.route('/portfolio/<model>', methods=['GET'])
def get_portfolio(model):
    trades = Trade.query.filter_by(model_name=model, sell_date=None).all()
    return ok([t.to_dict() for t in trades], count=len(trades))


@api_bp.route('/trades/<model>/closed', methods=['GET'])
def get_closed_trades(model):
    """查询已平仓交易 (供熔断脚本调用). ?limit=50"""
    limit = request.args.get('limit', 50, type=int)
    trades = (
        Trade.query
        .filter_by(model_name=model)
        .filter(Trade.sell_date.isnot(None))
        .order_by(Trade.sell_date.desc(), Trade.buy_date.desc())
        .limit(limit)
        .all()
    )
    return ok([t.to_dict() for t in trades], count=len(trades))


@api_bp.route('/signals/<model>/<date_str>', methods=['GET'])
def get_signals(model, date_str):
    signal_date = date.fromisoformat(date_str)
    signals = (
        Signal.query
        .filter_by(model_name=model, signal_date=signal_date)
        .order_by(Signal.rank)
        .all()
    )
    return ok([s.to_dict() for s in signals], count=len(signals))


# ================================================================
# 模型管理
# ================================================================

@api_bp.route('/models', methods=['GET'])
def list_models():
    from models import ModelRegistry
    models = ModelRegistry.query.all()
    return ok([m.to_dict() for m in models])


@api_bp.route('/models', methods=['POST'])
def create_model():
    """快速注册新模型."""
    from models import ModelRegistry
    body = request.get_json(force=True)
    name = body.get('name', '').strip()
    if not name:
        return err('name is required')
    if ModelRegistry.query.filter_by(name=name).first():
        return err(f'Model "{name}" already exists')
    m = ModelRegistry(
        name=name,
        display_name=body.get('display_name', name),
        hold_days=body.get('hold_days', 5),
        tp_pct=body.get('tp_pct', 10.0),
        daily_picks=body.get('daily_picks', 3),
        pred_threshold=body.get('pred_threshold', 2.0),
        cb_trades=body.get('cb_trades', 15),
        cb_low=body.get('cb_low', 20.0),
        cb_high=body.get('cb_high', 50.0),
    )
    db.session.add(m)
    db.session.commit()
    return ok(m.to_dict())


@api_bp.route('/models/<name>', methods=['PUT'])
def update_model(name):
    """编辑模型可变参数."""
    from models import ModelRegistry
    m = ModelRegistry.query.filter_by(name=name).first()
    if not m:
        return err(f'Model "{name}" not found', 404)

    body = request.get_json(force=True)
    mutable = (
        'display_name', 'hold_days', 'tp_pct', 'daily_picks',
        'pred_threshold', 'cb_trades', 'cb_low', 'cb_high', 'is_active',
    )
    for field in mutable:
        if field in body:
            setattr(m, field, body[field])
    db.session.commit()
    return ok(m.to_dict())


@api_bp.route('/models/<name>', methods=['DELETE'])
def delete_model(name):
    """删除模型及其全部关联数据 (信号/交易/净值/熔断/任务)."""
    from models import ModelRegistry
    m = ModelRegistry.query.filter_by(name=name).first()
    if not m:
        return err(f'Model "{name}" not found', 404)

    # 级联删除关联数据
    DailyNav.query.filter_by(model_name=name).delete()
    Signal.query.filter_by(model_name=name).delete()
    Trade.query.filter_by(model_name=name).delete()
    CircuitBreakerLog.query.filter_by(model_name=name).delete()

    from models import ScheduledTask, TaskExecutionLog
    tasks = ScheduledTask.query.filter_by(model_name=name).all()
    for t in tasks:
        TaskExecutionLog.query.filter_by(task_id=t.id).delete()
        db.session.delete(t)

    db.session.delete(m)
    db.session.commit()
    return ok({'deleted': name})


# ================================================================
# 定时任务管理
# ================================================================

@api_bp.route('/tasks', methods=['GET'])
def list_tasks():
    """列出所有定时任务配置 + 最近执行状态."""
    tasks = ScheduledTask.query.all()
    result = []
    for t in tasks:
        d = t.to_dict()
        last_log = (
            TaskExecutionLog.query
            .filter_by(task_id=t.id)
            .order_by(TaskExecutionLog.started_at.desc())
            .first()
        )
        d['last_execution'] = last_log.to_dict() if last_log else None
        result.append(d)
    return ok(result, count=len(result))


@api_bp.route('/tasks', methods=['POST'])
def create_task():
    """新建定时任务."""
    body = request.get_json(force=True)
    required = ['model_name', 'task_type', 'cron_expr', 'script_path']
    for f in required:
        if not body.get(f):
            return err(f'Missing required field: {f}')
    task = ScheduledTask(
        model_name=body['model_name'],
        task_type=body['task_type'],
        cron_expr=body['cron_expr'],
        script_path=body['script_path'],
        description=body.get('description', ''),
        is_enabled=body.get('is_enabled', True),
    )
    db.session.add(task)
    db.session.commit()

    from services.scheduler import reload_task
    reload_task(task.id)

    return ok(task.to_dict())


@api_bp.route('/tasks/<int:task_id>', methods=['PUT'])
def update_task(task_id):
    """更新任务字段."""
    task = ScheduledTask.query.get(task_id)
    if not task:
        return err('task not found', 404)

    body = request.get_json(force=True)
    for field in ('cron_expr', 'script_path', 'description', 'is_enabled', 'model_name', 'task_type'):
        if field in body:
            setattr(task, field, body[field])
    task.updated_at = datetime.utcnow()
    db.session.commit()

    from services.scheduler import reload_task
    reload_task(task_id)

    return ok(task.to_dict())


@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    """删除定时任务."""
    task = ScheduledTask.query.get(task_id)
    if not task:
        return err('task not found', 404)

    from services.scheduler import reload_task, scheduler
    job_id = f'task_{task_id}'
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # 删除关联日志
    TaskExecutionLog.query.filter_by(task_id=task_id).delete()
    db.session.delete(task)
    db.session.commit()
    return ok({'deleted': task_id})


@api_bp.route('/tasks/<int:task_id>/run', methods=['POST'])
def trigger_task(task_id):
    """手动触发一次执行 (不等待完成)."""
    task = ScheduledTask.query.get(task_id)
    if not task:
        return err('task not found', 404)

    from services.scheduler import run_task
    run_task(task_id)
    return ok({'message': f'Task {task_id} triggered'})


@api_bp.route('/tasks/<int:task_id>/logs', methods=['GET'])
def get_task_logs(task_id):
    """查询执行日志, 支持 ?limit=20."""
    limit = request.args.get('limit', 20, type=int)
    logs = (
        TaskExecutionLog.query
        .filter_by(task_id=task_id)
        .order_by(TaskExecutionLog.started_at.desc())
        .limit(limit)
        .all()
    )
    return ok([l.to_dict() for l in logs], count=len(logs))


# ================================================================
# 文件管理 (模型 / 板块数据 / 策略脚本)
# ================================================================

def _stock_data_dir() -> Path:
    return Path(current_app.config['STOCK_DATA_DIR'])


# 允许的目录映射: category → (base_dir_func, allowed_exts)
_FILE_CATEGORIES = {
    'models':     (lambda: _stock_data_dir() / 'models',           {'.txt', '.bin', '.model'}),
    'data':       (lambda: _stock_data_dir() / 'data' / '成分',     {'.txt', '.csv'}),
    'strategies': (lambda: _stock_data_dir() / 'strategies',        {'.py'}),
}


def _list_dir(dirpath: Path, exts: set) -> list:
    """列出目录下匹配扩展名的文件."""
    if not dirpath.exists():
        return []
    files = []
    for f in sorted(dirpath.rglob('*')):
        if f.is_file() and (not exts or f.suffix in exts):
            files.append({
                'name': str(f.relative_to(dirpath)),
                'size': f.stat().st_size,
                'modified': datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            })
    return files


@api_bp.route('/files', methods=['GET'])
def list_files():
    """列出所有管理文件: models, data, scripts."""
    result = {}
    for cat, (dir_fn, exts) in _FILE_CATEGORIES.items():
        result[cat] = _list_dir(dir_fn(), exts)
    return ok(result)


@api_bp.route('/files/<category>', methods=['POST'])
def upload_file(category):
    """上传文件. multipart/form-data, field name = 'file'.

    models:     {STOCK_DATA_DIR}/models/
    data:       {STOCK_DATA_DIR}/data/成分/
    strategies: {STOCK_DATA_DIR}/strategies/{subdir}/ (需 subdir 参数, 如 p0_30)
    """
    if category not in _FILE_CATEGORIES:
        return err(f'Invalid category: {category}. Use: {list(_FILE_CATEGORIES.keys())}')

    if 'file' not in request.files:
        return err('No file part in request')
    f = request.files['file']
    if not f.filename:
        return err('Empty filename')

    dir_fn, exts = _FILE_CATEGORIES[category]
    target_dir = dir_fn()

    # strategies 需要 subdir (模型子目录)
    if category == 'strategies':
        subdir = request.form.get('subdir', '').strip()
        if not subdir:
            return err('strategies upload requires "subdir" param (e.g. p0_30)')
        target_dir = target_dir / secure_filename(subdir)

    filename = secure_filename(f.filename)
    if not filename:
        return err('Invalid filename')
    if exts and Path(filename).suffix not in exts:
        return err(f'Extension not allowed. Accepted: {exts}')

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    f.save(str(dest))

    return ok({
        'category': category,
        'name': filename,
        'size': dest.stat().st_size,
        'path': str(dest.relative_to(_stock_data_dir())),
    })


@api_bp.route('/files/<category>/<path:filename>', methods=['DELETE'])
def delete_file(category, filename):
    """删除一个文件."""
    if category not in _FILE_CATEGORIES:
        return err('Invalid category')

    dir_fn, _ = _FILE_CATEGORIES[category]
    target = dir_fn() / filename

    # 安全检查: 不允许 .. 穿越
    try:
        target.resolve().relative_to(dir_fn().resolve())
    except ValueError:
        return err('Path traversal not allowed')

    if not target.exists():
        return err('File not found', 404)

    target.unlink()
    return ok({'deleted': filename})


# ================================================================
# 依赖管理 (requirements_extra.txt)
# ================================================================

def _extra_req_path() -> Path:
    return _stock_data_dir() / 'requirements_extra.txt'


@api_bp.route('/packages', methods=['GET'])
def get_packages():
    """返回 extra requirements 内容 + 已安装包列表."""
    req_path = _extra_req_path()
    content = req_path.read_text(encoding='utf-8') if req_path.exists() else ''
    return ok({'requirements': content})


@api_bp.route('/packages', methods=['POST'])
def save_packages():
    """保存 requirements_extra.txt 内容."""
    body = request.get_json(force=True)
    content = body.get('requirements', '')
    req_path = _extra_req_path()
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(content, encoding='utf-8')
    return ok({'saved': str(req_path)})


@api_bp.route('/packages/install', methods=['POST'])
def install_packages():
    """立即执行 pip install -r requirements_extra.txt."""
    import subprocess as sp
    req_path = _extra_req_path()
    if not req_path.exists() or not req_path.read_text().strip():
        return err('requirements_extra.txt is empty')
    try:
        result = sp.run(
            ['pip', 'install', '-r', str(req_path)],
            capture_output=True, text=True, timeout=300,
        )
        return ok({
            'returncode': result.returncode,
            'stdout': result.stdout[-4000:],
            'stderr': result.stderr[-2000:],
        })
    except sp.TimeoutExpired:
        return err('pip install timeout (300s)')
    except Exception as e:
        return err(str(e))
