# This image is built once by the bootstrap runbook and referenced by digest.
# It deliberately contains deployment tooling only; quality gates belong to the
# release workflow that produced the tag, never to a fallback deployment.
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:alpine

RUN apk add --no-cache docker-cli git python3

COPY release_executor.py /opt/eng-platform/release_executor.py
RUN chmod 0555 /opt/eng-platform/release_executor.py

ENTRYPOINT ["python3", "/opt/eng-platform/release_executor.py"]
