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
from pathlib import Path

from eng_platform_api.models import ServiceFactoryRequest
from eng_platform_api.services.service_factory import generate_plan

OUTPUT_DIR_KEY = "SERVICE_FACTORY_OUTPUT"


def generate(args: argparse.Namespace) -> dict[str, str]:
    """Use the same OSS-only generator as the API and UI."""
    plan = generate_plan(
        ServiceFactoryRequest(
            repository=args.repository or f"{args.owner}/{args.app_name}",
            service_name=args.service_name,
            service_type=args.service_type,
            runtime=args.runtime,
            gcp_project=args.gcp_project,
            region=args.region,
            owner=args.owner,
            cost_center=args.cost_center,
            environment=args.environment,
            cloud_run_service_name=args.cloud_run_service_name,
            health_path=args.health_path,
            openapi_path=args.openapi_path,
            quality_working_directory=args.working_directory,
            coverage_threshold=args.coverage_threshold,
            validation_targets=[
                t.strip() for t in args.validation_targets.split(",") if t.strip()
            ],
        )
    )
    return {
        "gcp-service-release.yaml": plan.yaml_contract,
        ".github/workflows/ci.yml": plan.caller_pr_check,
        ".github/workflows/platform-deploy.yml": plan.platform_deploy_workflow,
        ".github/workflows/platform-rollback.yml": plan.platform_rollback_workflow,
        ".github/workflows/semantic-release.yml": plan.semantic_release_workflow,
        f"catalog/services/{args.service_name}.yaml": plan.catalog_entry,
        ".quality-gate.yml": plan.quality_config,
        f"{args.working_directory.rstrip('/')}/.quality-sources.json": plan.quality_sources,
        "cloud-run-service-labels.yaml": plan.labels_manifest,
        "onboarding-checklist.md": "# Onboarding checklist\n\n"
        + "\n".join(f"- [ ] {step}" for step in plan.checklist)
        + "\n",
        "agent-handoff-prompt.md": plan.agent_prompt,
    }


def main():
    parser = argparse.ArgumentParser(description="Engineering Platform Service Factory")
    parser.add_argument(
        "--app-name", required=True, help="Application name (kebab-case)"
    )
    parser.add_argument("--service-name", required=True, help="Cloud Run service name")
    parser.add_argument(
        "--service-type", required=True, choices=["api", "web", "worker", "integration"]
    )
    parser.add_argument(
        "--runtime", required=True, choices=["python", "node", "static"]
    )
    parser.add_argument("--gcp-project", required=True, help="GCP project ID")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--owner", required=True, help="Owning team")
    parser.add_argument("--cost-center", required=True, help="Cost center code")
    parser.add_argument(
        "--environment", default="prod", choices=["prod", "staging", "dev"]
    )
    parser.add_argument("--cloud-run-service-name", default="")
    parser.add_argument("--health-path", default="/health")
    parser.add_argument("--openapi-path", default="/openapi.json")
    parser.add_argument("--sonar-project-key", default="", help="Deprecated; ignored")
    parser.add_argument("--sonar-organization", default="", help="Deprecated; ignored")
    parser.add_argument(
        "--validation-targets", default="", help="Comma-separated external system names"
    )
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--repository", default="", help="GitHub owner/repository")
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--coverage-threshold", type=float, default=70)

    args = parser.parse_args()

    output_dir = Path(os.environ.get(OUTPUT_DIR_KEY, args.output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    files = generate(args)

    for filename, content in files.items():
        filepath = output_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)
        print(f"  Created: {filepath}")

    print(f"\nGenerated {len(files)} files in {output_dir}")
    print("No GCP resources were created. No IAM was modified.")
    print("Review and customize the generated files before use.")


if __name__ == "__main__":
    main()
