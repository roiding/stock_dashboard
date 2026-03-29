import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()                           # 读取 .env (git-ignored)


def _build_db_uri():
    """优先用 DATABASE_URL 整串; 否则从拆分字段拼接 (密码自动 URL 编码)."""
    url = os.environ.get('DATABASE_URL')
    if url:
        return url
    user = os.environ.get('DB_USER', 'root')
    pwd = quote_plus(os.environ.get('DB_PASSWORD', 'password'))
    host = os.environ.get('DB_HOST', 'localhost')
    port = os.environ.get('DB_PORT', '3306')
    name = os.environ.get('DB_NAME', 'stock_dashboard')
    return f'mysql+pymysql://{user}:{pwd}@{host}:{port}/{name}'


class Config:
    SQLALCHEMY_DATABASE_URI = _build_db_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_POOL_RECYCLE = 280     # 5分钟内回收, 避免MySQL默认wait_timeout(8h)断连
    SQLALCHEMY_POOL_PRE_PING = True   # 每次取连接前ping一下, 死连接自动丢弃
    SQLALCHEMY_POOL_SIZE = 5
    SQLALCHEMY_POOL_TIMEOUT = 10
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key')
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200MB, 模型文件最大 ~124MB

    # scheduler subprocess 调用 API 的基地址
    API_BASE_URL = os.environ.get('API_BASE_URL', 'http://localhost:5000')

    # 主项目根目录 — 模型文件、板块数据等存放位置
    # 本机: 默认 dashboard/../ 即 stock_data/
    # 服务器: export STOCK_DATA_DIR=/opt/stock_data
    STOCK_DATA_DIR = os.environ.get(
        'STOCK_DATA_DIR',
        str(Path(__file__).resolve().parent.parent),
    )
