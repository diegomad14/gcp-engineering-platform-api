# Engineering Platform UI Architecture

## Technology

- **React 18** + **TypeScript** — component framework
- **Vite** — build tool and dev server
- **Recharts** — charting library for cost and metrics visualizations
- **React Router v6** — client-side routing

## Component Tree

```
App
├── Layout (sidebar navigation)
│   ├── NavLink: Dashboard
│   ├── NavLink: Services
│   ├── NavLink: Costs
│   ├── NavLink: Metrics
│   ├── NavLink: Quality
│   ├── NavLink: Releases
│   └── NavLink: Service Factory
└── Routes
    ├── DashboardPage
    │   ├── StatTile (4x: services, releases, cost, quality)
    │   ├── Recent Releases list
    │   ├── Top Cost Services list
    │   └── External Links
    ├── ServicesPage
    │   └── Table (service, repository, owner, environment, quality)
    ├── ServiceDetailPage
    │   └── Grid (Cloud Run state, traffic, validation, quality, finops)
    ├── CostsPage
    │   ├── View toggle (summary / by-service)
    │   ├── Stat tiles (total cost, credits, net cost, period)
    │   ├── BarChart (Recharts)
    │   └── Detail table
    ├── MetricsPage
    │   └── Grid of service metric cards
    ├── QualityPage
    │   └── Per-service quality gate cards and checks
    ├── ReleasesPage
    │   └── Table (service, repository, version, revision, action, run)
    └── ServiceFactoryPage
        ├── Form (service info, GCP config, ownership, validation targets)
        ├── Template catalog
        └── Generated plan display (YAML, checklist, caller workflows)
```

## Pages

| Page | API Call | Key Data |
|------|----------|----------|
| Dashboard | `getServices`, `getReleaseSummary`, `getCostSummary`, `getQuality` | Aggregated stats |
| Services | `getServices` | Independent service list |
| ServiceDetail | `getService(serviceName)` | Single service and Cloud Run state |
| Costs | `getCostSummary`, `getCostByService` | Costs with charts |
| Metrics | `getMetrics` | Cloud Run metrics per service |
| Quality | `getQuality` | Latest normalized report per service |
| Releases | `getReleaseSummary` | Release history |
| Service Factory | `getTemplates`, `generatePlan` | Template catalog, plan generation |

## Design Decisions

- **No authentication in MVP.** Production will require IAP/OAuth.
- **No deploy button.** All actions are read-only or generate artifacts.
- **Incremental loading.** Each page fetches its own data on mount.
- **Error handling.** API errors are caught and displayed, not swallowed.
- **Responsive layout.** Grid-based layouts with flex wrap for smaller screens.

## Security

- No secrets, tokens, or credentials in the frontend.
- API proxy via nginx in production (Dockerfile).
- Vite dev proxy for local development.
- CORS configured server-side (FastAPI), not client-side.
