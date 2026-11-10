FROM python:3.13-slim AS build
RUN pip install --no-cache-dir uv==0.12.17
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/srv/.venv/bin:$PATH
WORKDIR /srv
COPY --from=build /srv/.venv /srv/.venv
COPY app /srv/app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
