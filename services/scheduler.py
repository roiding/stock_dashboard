"""APScheduler 定时任务管理 — 从 DB 加载 cron 任务, 支持热更新."""

import os
import subprocess
import threading
from datetime import datetime, date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = BackgroundScheduler(daemon=True)
_app = None  # Flask app 引用, 供 run_task 使用


def _is_trading_day() -> bool:
    """判断今天是否为交易日 (通过 market_data 获取交易日历)."""
    from services.market_data import get_recent_trading_days
    trading_days = get_recent_trading_days(30)
    return date.today() in trading_days if trading_days else False


def init_scheduler(app):
    """读取 scheduled_tasks 表, 注册启用的任务, 启动调度器."""
    global _app
    _app = app

    with app.app_context():
        from models import ScheduledTask
        tasks = ScheduledTask.query.filter_by(is_enabled=True).all()
        for task in tasks:
            _register_job(task)

    # 系统级任务: 每日结算 (15:10 周一到周五)
    scheduler.add_job(
        _run_daily_settle_wrapper,
        CronTrigger(hour=15, minute=10, day_of_week='mon-fri'),
        id='system_daily_settle',
        replace_existing=True,
    )

    scheduler.start()


def _parse_cron(expr: str) -> dict:
    """将 '30 15 * * 1-5' 拆成 CronTrigger 参数."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f'Invalid cron expression: {expr}')
    return dict(
        minute=parts[0], hour=parts[1],
        day=parts[2], month=parts[3],
        day_of_week=parts[4],
    )


def _register_job(task):
    try:
        trigger = CronTrigger(**_parse_cron(task.cron_expr))
        scheduler.add_job(
            _run_task_wrapper,
            trigger,
            args=[task.id],
            id=f'task_{task.id}',
            replace_existing=True,
        )
    except Exception as e:
        print(f'[scheduler] Failed to register task {task.id}: {e}')


def _run_task_wrapper(task_id: int):
    """在 Flask app context 中执行任务 (非交易日跳过)."""
    if _app is None:
        return
    with _app.app_context():
        if not _is_trading_day():
            print(f'[scheduler] 非交易日, 跳过任务 {task_id}')
            return
        run_task(task_id)


def _run_daily_settle_wrapper():
    """在 Flask app context 中执行每日结算 (非交易日跳过)."""
    if _app is None:
        return
    with _app.app_context():
        if not _is_trading_day():
            print('[scheduler] 非交易日, 跳过每日结算')
            return
        from services.daily_settle import run_daily_settle
        run_daily_settle()


def run_task(task_id: int):
    """执行一个任务: subprocess 运行脚本, 记录日志."""
    from models import db, ScheduledTask, TaskExecutionLog

    task = ScheduledTask.query.get(task_id)
    if task is None:
        return

    # 提前复制需要的属性，避免会话关闭后访问
    task_model_name = task.model_name
    task_task_type = task.task_type
    task_script_path = task.script_path

    log = TaskExecutionLog(
        task_id=task.id,
        model_name=task_model_name,
        task_type=task_task_type,
        started_at=datetime.utcnow(),
        status='running',
    )
    db.session.add(log)
    db.session.commit()
    log_id = log.id

    def _execute():
        with _app.app_context():
            log_ref = TaskExecutionLog.query.get(log_id)
            try:
                api_base = _app.config.get('API_BASE_URL', 'http://localhost:5000')
                stock_data_dir = _app.config.get('STOCK_DATA_DIR', '')
                env = {**os.environ,
                       'API_BASE_URL': api_base,
                       'STOCK_DATA_DIR': stock_data_dir}
                # script_path 相对于 STOCK_DATA_DIR (如 strategies/p0_30/predict_online.py)
                script = os.path.join(stock_data_dir, task_script_path)
                result = subprocess.run(
                    ['python', script],
                    capture_output=True, text=True, timeout=600, env=env,
                )
                log_ref.output = result.stdout[-4000:] if result.stdout else None
                log_ref.error = result.stderr[-4000:] if result.stderr else None
                log_ref.status = 'success' if result.returncode == 0 else 'failed'
            except subprocess.TimeoutExpired:
                log_ref.status = 'failed'
                log_ref.error = 'Timeout after 600s'
            except Exception as e:
                log_ref.status = 'failed'
                log_ref.error = str(e)[:4000]
            finally:
                log_ref.finished_at = datetime.utcnow()
                db.session.commit()

    # 后台线程执行, 不阻塞调度器 / API
    threading.Thread(target=_execute, daemon=True).start()


def reload_task(task_id: int):
    """cron 更新后重新注册任务 (或移除禁用的任务)."""
    from models import ScheduledTask
    task = ScheduledTask.query.get(task_id)
    if task is None:
        return

    job_id = f'task_{task_id}'

    # 先移除旧 job
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # 重新注册 (如果仍启用)
    if task and task.is_enabled:
        _register_job(task)
