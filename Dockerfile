FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd --create-home appuser && mkdir -p /app/data && chown appuser /app/data
USER appuser

EXPOSE 5000

CMD ["gunicorn", "app:app", "-b", "0.0.0.0:5000", "--workers", "1", "--threads", "4"]
