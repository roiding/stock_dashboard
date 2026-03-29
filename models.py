from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class ModelRegistry(db.Model):
    __tablename__ = 'model_registry'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    display_name = db.Column(db.String(100))
    hold_days = db.Column(db.Integer)
    tp_pct = db.Column(db.Float)
    daily_picks = db.Column(db.Integer)
    pred_threshold = db.Column(db.Float)
    cb_trades = db.Column(db.Integer)
    cb_low = db.Column(db.Float)
    cb_high = db.Column(db.Float)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'display_name': self.display_name, 'hold_days': self.hold_days,
            'tp_pct': self.tp_pct, 'daily_picks': self.daily_picks,
            'pred_threshold': self.pred_threshold,
            'cb_trades': self.cb_trades, 'cb_low': self.cb_low,
            'cb_high': self.cb_high, 'is_active': self.is_active,
        }


class Signal(db.Model):
    __tablename__ = 'signals'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    model_name = db.Column(db.String(50), db.ForeignKey('model_registry.name'), nullable=False)
    signal_date = db.Column(db.Date, nullable=False)
    code = db.Column(db.String(10), nullable=False)
    close = db.Column(db.Float)
    pred = db.Column(db.Float)
    rank = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('model_name', 'signal_date', 'code'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'model_name': self.model_name,
            'signal_date': str(self.signal_date), 'code': self.code,
            'close': self.close, 'pred': self.pred, 'rank': self.rank,
        }


class Trade(db.Model):
    __tablename__ = 'trades'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    model_name = db.Column(db.String(50), db.ForeignKey('model_registry.name'), nullable=False)
    code = db.Column(db.String(10), nullable=False)
    signal_date = db.Column(db.Date, nullable=False)
    buy_date = db.Column(db.Date, nullable=False)
    buy_price = db.Column(db.Float, nullable=False)
    sell_date = db.Column(db.Date, nullable=True)
    sell_price = db.Column(db.Float, nullable=True)
    sell_reason = db.Column(
        db.Enum('tp', 'expire', 'circuit_breaker', 'manual', name='sell_reason_enum'),
        nullable=True,
    )
    pnl = db.Column(db.Float, nullable=True)
    is_virtual = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'model_name': self.model_name,
            'code': self.code, 'signal_date': str(self.signal_date),
            'buy_date': str(self.buy_date), 'buy_price': self.buy_price,
            'sell_date': str(self.sell_date) if self.sell_date else None,
            'sell_price': self.sell_price, 'sell_reason': self.sell_reason,
            'pnl': self.pnl,
            'is_virtual': self.is_virtual,
        }


class CircuitBreakerLog(db.Model):
    __tablename__ = 'circuit_breaker_log'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    model_name = db.Column(db.String(50), db.ForeignKey('model_registry.name'), nullable=False)
    check_date = db.Column(db.Date, nullable=False)
    win_rate = db.Column(db.Float)
    sample_size = db.Column(db.Integer)
    status = db.Column(
        db.Enum('normal', 'observe', 'circuit_break', name='cb_status_enum'),
        nullable=False,
    )
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'model_name': self.model_name,
            'check_date': str(self.check_date), 'win_rate': self.win_rate,
            'sample_size': self.sample_size, 'status': self.status,
            'message': self.message,
        }


class ScheduledTask(db.Model):
    __tablename__ = 'scheduled_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    model_name = db.Column(db.String(50), db.ForeignKey('model_registry.name'), nullable=False)
    task_type = db.Column(
        db.Enum('predict', 'circuit_breaker', name='task_type_enum'),
        nullable=False,
    )
    cron_expr = db.Column(db.String(50))
    script_path = db.Column(db.String(200))
    is_enabled = db.Column(db.Boolean, default=True)
    description = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('model_name', 'task_type'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'model_name': self.model_name,
            'task_type': self.task_type, 'cron_expr': self.cron_expr,
            'script_path': self.script_path, 'is_enabled': self.is_enabled,
            'description': self.description,
            'updated_at': str(self.updated_at) if self.updated_at else None,
        }


class TaskExecutionLog(db.Model):
    __tablename__ = 'task_execution_log'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.Integer, db.ForeignKey('scheduled_tasks.id'), nullable=False)
    model_name = db.Column(db.String(50))
    task_type = db.Column(db.String(20))
    started_at = db.Column(db.DateTime, nullable=False)
    finished_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(
        db.Enum('running', 'success', 'failed', name='exec_status_enum'),
        nullable=False,
    )
    output = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id, 'task_id': self.task_id,
            'model_name': self.model_name, 'task_type': self.task_type,
            'started_at': str(self.started_at) if self.started_at else None,
            'finished_at': str(self.finished_at) if self.finished_at else None,
            'status': self.status, 'output': self.output, 'error': self.error,
        }


class DailyNav(db.Model):
    """每日净值 (由脚本计算后推送, mark-to-market)."""
    __tablename__ = 'daily_nav'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    model_name = db.Column(db.String(50), db.ForeignKey('model_registry.name'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    nav = db.Column(db.Float, nullable=False)
    open_positions = db.Column(db.Integer, default=0)
    daily_return = db.Column(db.Float)   # 当日收益率
    __table_args__ = (db.UniqueConstraint('model_name', 'date'),)

    def to_dict(self):
        return {
            'date': str(self.date), 'nav': self.nav,
            'open_positions': self.open_positions,
            'daily_return': self.daily_return,
        }
