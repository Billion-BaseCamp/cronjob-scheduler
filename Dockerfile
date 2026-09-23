# Bookworm (Debian 12). python:3.11-slim is Trixie, and Playwright 1.52
# has no Debian 13 dep list — --with-deps then asks apt for Ubuntu 20.04
# packages (ttf-unifont, ttf-ubuntu-font-family) that Trixie removed.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# git+ssh for nucleus; wget/gnupg for Chrome
RUN apt-get update && \
    apt-get install -y --no-install-recommends git openssh-client wget gnupg ca-certificates && \
    mkdir -p /root/.ssh && \
    ssh-keyscan github.com >> /root/.ssh/known_hosts && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY requirements.txt .
RUN --mount=type=ssh pip install --no-cache-dir -r requirements.txt

# channel="chrome" — portal blocks Playwright's bundled Chromium
RUN playwright install --with-deps chrome

RUN apt-get purge -y git && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.cache /root/.ssh

COPY app ./app
COPY main.py .

EXPOSE 8002

# One process. Never --reload. Do not use gunicorn --timeout 60 (Chrome jobs run longer).
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
