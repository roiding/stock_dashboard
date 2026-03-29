"""建表 + seed 数据 (幂等)."""

from app import create_app
from models import db, ModelRegistry, ScheduledTask


def init_db():
    app = create_app()
    with app.app_context():
        db.create_all()
        print('[init_db] Tables created / verified.')

        # 自动添加 is_virtual 列 (如表已存在但缺少该列)
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('trades')]
        if 'is_virtual' not in columns:
            db.session.execute(
                text('ALTER TABLE trades ADD COLUMN is_virtual BOOLEAN DEFAULT 0'))
            db.session.commit()
            print('[init_db] Added is_virtual column to trades table.')
        else:
            print('[init_db] is_virtual column already exists.')

        # ---- seed model_registry ----
        models_data = [
            dict(name='p0_30', display_name='0-30日模型', hold_days=10,
                 tp_pct=10.0, daily_picks=3, pred_threshold=2.0,
                 cb_trades=15, cb_low=20.0, cb_high=50.0),
            # dict(name='st_2d', display_name='ST 2日模型', hold_days=2,
            #      tp_pct=10.0, daily_picks=2, pred_threshold=2.0,
            #      cb_trades=15, cb_low=20.0, cb_high=50.0),
        ]
        for m in models_data:
            if not ModelRegistry.query.filter_by(name=m['name']).first():
                db.session.add(ModelRegistry(**m))
                print(f'  + model: {m["name"]}')

        # ---- seed scheduled_tasks ----
        tasks_data = [
            dict(model_name='p0_30', task_type='predict',
                 cron_expr='30 15 * * 1-5',
                 script_path='strategies/p0_30/predict_online.py',
                 description='0-30模型 工作日15:30预测'),
            dict(model_name='p0_30', task_type='circuit_breaker',
                 cron_expr='55 14 * * 1-5',
                 script_path='strategies/p0_30/circuit_breaker.py',
                 description='0-30模型 工作日14:55熔断检查'),
            # dict(model_name='st_2d', task_type='predict',
            #      cron_expr='30 15 * * 1-5',
            #      script_path='strategies/st_2d/predict.py',
            #      description='ST模型 工作日15:30预测'),
            # dict(model_name='st_2d', task_type='circuit_breaker',
            #      cron_expr='0 16 * * 1-5',
            #      script_path='strategies/p0_30/circuit_breaker.py',
            #      description='ST模型 工作日16:00熔断检查'),
        ]
        for t in tasks_data:
            exists = ScheduledTask.query.filter_by(
                model_name=t['model_name'], task_type=t['task_type'],
            ).first()
            if not exists:
                db.session.add(ScheduledTask(**t))
                print(f'  + task: {t["model_name"]}/{t["task_type"]}')

        db.session.commit()
        print('[init_db] Done.')


if __name__ == '__main__':
    init_db()
