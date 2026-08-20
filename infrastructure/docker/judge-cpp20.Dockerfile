# Sentinel judge sandbox — C++20 (GCC).
#
# Larger than the Python image because a toolchain is large. Note that the
# *compiler* runs inside this sandbox too: a template that expands forever is an
# attack on the judge that never reaches the run step, and compiling on the host
# would put GCC outside every control in sandbox.py.
FROM gcc:13-bookworm

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true
RUN useradd --uid 65534 --no-create-home --shell /usr/sbin/nologin sandbox 2>/dev/null || true
USER 65534:65534
WORKDIR /box
CMD ["/usr/bin/g++", "--version"]
