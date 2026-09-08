FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# install git (required for pip git+ssh installs)
# Install dependencies first, then trust GitHub SSH host
RUN apt-get update && \
    apt-get install -y --no-install-recommends git openssh-client && \
    mkdir -p /root/.ssh && \
    ssh-keyscan github.com >> /root/.ssh/known_hosts && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY requirements.txt .
RUN --mount=type=ssh pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y git && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.cache /root/.ssh

COPY app ./app
COPY main.py .

EXPOSE 8002

CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--workers", "1", "--bind", "0.0.0.0:8002", "--timeout", "60"]
