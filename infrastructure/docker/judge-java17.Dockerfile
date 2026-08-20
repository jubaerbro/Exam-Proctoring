# Sentinel judge sandbox — Java 17.
#
# NOT VERIFIED. The adapter in sentinel_judge/languages.py is written and the
# JVM flags are chosen deliberately (MaxRAMPercentage so the JVM respects the
# container's memory cap rather than the host's), but no Java submission has
# been executed through this image. See docs/PHASE_REPORTS.md, Phase 3.
FROM eclipse-temurin:17-jdk-jammy

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true
RUN useradd --uid 65534 --no-create-home --shell /usr/sbin/nologin sandbox 2>/dev/null || true
USER 65534:65534
WORKDIR /box
CMD ["/opt/java/openjdk/bin/java", "-version"]
