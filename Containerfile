# The sandbox image. Deliberately small: this is where model-authored code runs, so every extra
# package is extra surface. Built locally by scripts/setup-sandbox.sh — there is no registry to
# publish, sign or trust.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Deliberately NO fixed non-root user baked in here. An earlier version created a hardcoded
# `argus` account at uid 1000 and set `USER argus`, reasoning that --userns=keep-id (see
# engine/sandbox/podman.py) would map it onto the invoking host user. That only actually held up
# when the operator's own host uid HAPPENED to be 1000 — on any other machine, keep-id maps the
# real host uid onto the SAME numeric uid inside the namespace, which is a different number than
# the image's hardcoded 1000, so the baked-in "argus" account (still uid 1000) corresponded to some
# unrelated subordinate uid on the host — not the operator, and not the owner of the bind-mounted
# workspace directory. Every write from inside the container then failed with EPERM.
#
# The uid that matters is only known at RUN time (whoever is actually running Argus), so it has to
# come from the runtime side, not the image: podman.py passes `--user <host-uid>:<host-gid>`
# alongside `--userns=keep-id`. That makes the container process run as EXACTLY the invoking user's
# own identity, and keep-id's entire purpose is mapping that identity 1:1 onto the same uid/gid on
# the host — so the process already owns whatever it creates in the bind mount, on any machine,
# without the image needing to guess a uid in advance.
#
# We still want a stable $HOME (pip, git, etc. expect one) that works for an arbitrary uid with no
# /etc/passwd entry, hence a world-writable directory (sticky bit so a future multi-uid scenario
# can't let one uid delete another's files) rather than one owned by a single named account.
RUN mkdir -p /home/argus && chmod 1777 /home/argus
ENV HOME=/home/argus
WORKDIR /home/argus

CMD ["sleep", "infinity"]
