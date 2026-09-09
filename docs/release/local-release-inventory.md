# Inventario del release actual y diseño de la fase local

Fecha de corte: **2026-09-09**. Este inventario documenta el estado observado y
los contratos implementados antes de habilitar publicación o despliegue real
desde el motor local.

## Resultado del inventario

El flujo productivo existente sigue siendo GitHub Actions-first. Los repos de
servicio conservan workflows de quality, semantic release y Cloud Run, y la
plataforma reconstruye buena parte de su estado desde GitHub. El camino local
de fases 0–5 ya tiene CLI, contratos y planes, pero continúa sin ejecutar
mutaciones remotas ni activar un segundo publicador en paralelo.

| Tramo | Estado observado | Decisión fase 1 |
|---|---|---|
| Fuente | Cada servicio se identifica por `service_name`, repositorio y SHA; el catálogo estático tiene seis servicios. | Validar origen contra el catálogo y registrar SHA completo, branch y árbol sucio. |
| Versión | `cgm-sanplat-api` y `eng-platform-api` conservan semantic-release en workflows de Actions; las versiones de paquetes están fijadas. | Calcular SemVer/notas localmente desde Conventional Commits y bloquear tags locales en SHA distinto. |
| Calidad | El runner local existente produce reporte normalizado y usa cobertura global; la política canónica `oss-v2` exige además 80% diferencial. | No aceptar ni reutilizar evidencia sin SHA, base, política, toolchain y diferencial exactos. |
| Build | Los workflows construyen y publican imágenes en Artifact Registry dentro de Actions. | Calcular `reuse_key`; opcionalmente construir con BuildKit local y `--load`. |
| Publicación | Tags/releases, `docker push`, Deployments y dispatches viven en GitHub/Actions. | Fase 2 preparada con un publicador único, reconciliación y auditoría; live exige confirmación separada. |
| Candidate/promote | Cloud Run y sus revisiones son gestionados por workflows existentes. | Fase 3 preparada por digest/revisión, sin `--source`; live exige autorización y confirmación. |
| SanPlat | La operación requiere ventana corporativa y pareja API/Web coordinada. | Fase 4 conserva el orden operativo; ejecución bloqueada sin adaptador revisado y ventana concreta. |

Los valores como `timeout=1800` o `--task-timeout=1800s` encontrados en
workflows son límites configurados, no duraciones observadas. La fase 0 no
inventa tiempos de ejecución cuando no existe evidencia local o remota
capturada.

## Auditoría de bases vigentes y reconciliación

La referencia documentada se verificó contra el objeto completo, no solo contra
el prefijo corto. La propuesta revisable parte de `origin/main` después de
`git fetch origin main`; el checkout original se conserva aparte y no se
reescribe.

| Repo | HEAD documentado y verificado | `origin/main` verificado | Divergencia observada | Propuesta limpia |
|---|---|---|---:|---|
| API | `d2e8e4abbc750a4a331827ba4849fbc8ee892179` | `6f502ca8ca65cf7de019ac17c22ceadde57e7542` | 50 de `main`, 1 de la base | rama `codex/reconciled-local-release-audit-api`, base `6f502ca` |
| Web | `311eab1f2da2c9d9c793d1f1bf2a8994f64b49bc` | `8a6c13de165fbba9c4083adc625b76956f1bd399` | 20 de `main`, 1 de la base | `7c103939b4aa9547cbadc0e01fd4134c881ec6e3`, padre `8a6c13d` |

Los conteos se obtuvieron con los rangos de Git de cada repositorio y se
confirmaron antes de reconciliar. No se hicieron push, publicaciones,
despliegues, cambios administrativos ni cambios de permisos.

## Checkouts del catálogo

| Servicio | Repositorio | Checkout observado | HEAD corto | Árbol |
|---|---|---|---|---|
| `cgm-sanplat-api` | `diegomad14/cgm-sanplat-api` | `fix/fnd-orphaned-alarms` | `0620244` | limpio |
| `cgm-sanplat-web` | `diegomad14/cgm-sanplat-web` | `feat/scrum-54-local-release-fallback` | `8e72653` | limpio |
| `cgm-bot-api` | `diegomad14/cgm-bot-core` | `feat/scrum-54-local-release-fallback` | `9fa8a2a` | limpio |
| `communications-ms` | `diegomad14/communications-ms` | `feat/scrum-54-local-release-fallback` | `04d0db8` | limpio |
| `eng-platform-api` | `diegomad14/gcp-engineering-platform-api` | `fix/communications-runtime-quality` | `d2e8e4a` | checkout original sucio; cambios preservados |
| `eng-platform-api` | `diegomad14/gcp-engineering-platform-api` | `codex/reconciled-local-release-audit-api` | `HEAD` | limpio; parte de `origin/main` vigente |
| `eng-platform-web` | `diegomad14/gcp-engineering-platform-web` | `fix/scrum-54-drill-audit` | `311eab1` | checkout original preservado con cambios preexistentes |
| `eng-platform-web` | `diegomad14/gcp-engineering-platform-web` | `codex/reconciled-local-release-audit-web` | `7c10393` | limpio; parte de `origin/main` vigente |

Los checkouts originales de API y Web se preservaron. El motor y sus pruebas se
añadieron en worktrees limpios desde las bases vigentes, reaplicando solo los
cambios del motor y sin resetear, limpiar ni sobrescribir los árboles originales.

## Dependencias de workflows y límites

En `cgm-sanplat-api`, `semantic-release.yml` se dispara sobre `main` y usa
`semantic-release@25.0.7`, `@semantic-release/commit-analyzer@13.0.1`,
`@semantic-release/release-notes-generator@14.1.1` y
`@semantic-release/github@12.0.9`. `platform-deploy.yml` contiene publicación
de imagen, despliegue Cloud Run y tareas con límites configurados de hasta
1800 segundos. Ninguno de esos pasos se invoca desde la fase 1.

Los workflows actuales de los repos siguen dependiendo de `uses:` de Actions.
La propuesta no modifica workflows: conserva los checks OSS, la autorización
previa, la concurrencia por servicio y las correcciones de `origin/main`. El
workflow Sonar que permanece en el checkout original de API no se toma como
evidencia vigente ni se copia a la propuesta; la política canónica documentada
es `oss-v2`.

## Artefactos entregados

- `scripts/release/local_release.py`: CLI determinista, stdlib-only.
- `scripts/release/local-release`: wrapper operativo.
- `tests/test_local_release.py`: ocho pruebas del contrato inicial.
- `schemas/local-release-manifest.schema.json`: contrato durable del manifiesto.
- `docs/release/local-release.md`: guía de uso y límites.
- `docs/release/local-lifecycle.md`: publicación, candidate, registro,
  promoción, rollback, SanPlat, adopción y reanudación.

El motor separa manifiesto de release y ejecución. Repetir `prepare` para el
mismo servicio/repositorio/SHA reutiliza la identidad y crea otro registro de
intento. Un tag existente en otro SHA, una evidencia vencida o una diferencia
de base/toolchain bloquean la reutilización.

## Próximos gates

1. Resolver y probar la fuente de cobertura diferencial `oss-v2` en cada perfil.
2. Acordar exclusión entre operadores/Actions y habilitar publicación/registro
   con una aprobación separada y auditoría de GitHub/Artifact Registry.
3. Conectar el adapter de Deployments/estados reales para el camino local sin
   inventar un run de Actions.
4. Aportar adaptador SanPlat, configuración `API_BASE_URL` y ventana corporativa
   completa antes del piloto coordinado.
5. Medir el flujo real y retirar rutas duplicadas solo con aprobación
   administrativa.
