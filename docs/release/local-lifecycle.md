# Ciclo local completo: fases 2 a 5

Este documento describe la continuación de la fase 1. El CLI sigue siendo
local por defecto: `publish`, `candidate`, `promote`, `rollback`, `register`,
`sanplat` y `adopt` generan un plan y guardan un intento local. Una mutación
requeriría simultáneamente `--execute`, `--confirm-remote-effects`, exclusión
compartida y autorización previa; promoción y rollback requieren además
`PROMOTE_PROD` o `ROLLBACK_PROD`. Ninguna fase 0–5 se considera cerrada. Como
los dos adaptadores aún no están configurados, el modo live falla cerrado.

## Controles que siguen pendientes

`release_lifecycle.live_control_plan()` deja explícitos los dos cruces que no
se deben inferir del lock local:

- CLI/CLI: el contrato de lease durable ya está implementado sobre el estado
  local bloqueado o Firestore configurado; la verificación contra dos hosts aún
  no está aceptada.
- CLI/Actions: falta un handshake que coordine esa lease con la concurrencia,
  dispatch y estado de los workflows protegidos.

El adaptador local consume una autorización firmada por Engineering Platform y
la liga a actor, `release_id`, repositorio, SHA, tag, digest, destino,
operación y configuración. La autorización no puede emitirse desde el CLI, una
opción local, un manifiesto ni un token de calidad. Los endpoints de control
solo guardan estado: no contienen callbacks que ejecuten efectos externos.

Las claves de exclusión son explícitas: `publication:<repository>:<policy>`
para reservar versión/publicación y `deployment:<service>:<environment-or-group>`
para reservar un destino canónico. La adquisición, renovación y liberación
validan propietario, generación y versión; una lease vencida queda sin takeover
automático. Solo una observación reconciliada `NOT_STARTED` permite un nuevo
propietario; `COMPLETE` e `INDETERMINATE` no reenvían el efecto. La intención se
registra antes de actuar y `UNKNOWN` exige reconciliación, sin reintento
automático.

La implementación conserva las protecciones actuales de Actions y el camino de
autorización del control plane. Los comandos genéricos de candidate, promote y
rollback rechazan manifiestos SanPlat; solo un adaptador SanPlat revisado y una
ventana corporativa pueden desbloquear esa coordinación. La activación remota
del lifecycle permanece deshabilitada y no existe un bypass del CLI.

La API expone el contrato interno en:

- `POST /api/internal/release-execution/authorizations/consume`
- `POST /api/internal/release-execution/leases/{acquire,renew,release,reconcile}`
- `POST /api/internal/release-execution/intents`
- `POST /api/internal/release-execution/intents/{result,reconcile}`

El store local usa `ENG_PLATFORM_RELEASE_CONTROL_STORE_PATH` y bloqueo de
archivo; es una persistencia durable de un solo host para desarrollo, no una
prueba de exclusión multi-host. Para producción se debe configurar la
colección existente mediante `ENG_PLATFORM_RELEASE_CONTROL_FIRESTORE_COLLECTION`
y conservar el consumo durable de autorizaciones en Firestore.

## Fase 2 — Publicación idempotente

```bash
./scripts/release/local-release publish \
  --manifest ~/.local/state/cgm-release/manifests/<release-id>.json \
  --json
```

El plan usa un único publicador (`git` + `docker` + `gh`) y, antes de repetir
un efecto, reconcilia:

1. digest existente en Artifact Registry;
2. tag remoto y su SHA completo;
3. GitHub Release asociada al tag.

El modo live no debe hacer push de `main`: publica solo el tag exacto y usa
`gh release create --verify-tag`. Si una respuesta se pierde, el siguiente
intento consulta primero el estado real. La creación real queda condicionada a
una aprobación independiente porque puede activar otros workflows del
repositorio. El plan también deja una auditoría de solo lectura para `push`,
`create`, `release`, `deployment`, `deployment_status` y `workflow_run`; usar
`gh` no suprime los efectos encadenados de GitHub Actions.

## Fase 3 — Candidate, registro, promoción y rollback

