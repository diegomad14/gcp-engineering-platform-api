#!/usr/bin/env python3
"""Service Factory — generate onboarding artifacts for new services.

Usage:
    python scripts/service_factory.py --app-name my-app --service-name my-api \\
        --service-type api --runtime python --gcp-project my-project \\
        --owner my-team --cost-center cc-001

Does NOT create GCP resources, IAM bindings, or secrets.
"""

import argparse
import os
import sys
from pathlib import Path

TPL_DIR = Path(__file__).resolve().parent.parent / "templates" / "service-factory"
OUTPUT_DIR_KEY = "SERVICE_FACTORY_OUTPUT"


def render(template_text: str, vars_: dict) -> str:
    """Simple {{VAR}} template rendering."""
    result = template_text
    for key, value in vars_.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


def generate(args: argparse.Namespace) -> dict[str, str]:
    """Generate all onboarding artifacts. Returns dict of filename -> content."""
    vars_ = {
        "APP_NAME": args.app_name,
        "SERVICE_NAME": args.service_name,
        "SERVICE_TYPE": args.service_type,
        "RUNTIME": args.runtime,
        "GCP_PROJECT": args.gcp_project,
        "REGION": args.region,
        "OWNER": args.owner,
        "COST_CENTER": args.cost_center,
        "ENV": args.environment,
        "CLOUD_RUN_SERVICE_NAME": args.cloud_run_service_name or args.service_name,
        "HEALTH_PATH": args.health_path,
        "OPENAPI_PATH": args.openapi_path,
        "SONAR_PROJECT_KEY": args.sonar_project_key or f"{args.owner}_{args.app_name}",
        "SONAR_ORGANIZATION": args.sonar_organization or "<ORG>",
        "REPOSITORY": f"<ORG>/{args.app_name}",
        "VALIDATION_TARGETS": "",
    }

    if args.validation_targets:
        targets = args.validation_targets.split(",")
        vars_["VALIDATION_TARGETS"] = "\n".join(
            f"    - name: {t.strip()}\n"
            f"      path: /health\n"
            f"      expected_status: 200\n"
            f"      pii_safe: true\n"
            f"      external_source: true"
            for t in targets
        )

    output = {}

    # Release contract
    tpl_path = TPL_DIR / "gcp-application-release.yaml.tpl"
    if tpl_path.exists():
        output[f"{args.app_name}-release.yaml"] = render(tpl_path.read_text(), vars_)

    # Labels manifest
    tpl_path = TPL_DIR / "cloud-run-service-labels.yaml.tpl"
    if tpl_path.exists():
        output["cloud-run-service-labels.yaml"] = render(tpl_path.read_text(), vars_)

    # SonarQube project properties
    tpl_path = TPL_DIR / "sonar-project.properties.tpl"
    if tpl_path.exists():
        output["sonar-project.properties"] = render(tpl_path.read_text(), vars_)

    # Onboarding checklist
    checklist = f"""# {args.app_name} — Onboarding Checklist

1. Create Cloud Run service: `{vars_['CLOUD_RUN_SERVICE_NAME']}` in `{args.gcp_project}` (`{args.region}`)
2. Apply labels from `cloud-run-service-labels.yaml`
3. Create Artifact Registry repository (if not exists)
4. Create runtime SA: `{args.service_name}-runtime@{args.gcp_project}.iam.gserviceaccount.com`
5. Create deployer SA with WIF/OIDC binding for GitHub Actions
6. Add `SONAR_TOKEN` to GitHub secrets (if SonarQube enabled)
7. Add `GCP_WIF_PROVIDER`, `GCP_WIF_SERVICE_ACCOUNT`, `GCP_PROJECT_ID`, `GCP_REGION` to GitHub vars
8. Copy generated caller workflows to `.github/workflows/` in app repo
9. Copy `{args.app_name}-release.yaml` to app repo
10. Open PR with generated artifacts
11. Complete platform adoption readiness checklist (docs/checklists/)
12. Schedule platform review
"""
    output["onboarding-checklist.md"] = checklist

    # SonarQube properties
    sonar_props = f"""sonar.organization=<ORG>
sonar.projectKey={vars_['SONAR_PROJECT_KEY']}
sonar.projectName={args.app_name}
sonar.sources=src
sonar.sourceEncoding=UTF-8
"""
    output["sonar-project.properties"] = sonar_props

    return output


def main():
    parser = argparse.ArgumentParser(description="Engineering Platform Service Factory")
    parser.add_argument("--app-name", required=True, help="Application name (kebab-case)")
    parser.add_argument("--service-name", required=True, help="Cloud Run service name")
    parser.add_argument("--service-type", required=True, choices=["api", "web", "worker", "integration"])
    parser.add_argument("--runtime", required=True, choices=["python", "node", "static"])
    parser.add_argument("--gcp-project", required=True, help="GCP project ID")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--owner", required=True, help="Owning team")
    parser.add_argument("--cost-center", required=True, help="Cost center code")
    parser.add_argument("--environment", default="prod", choices=["prod", "staging", "dev"])
    parser.add_argument("--cloud-run-service-name", default="")
    parser.add_argument("--health-path", default="/health")
    parser.add_argument("--openapi-path", default="/openapi.json")
    parser.add_argument("--sonar-project-key", default="")
    parser.add_argument("--sonar-organization", default="", help="SonarQube Cloud organization key")
    parser.add_argument("--validation-targets", default="", help="Comma-separated external system names")
    parser.add_argument("--output-dir", default=".")

    args = parser.parse_args()

    output_dir = Path(os.environ.get(OUTPUT_DIR_KEY, args.output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    files = generate(args)

    for filename, content in files.items():
        filepath = output_dir / filename
        filepath.write_text(content)
        print(f"  Created: {filepath}")

    print(f"\nGenerated {len(files)} files in {output_dir}")
    print("No GCP resources were created. No IAM was modified.")
    print("Review and customize the generated files before use.")


if __name__ == "__main__":
    main()
