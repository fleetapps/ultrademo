# Playwright's image ships Chromium builds that match playwright==1.63.0 and the system libraries they need.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
COPY --from=astral/uv:0.12.18 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3.12
WORKDIR /app
COPY . .
RUN uv sync --locked --package ultrademo-operator
ENV PATH="/app/.venv/bin:$PATH"
USER pwuser
EXPOSE 8100
CMD ["uvicorn", "ultrademo_operator.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100"]
