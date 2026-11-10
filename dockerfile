
FROM python:3.14-slim-trixie

WORKDIR /app

RUN pip install --no-cache-dir requests

COPY app.py .

CMD ["python", "-u", "app.py"]
