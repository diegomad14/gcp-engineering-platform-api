# Engineering Platform API agent checks

Before declaring a change complete:

1. Run the repository quality gate from `.github/workflows/ci.yml` locally where practical.
2. Push the PR branch and wait for `quality`, `workflows`, `validate`, and `SonarCloud Code Analysis`.
3. Query the exact SonarCloud PR result:

   ```bash
   python scripts/quality/sonar_agent_check.py --pull-request <PR_NUMBER>
   ```

4. Fix every new vulnerability and every issue that leaves the Quality Gate red. Do not suppress or mark an issue false-positive without an explicit, documented technical reason.

The checker reads `SONAR_TOKEN` when provided; otherwise it retrieves
`sonarcloud-api-maintenance` from Secret Manager in `cgm-assistant-prod`. Never
print, copy into Git, or document the token value.
