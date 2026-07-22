# The sandbox image. Deliberately small: this is where model-authored code runs, so every extra
# package is extra surface. Built locally by scripts/setup-sandbox.sh — there is no registry to
# publish, sign or trust.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# A non-root user inside the container. --userns=keep-id maps this to the invoking host user, so
# files the agent creates in the bind-mounted workspace are owned by the operator, not by root.
RUN useradd --create-home --shell /bin/bash argus
USER argus
WORKDIR /home/argus

CMD ["sleep", "infinity"]
