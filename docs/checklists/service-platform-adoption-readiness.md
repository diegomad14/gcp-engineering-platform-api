# Service Platform Adoption Readiness Checklist

## Service Identity

- [ ] Service owner defined
- [ ] App repository identified
- [ ] GCP project identified
- [ ] Cloud Run services identified
- [ ] Runtime service account identified
- [ ] Deployer service account identified
- [ ] WIF provider identified

## Release Readiness

- [ ] Release candidate no-traffic supported
- [ ] Candidate tagged URL supported
- [ ] Manual promote supported
- [ ] Rollback command documented
- [ ] Rollback revision preserved
- [ ] No auto-promote

## Validation Readiness

- [ ] API `/health`
- [ ] Web HEAD if applicable
- [ ] OpenAPI path count if applicable
- [ ] CORS preflight if applicable
- [ ] Browser/manual validation if applicable
- [ ] Behavior-level smoke

## Security Readiness

- [ ] No service account JSON
- [ ] No `GCP_SA_KEY`
- [ ] No secrets in repo
- [ ] No PII in repo
- [ ] Private data source pattern if applicable
- [ ] Credential exposure process documented

## Migration Readiness

- [ ] Current app workflows inventoried
- [ ] Platform templates compared
- [ ] Dry-run plan defined
- [ ] No production affected in initial PR
- [ ] Rollback plan defined
