"""Cloud Run service — read-only operational data.

MVP: Returns mock data. No mutations, no deploy, no traffic changes.
"""


def get_service_status(service_name: str, project_id: str, region: str) -> dict:
    """Return read-only status for a Cloud Run service.

    This is a read-only inspection. It does NOT deploy, update traffic,
    or modify any GCP resource.
    """
    return {
        "service_name": service_name,
        "project_id": project_id,
        "region": region,
        "status": "READY",
        "latest_revision": f"{service_name}-00001-abc",
        "traffic": [],
        "note": "Mock data — real implementation requires Cloud Run API read access",
    }
