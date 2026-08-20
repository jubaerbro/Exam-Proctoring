# Sentinel judge sandbox — Python 3.11.
#
# This image is what untrusted candidate code executes inside. Everything in it
# is reachable by a submission, so the rule is: nothing that is not needed to
# run a program.
#
# It deliberately contains NO judge code. The runner drives it from outside via
# the container runtime, so a submission that fully compromises this image still
# has no access to the queue, the database, or the scoring logic.
FROM python:3.11-slim-bookworm

# coreutils supplies `cp`, which the sandbox wrapper uses to copy the submission
# from the read-only /src mount onto the writable tmpfs. Without it every run
# fails at staging — see `sandbox.STAGING_FAILED_MARKER`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends coreutils \
 && rm -rf /var/lib/apt/lists/* \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# The setuid strip above is belt-and-braces: the runner already passes
# --cap-drop=ALL and no-new-privileges, so a setuid binary could not escalate.
# Removing them means a future misconfiguration has nothing to reach for.

# The runner overrides all of this (--user, --workdir, --read-only), but a
# sensible default means the image is not dangerous if run by hand.
RUN useradd --uid 65534 --no-create-home --shell /usr/sbin/nologin sandbox 2>/dev/null || true
USER 65534:65534
WORKDIR /box
CMD ["/usr/local/bin/python3.11", "-c", "print('sentinel judge sandbox')"]
