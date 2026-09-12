# Release local y trazable

Estado de la continuación del 2026-09-12: releases individuales implementados,
validación final y despliegue pendientes. El servidor controla la activación
por servicio, apagada por defecto. El CLI usa una sesión autorizada y conserva
autorización, reserva, intención y resultado durables. Para servicios adoptados
se bloquea Actions desde la plataforma; no se exige coordinar API y Web.
El flujo y responsabilidades actuales están en [local-lifecycle.md](local-lifecycle.md).
La evidencia histórica no acredita el código modificado posteriormente.

El motor vive en `scripts/release/local_release.py` y solo usa la biblioteca
estándar de Python para preparar una release local reproducible. El wrapper
`scripts/release/local-release` expone el mismo CLI sin depender de una
instalación de Node, GitHub Actions, Cloud Build o un servicio externo.

## Contrato de la fase 1

La fase 1 puede:

- inspeccionar Git, herramientas y workflows existentes;
- calcular la versión SemVer y las notas desde Conventional Commits;
- validar un SHA exacto, un `base_sha` y el estado del árbol;
- ejecutar el quality gate local existente y envolverlo en evidencia `oss-v2`;
- persistir un manifiesto durable y un registro de ejecución separado;
- calcular una clave de reutilización conservadora para Docker/BuildKit;
- planear o ejecutar una imagen local con `docker buildx build --load`.

La fase 1 no publica ni despliega. No hace `git push`, no crea releases de
GitHub, no hace `docker push`, no invoca `gcloud`, no despacha Actions y no usa
Cloud Build. `build --execute` sigue siendo local: carga la imagen en el
daemon/BuildKit del equipo y registra `remote_published: false`.

## Flujo operativo

Desde `gcp-engineering-platform-api`:

```bash
./scripts/release/local-release doctor \
  --service eng-platform-api \
  --repo-path . \
  --json

./scripts/release/local-release plan \
  --service eng-platform-api \
  --repo-path /ruta/al/repo-del-servicio \
  --base-sha <base-sha> \
  --json

./scripts/release/local-release version \
  --service eng-platform-api \
  --repo-path /ruta/al/repo-del-servicio \
  --base-sha <base-sha> \
  --json

./scripts/release/local-release notes \
  --service eng-platform-api \
  --repo-path /ruta/al/repo-del-servicio \
  --base-sha <base-sha> \
  --json

./scripts/release/local-release quality \
  --service eng-platform-api \
  --repo-path /ruta/al/repo-del-servicio \
  --base-sha <base-sha> \
  --differential-report /ruta/a/oss-v2-differential.json \
  --json

./scripts/release/local-release prepare \
  --service eng-platform-api \
  --repo-path /ruta/al/repo-del-servicio \
  --base-sha <base-sha> \
  --json

./scripts/release/local-release build \
  --manifest ~/.local/state/cgm-release/manifests/<release-id>.json \
  --json
```

El último comando solo planea el build. Para ejecutarlo localmente se añade
`--execute`; exige que el manifiesto tenga evidencia `oss-v2` aprobada y
construye explícitamente para `linux/amd64`, pero no autoriza publicación
remota.

La reclamación del build se hace bajo el lock local y usa una identidad
determinista (repositorio, SHA, Dockerfile, contexto, dependencias, argumentos,
arquitectura, plataforma e imágenes base). Dos procesos concurrentes no
generan dos artefactos: uno construye y el otro reutiliza el registro
`<reuse_key>.json`. Los estados `BUILDING` y `UNKNOWN` requieren
`reconcile-build`; esa reconciliación solo inspecciona la imagen local y no
reconstruye ni publica.

Un árbol sucio se rechaza para plan, calidad y preparación. `prepare
--allow-dirty` existe únicamente para inspección local no publicable y deja
`source.publishable: false` en el manifiesto.

## Evidencia y reutilización

El perfil Python/Node requiere la política `oss-v2`: SHA de código exacto,
`base_sha` exacto, cobertura diferencial de líneas ejecutables de al menos 80%,
toolchain compatible y vigencia máxima de 168 horas. El alcance canónico de
Python incluye `src` y `scripts/release`; sus auxiliares ejecutables se
comprueban además con los controles estáticos aplicables. No hay exclusiones
para conseguir un resultado verde. El runner existente
también informa cobertura global, pero esa cifra no sustituye la evidencia
diferencial. Sin el reporte diferencial correcto, la evidencia queda en
`policy_status: FAILED` y no puede reutilizarse.

La reutilización de calidad exige coincidencia exacta de servicio, repositorio,
commit, base, política, estado aprobado, expiración y fingerprint de toolchain.
La reutilización de artefactos incluye repositorio/SHA, Dockerfile y
`.dockerignore`, contexto, dependencias, argumentos, arquitectura, plataforma
`linux/amd64` e imágenes base.

## Estado durable

Por defecto se usa `~/.local/state/cgm-release`; se puede cambiar con
`CGM_RELEASE_STATE_DIR`. El motor mantiene:

| Ruta | Propósito |
|---|---|
| `manifests/` | identidad y plan inmutable de la release |
| `executions/` | cada intento local, incluido reintento idempotente |
| `evidence/` | reportes de calidad y su envolvente de trazabilidad |
| `artifacts/` | evidencia de imágenes locales disponibles |
| `release.lock` | exclusión mutua local; no sustituye una exclusión compartida |

Cuando se ejecuta con fixture, cada mutación controlada añade en el registro de
ejecución su `control_intents`, lease, identidad de autorización y resultado.
Una desconexión deja `UNKNOWN`; `resume` expone el estado durable y
`reconcile_register_release` hace una consulta GET de solo lectura para
confirmar la identidad exacta sin repetir el POST.

El esquema del manifiesto está en
`schemas/local-release-manifest.schema.json`. Los estados remotos no se
simulan: la fase 1 solo registra `remote_effects: []` y deja las etapas
posteriores en `pending`.

## Frontera con las siguientes fases

La publicación de tag/release de GitHub, el push de Artifact Registry, el
registro en Engineering Platform, el candidate Cloud Run, la promoción y el
rollback están implementados como comandos separados y guardados en
`local-lifecycle.md`; el recorrido controlado solo se habilita para fixtures
explícitos y el CLI normal queda en dry-run. El modo live además exige un
handshake CLI/Actions aceptado, una autorización previa ligada a identidad y
una aprobación explícita; mientras esa integración no exista, el motor falla
cerrado. El adaptador ejecutable está en
`scripts/release/execution_control.py` y solo habla con el control plane; no
emite autorizaciones ni ejecuta proveedores. El CLI no ofrece una operación
conjunta SanPlat: todos los candidate/promote/rollback son individuales; los
historiales conjuntos heredados solo se pueden leer y un estado incierto impide
la reanudación automática.

El runner local bajo `scripts/ops/local-release-runner/` es una contingencia
para Actions bloqueado y no se reemplaza ni se confunde con este motor
primario de release local.
