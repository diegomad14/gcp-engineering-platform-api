# GitHub Actions WIF

GitHub Actions uses Workload Identity Federation with a dedicated deployer
service account. Release workflows run only on `ubuntu-latest`; no runner label
or user-selectable runner variable is accepted. The backend may select Cloud Build after
GitHub's private-minute quota is exhausted, but that choice is not supplied by
the workflow caller or the web UI.

Scope the WIF provider to the expected repository and workflow identity. The
Cloud Build executor uses a separate least-privilege service account; neither
path may use a service-account key or the default Compute Editor account.
