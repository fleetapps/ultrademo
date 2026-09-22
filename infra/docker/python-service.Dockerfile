# Build one Python workspace member: docker build --build-arg PACKAGE=ultrademo-api -f infra/docker/python-service.Dockerfile .
FROM astral/uv:0.12.18-python3.13-trixie-slim
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
RUN uv sync --locked --package "${PACKAGE}"
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
