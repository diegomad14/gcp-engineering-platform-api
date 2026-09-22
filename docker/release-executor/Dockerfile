# This image is built once by the bootstrap runbook and referenced by digest.
# It deliberately contains deployment tooling only; quality gates belong to the
# release workflow that produced the tag, never to a fallback deployment.
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:alpine

RUN apk add --no-cache docker-cli git python3 \
    && adduser -D -u 65532 -G root release-executor \
    && install -d -o release-executor -g root /tmp/release-executor

COPY release_executor.py /opt/eng-platform/release_executor.py
RUN chmod 0555 /opt/eng-platform/release_executor.py

ENV HOME=/tmp/release-executor
# Cloud Build needs its standard root build identity to use the builder-owned
# credential mount and Docker socket. This image is not a service: it runs one
# short-lived, allowlisted release command. GitHub Actions overrides the user
# and group at runtime and executes the same digest unprivileged.
# trivy:ignore:DS-0002
USER root  # nosemgrep: dockerfile.security.last-user-is-root.last-user-is-root

ENTRYPOINT ["python3", "/opt/eng-platform/release_executor.py"]
