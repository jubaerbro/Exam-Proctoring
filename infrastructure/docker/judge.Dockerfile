# Sentinel judge worker.
#
# IMPLEMENTED (Phase 3). This image runs the worker that claims submissions from
# Redis and executes them in sandbox containers. It contains no candidate code
# itself — it drives the sandbox images from outside.
#
# The sandbox security tests (tests/security/test_judge_sandbox.py) pass against
# a real Docker daemon before this is called complete, which is the gate
# MVP_SCOPE.md §5 sets for Phase 3.
#
# One thing to be clear-eyed about: in the development compose stack this
# container mounts the host's Docker socket, which is root-equivalent on the
# host. That is why the worker runs no submission code in its own process and
# why the API cannot reach a container runtime at all. Production uses a
# rootless daemon on an isolated host pool — see DEPLOYMENT.md §2.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# The Docker CLI. The worker shells out to it rather than using the Python SDK:
# the flags in sandbox.py are the security control, and `docker create ...`
# written literally is something a reviewer can read and compare against
# `docker run --help`. An SDK call graph is not.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The worker imports sentinel_api models to write results, so it needs the API
# package installed — but it never imports the FastAPI app and never serves HTTP.
COPY apps/api/pyproject.toml /app/apps/api/pyproject.toml
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -e /app/apps/api

COPY apps/api /app/apps/api
COPY workers/judge /app/workers/judge
COPY infrastructure/security /app/infrastructure/security

ENV PYTHONPATH=/app/apps/api:/app/workers/judge:/app

# Not root. The socket mount is the dangerous part, and group membership for it
# is granted by the compose file rather than by running as uid 0.
RUN useradd --uid 10002 --create-home judge && chown -R judge:judge /app
USER judge

CMD ["python", "-m", "sentinel_judge.worker"]
