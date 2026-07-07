"""BigQuery billing service — builds and executes cost queries.

MVP: Returns mock data. Real BigQuery integration requires:
- Cloud Billing Export enabled
- BigQuery dataset created
- Platform SA with roles/bigquery.dataViewer

Query templates are in sql/billing/.
"""

from datetime import date, timedelta
from typing import Optional

from ..config import config
from ..models import CostItem, CostPeriod, CostSummary


def _mock_cost_data() -> list[CostItem]:
    """Realistic mock cost data for development and testing."""
    return [
        CostItem(
            project_id="cgm-assistant-prod",
            app="cgm-integration-platform",
            service_name="cgm-sanplat-api",
            gcp_service="Cloud Run",
            cost=12.34,
            credits=-1.20,
            net_cost=11.14,
        ),
        CostItem(
            project_id="cgm-assistant-prod",
            app="cgm-integration-platform",
            service_name="cgm-sanplat-web",
            gcp_service="Cloud Run",
            cost=8.50,
            credits=-0.80,
            net_cost=7.70,
        ),
        CostItem(
            project_id="cgm-assistant-prod",
            app="cgm-integration-platform",
            service_name="cgm-sanplat-api",
            gcp_service="Cloud SQL",
            cost=45.00,
            credits=-5.00,
            net_cost=40.00,
        ),
        CostItem(
            project_id="cgm-assistant-prod",
            app="cgm-integration-platform",
            service_name="cgm-sanplat-api",
            gcp_service="Secret Manager",
            cost=1.20,
            credits=0.00,
            net_cost=1.20,
        ),
        CostItem(
            project_id="cgm-assistant-prod",
            app="",
            service_name="",
            gcp_service="Compute Engine",
            cost=3.50,
            credits=0.00,
            net_cost=3.50,
        ),
    ]


def build_cost_query(
    group_by: str = "service",
    days: int = 30,
    app_filter: Optional[str] = None,
) -> str:
    """Build a parametrized BigQuery query for cost data.

    The caller is responsible for executing this query against BigQuery.
    This function only builds the SQL string.
    """
    table = f"`{config.billing.bigquery_project_id}.{config.billing.bigquery_dataset}.{config.billing.bigquery_table}`"

    group_clause: str
    select_clause: str

    if group_by == "app":
        select_clause = """
        SELECT
          labels_app.value AS app,
          project.id AS project_id,
          service.description AS gcp_service,
          SUM(cost) AS cost,
          SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
          SUM(cost) + SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
        """
        group_clause = "GROUP BY app, project_id, gcp_service"
    elif group_by == "service":
        select_clause = """
        SELECT
          service.description AS gcp_service,
          resource.name AS service_name,
          project.id AS project_id,
          SUM(cost) AS cost,
          SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
          SUM(cost) + SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
        """
        group_clause = "GROUP BY gcp_service, service_name, project_id"
    elif group_by == "sku":
        select_clause = """
        SELECT
          sku.id AS sku_id,
          sku.description AS sku_description,
          service.description AS gcp_service,
          SUM(cost) AS cost,
          SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
          SUM(cost) + SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
        """
        group_clause = "GROUP BY sku_id, sku_description, gcp_service"
    else:
        select_clause = """
        SELECT
          project.id AS project_id,
          service.description AS gcp_service,
          SUM(cost) AS cost,
          SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
          SUM(cost) + SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
        """
        group_clause = "GROUP BY project_id, gcp_service"

    where_clause = f"""
        WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    """

    if app_filter:
        where_clause += f"""
          AND EXISTS (
            SELECT 1 FROM UNNEST(labels) l
            WHERE l.key = '{config.billing.app_label}' AND l.value = '{app_filter}'
          )
        """

    query = f"""
        {select_clause}
        FROM {table}
        CROSS JOIN UNNEST(labels) AS labels_app
        {where_clause}
          AND labels_app.key = '{config.billing.app_label}'
        {group_clause}
        ORDER BY net_cost DESC
    """

    return query


def get_cost_summary(days: int = 30) -> CostSummary:
    """Return cost summary. Uses mock data unless billing is configured."""
    today = date.today()
    period = CostPeriod(
        start=(today - timedelta(days=days)).isoformat(),
        end=today.isoformat(),
    )

    if not config.billing.enabled or config.mock_mode:
        items = _mock_cost_data()
    else:
        items = _mock_cost_data()  # TODO: Execute real BigQuery query

    total_cost = sum(item.cost for item in items)
    total_credits = sum(item.credits for item in items)
    total_net_cost = sum(item.net_cost for item in items)

    return CostSummary(
        currency="USD",
        period=period,
        total_cost=round(total_cost, 2),
        total_credits=round(total_credits, 2),
        total_net_cost=round(total_net_cost, 2),
        items=items,
    )


def get_cost_by_service(days: int = 30) -> CostSummary:
    """Alias for cost summary grouped by service."""
    return get_cost_summary(days=days)


def get_cost_by_app(days: int = 30) -> CostSummary:
    """Alias for cost summary grouped by app label."""
    return get_cost_summary(days=days)
