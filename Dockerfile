FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
ENV HOST=0.0.0.0 PORT=8000
CMD ["order-approvals"]
