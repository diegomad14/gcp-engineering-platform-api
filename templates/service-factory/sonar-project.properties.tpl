# DEPRECATED: retained only for repositories that have not migrated.
# SonarQube Cloud project configuration — legacy template.
# Required: sonar.organization and sonar.projectKey
# These values cannot be set in the SonarCloud UI.

sonar.organization={{SONAR_ORGANIZATION}}
sonar.projectKey={{SONAR_PROJECT_KEY}}
sonar.projectName={{APP_NAME}}
sonar.sources=src
sonar.sourceEncoding=UTF-8
sonar.python.version=3.11

# Optional: exclude test files from coverage
# sonar.coverage.exclusions=tests/**,**/test_*.py

# Optional: quality gate timeout (seconds) for blocking mode
# sonar.qualitygate.timeout=300
