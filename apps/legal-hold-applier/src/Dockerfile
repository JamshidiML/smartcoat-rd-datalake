FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=smartcoat-packages smartcoat_logging /app/packages/smartcoat_logging
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY contract.py main.py ./
RUN useradd --create-home --uid 10003 holdapplier && chown -R holdapplier:holdapplier /app
USER holdapplier
EXPOSE 8090
CMD ["python", "main.py"]