```bash
./scripts/release/local-release candidate \
  --manifest ~/.local/state/cgm-release/manifests/<release-id>.json \
  --json

./scripts/release/local-release register \
  --manifest ~/.local/state/cgm-release/manifests/<release-id>.json \
  --status candidate \
  --platform-api-url "$ENG_PLATFORM_API_URL" \
  --json

./scripts/release/local-release promote \
  --manifest ~/.local/state/cgm-release/manifests/<release-id>.json \
  --confirm PROMOTE_PROD \
  --json

./scripts/release/local-release rollback \
  --manifest ~/.local/state/cgm-release/manifests/<release-id>.json \
  --target-revision <known-good-revision> \
  --confirm ROLLBACK_PROD \
  --json
```

El candidate se despliega directamente con `gcloud run deploy --image
<image>@sha256:<digest> --no-traffic`; no usa `--source`, Cloud Build ni
Actions. La promoción verifica `Ready=True`, captura el tráfico actual y mueve
100% únicamente a la revisión candidate registrada. Si la conexión se pierde
después del cambio, el intento queda `UNKNOWN` y se exige reconciliación; no se
re-promueve automáticamente.

El registro envía `release_id`, `source_sha` y `artifact_digest` a
`POST /api/releases/`. El control plane conserva esos campos y hace idempotente
el mismo release por servicio. Un registro local no tiene `github_run_url`: la
UI lo debe tratar como `Manual/untracked` hasta que exista una representación
oficial de la ejecución local.

Rollback solo cambia tráfico a una revisión conocida y saludable. No crea tags,
no reconstruye la imagen y no revierte migraciones ni mensajes.

## Fase 4 — Coordinación SanPlat

```bash
./scripts/release/local-release sanplat \
  --api-manifest ~/.local/state/cgm-release/manifests/<api-release-id>.json \
  --web-manifest ~/.local/state/cgm-release/manifests/<web-release-id>.json \
  --release-group-id <corporate-window-id> \
  --auxiliary-service cgm-bot-api \
  --json
```

El plan conserva los dos `release_id`, los dos digests/revisiones y los
servicios auxiliares sin cambios. El orden es obligatorio:

`prepare → authorize → capture-state → maintenance → pause-deliveries → drain
→ migrations (si aplica) → promote-pair → validate-functional → resume`.

La ejecución live está bloqueada hasta registrar un adaptador revisado y una
ventana corporativa concreta. Esto evita hacer una pausa parcial o promover una
pareja sin validar Microsoft/SanPlat, persistencia y rechazo anónimo. La fuente
operativa es [[release_process]] y la política de acceso es
[[acceso-corporativo-sanplat]]. La configuración Web debe declarar `API_BASE_URL`
para la revisión candidate y no puede adoptar accidentalmente la URL candidate
como default productivo.

## Fase 5 — Adopción

```bash
./scripts/release/local-release adopt \
  --service eng-platform-api \
  --repo-path /ruta/al/checkout \
  --json
```

El Service Factory también genera `.cgm/local-release.yaml` desde
`templates/service-factory/local-release.yaml.tpl`. El contrato fija `oss-v2`,
80% diferencial, 168 horas de vigencia, `linux/amd64`, un publicador único y
confirmación explícita para efectos remotos.

Durante la transición, los checks protegidos de Actions siguen siendo
autoridad. Retirar semantic-release, cambiar disparadores, modificar branch
protection o añadir una acción de UI requiere un cambio independiente aprobado.
La adopción no deshabilita gates para declarar independencia.

## Reanudación y estados

Todos los comandos guardan registros bajo `CGM_RELEASE_STATE_DIR` (por defecto
`~/.local/state/cgm-release`). Cada efecto se registra como intención antes de
ejecutarse y como `CONFIRMED`, `FAILED` o `UNKNOWN` después. `resume` permite
identificar la primera etapa local segura y devuelve el comando siguiente sin
mutar nada. Después de una desconexión puede consultar los sistemas reales en
modo solo lectura:

```bash
./scripts/release/local-release resume \
  --release-id <release-id> \
  --reconcile \
  --json
```

Un estado `UNKNOWN` siempre exige esa reconciliación antes de repetir. Si el
resultado no es concluyente, el motor conserva el estado y detiene la
continuación; no reconstruye, no crea otro tag y no vuelve a promover por
defecto.

## Medición pendiente

Todavía no se declara una mejora absoluta de tiempo. Antes del primer uso
productivo se deben medir por separado preparación, calidad, build frío, build
caliente, reutilización, upload, GitHub, candidate, ventana corporativa y
validación. Los `timeout` de workflows no son duraciones observadas.
