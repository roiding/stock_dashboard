FROM python:3.11-slim AS builder

# ---- TA-Lib C library (源码编译, 支持任意架构) ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make wget && \
    wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && ./configure --prefix=/usr && make -j"$(nproc)" && make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# ---- 运行阶段 ----
FROM python:3.11-slim

# 从 builder 复制 TA-Lib 动态库 + Python 包
COPY --from=builder /usr/lib/libta_lib* /usr/lib/
COPY --from=builder /install /usr/local

WORKDIR /app
COPY . .

ENV STOCK_DATA_DIR=/data \
    API_BASE_URL=http://localhost:5000 \
    FLASK_DEBUG=0

EXPOSE 5000

ENTRYPOINT ["sh", "entrypoint.sh"]
