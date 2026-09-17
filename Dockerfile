# 可通过构建参数使用镜像站；发布记录必须保留实际使用的摘要。
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:2f2e5a876c71a6757f55ec57f2add0225ddaf01c802a33fcc29073943f94d907
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml requirements-dev.lock README.md ./
COPY app/ ./app/
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --constraint requirements-dev.lock . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/attachments /data/application-marketplace-demo \
    && chown -R app:app /data
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY config/ ./config/
COPY scripts/verify_services.py ./scripts/verify_services.py
# 发布包可能在 umask=077 的目录解压，显式保证非 root 进程可读运行文件。
RUN chmod -R a+rX /app
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
