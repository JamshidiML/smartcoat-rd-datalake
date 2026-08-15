FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_HOME=/opt/paddle-models \
    HOME=/opt/paddle-home
WORKDIR /worker
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libgomp1 poppler-utils tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/paddle-home /opt/paddle-models
COPY requirements.txt /worker/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN FLAGS_enable_pir_api=0 python -c "from paddleocr import PaddleOCR; PaddleOCR(lang='german', device='cpu', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, enable_mkldnn=True)"
ENV FLAGS_enable_pir_api=0
COPY --from=api-source database.py domain.py identifiers.py storage.py validation.py /worker/
COPY . /worker
RUN useradd --create-home --uid 10002 worker \
    && chown -R worker:worker /worker /opt/paddle-home /opt/paddle-models
USER worker
CMD ["python", "-m", "jobs.worker"]
