FROM astral/uv:0.12.18-python3.13-trixie-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
RUN uv sync --locked --package ultrademo-agent
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 --create-home app && chown -R app /app
USER app
# Bake VAD and turn-detector weights into the image so a job never downloads at call time.
RUN python -m livekit.agents download-files
CMD ["python", "-m", "livekit.agents", "start", "services/session-agent/src/ultrademo_agent/worker.py"]
