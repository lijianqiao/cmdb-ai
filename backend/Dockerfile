# 后端镜像：FastAPI + Agent 运行时
#
# 两个容易踩的点写在最前面：
# 1. **ripgrep 是硬依赖**：kb_grep 工具直接 shutil.which("rg")，缺了它知识库
#    全文检索会在运行时返回失败而不是启动就报错，很难联想到是镜像少装了东西。
# 2. **只能单 worker**：SpawnManager / ws_hub / turn_registry 都是进程内状态，
#    app.main.validate_single_worker_environment 会拒绝 WEB_CONCURRENCY != 1。
#    所以这里不传 --workers，横向扩容要先把那三样外置。

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只装依赖：pyproject / uv.lock 不变时这一层可以完全复用缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev


FROM python:3.14-slim AS runtime

# ripgrep：kb_grep 的运行时依赖，见文件头说明
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行；知识库目录要归它所有，否则上传/删除写不进去
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /app/knowledge /app/knowledge_trash \
    && chown -R appuser:appuser /app/knowledge /app/knowledge_trash

USER appuser
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
