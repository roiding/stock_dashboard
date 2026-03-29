"""mootdx 在线行情服务 — 单例 client, 自动重连."""

from datetime import datetime as _dt
from mootdx.quotes import Quotes

_client = None
_req_count = 0
_REQ_PER_CONN = 500  # 每 N 次请求重建连接防止超时


def _market_of(code: str) -> int:
    """0 = 深圳, 1 = 上海."""
    return 1 if code.startswith(('5', '6', '9')) else 0


def _get_client():
    global _client, _req_count
    _req_count += 1
    if _client is None or _req_count % _REQ_PER_CONN == 0:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = Quotes.factory(market='std', timeout=15)
    return _client


def _reconnect():
    global _client
    try:
        if _client is not None:
            _client.close()
    except Exception:
        pass
    _client = Quotes.factory(market='std', timeout=15)


def get_realtime_quotes(codes: list) -> dict:
    """批量获取实时行情.

    Parameters
    ----------
    codes : list[str]
        股票代码列表, 如 ['600123', '000456']

    Returns
    -------
    dict  {code: {price, open, high, low, last_close, vol, amount}}
    """
    if not codes:
        return {}
    result = {}
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            client = _get_client()
            df = client.quotes(symbol=batch)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                code = str(row.get('code', '')).zfill(6)
                last_close = float(row.get('last_close', 0))
                price = float(row.get('price', 0))
                change_pct = ((price / last_close - 1) * 100) if last_close else 0
                result[code] = {
                    'price': price,
                    'open': float(row.get('open', 0)),
                    'high': float(row.get('high', 0)),
                    'low': float(row.get('low', 0)),
                    'last_close': last_close,
                    'vol': float(row.get('vol', 0)),
                    'amount': float(row.get('amount', 0)),
                    'change_pct': round(change_pct, 2),
                }
        except Exception as e:
            print(f'[market_data] batch error: {e}')
            _reconnect()
    return result


def get_recent_trading_days(count=120):
    """获取最近 count 个交易日列表 [date, ...], 升序.

    通过上证指数日K线 (index_bars) 获取交易日历.
    """
    for attempt in range(2):
        try:
            client = _get_client()
            df = client.index_bars(symbol='999999', frequency=9,
                                   start=0, offset=max(count, 120))
            if df is not None and not df.empty:
                dates = set()
                for idx in df.index:
                    try:
                        d = idx.date() if hasattr(idx, 'date') else \
                            _dt.strptime(str(idx)[:10], '%Y-%m-%d').date()
                        dates.add(d)
                    except (ValueError, TypeError):
                        continue
                if not dates and 'datetime' in df.columns:
                    for dt_str in df['datetime']:
                        try:
                            d = _dt.strptime(str(dt_str)[:10], '%Y-%m-%d').date()
                            dates.add(d)
                        except (ValueError, TypeError):
                            continue
                if dates:
                    dates = sorted(dates)
                    return dates[-count:] if len(dates) > count else dates
        except Exception as e:
            print(f'[market_data] get_trading_days error (attempt {attempt+1}): {e}')
            _reconnect()

    return []
