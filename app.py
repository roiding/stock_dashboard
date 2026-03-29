"""Flask 应用入口 — create_app() 工厂."""

import os

from flask import Flask

from config import Config
from models import db


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    from api import api_bp
    from views import views_bp
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(views_bp)

    # 启动 APScheduler (仅主进程, 避免 reloader 重复)
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        from services.scheduler import init_scheduler
        init_scheduler(app)

    return app


if __name__ == '__main__':
    app = create_app()
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=5000)
