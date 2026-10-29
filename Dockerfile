FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --uid 10001 --create-home app && mkdir -p /app/data && chown app:app /app/data
COPY src ./src
RUN python -m compileall -q src

ENV PYTHONUNBUFFERED=1
STOPSIGNAL SIGINT
USER app
CMD ["python", "-m", "src.bot"]
