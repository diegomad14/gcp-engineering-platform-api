FROM golang:1.27.2-bookworm

ARG TRIVY_VERSION=0.72.0
ENV GOTOOLCHAIN=local
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --break-system-packages --no-cache-dir \
        pytest-cov==7.0.0 ruff==0.15.22 semgrep==1.136.0 setuptools==80.9.0 \
    && curl --fail --location --silent --show-error \
        "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
        | tar -xz -C /usr/local/bin trivy \
    && chmod 0555 /usr/local/bin/trivy
COPY scripts/quality/quality_gate.py scripts/quality/differential_coverage.py scripts/quality/go_coverage.py scripts/quality/go_executable_lines.go /opt/eng-platform/
COPY docker/quality-executor/trivyignore.yaml /opt/eng-platform/trivyignore.yaml
COPY docker/quality-executor/quality_executor.py docker/quality-executor/quality_profiles.py docker/quality-executor/untrusted_command.py docker/quality-executor/trusted_scanner.py docker/quality-executor/trusted_scanner.sh docker/quality-executor/test_quality_executor.py /opt/eng-platform/
COPY docker/quality-executor/test_go_coverage.py /opt/eng-platform/
COPY docker/quality-executor/smoke_go_isolation.py /opt/eng-platform/
COPY src/eng_platform_api/release_quality_profiles.json src/eng_platform_api/release_quality_profiles.go.json /opt/eng-platform/
RUN go build -o /opt/eng-platform/go_executable_lines /opt/eng-platform/go_executable_lines.go \
    && mkdir -p /opt/eng-platform/trusted-bin \
    && chown 0:0 /usr/local/bin/trivy \
    && install -m 0555 /opt/eng-platform/trusted_scanner.sh /opt/eng-platform/trusted-bin/semgrep \
    && install -m 0555 /opt/eng-platform/trusted_scanner.sh /opt/eng-platform/trusted-bin/trivy \
    && chmod 0555 /opt/eng-platform/quality_executor.py /opt/eng-platform/untrusted_command.py /opt/eng-platform/trusted_scanner.py /opt/eng-platform/go_executable_lines \
    && chmod 0444 /opt/eng-platform/release_quality_profiles.json /opt/eng-platform/release_quality_profiles.go.json /opt/eng-platform/trivyignore.yaml \
    && cd /opt/eng-platform \
    && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_quality_executor.py test_go_coverage.py
ENTRYPOINT ["python3", "/opt/eng-platform/quality_executor.py"]
