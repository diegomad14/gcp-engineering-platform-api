# This image is built once by the bootstrap runbook and referenced by digest.
# It deliberately contains deployment tooling only; quality gates belong to the
# release workflow that produced the tag, never to a fallback deployment.
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:stable

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io git python3 \
    && rm -rf /var/lib/apt/lists/*

COPY release_executor.py /opt/eng-platform/release_executor.py
RUN chmod 0555 /opt/eng-platform/release_executor.py

ENTRYPOINT ["python3", "/opt/eng-platform/release_executor.py"]
