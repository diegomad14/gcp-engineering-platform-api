# Release local y trazable

Estado: fases 0–5 están implementadas como contratos revisables y dry-run;
ninguna fase se declara cerrada. Las mutaciones remotas siguen bloqueadas por
falta de un adaptador revisado de exclusión compartida y autorización previa.
El lifecycle está documentado en `docs/release/local-lifecycle.md`, 2026-09-09.

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

Un árbol sucio se rechaza para plan, calidad y preparación. `prepare
--allow-dirty` existe únicamente para inspección local no publicable y deja
`source.publishable: false` en el manifiesto.

## Evidencia y reutilización

El perfil Python/Node requiere la política `oss-v2`: SHA de código exacto,
`base_sha` exacto, cobertura diferencial de líneas ejecutables de al menos 80%,
toolchain compatible y vigencia máxima de 168 horas. El runner existente
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

El esquema del manifiesto está en
`schemas/local-release-manifest.schema.json`. Los estados remotos no se
simulan: la fase 1 solo registra `remote_effects: []` y deja las etapas
posteriores en `pending`.

## Frontera con las siguientes fases

La publicación de tag/release de GitHub, el push de Artifact Registry, el
registro en Engineering Platform, el candidate Cloud Run, la promoción y el
rollback están implementados como comandos separados y guardados en
`local-lifecycle.md`; todos quedan en dry-run. El modo live además exige un
adaptador revisado de exclusión CLI/CLI y CLI/Actions, autorización previa
ligada a identidad y una aprobación explícita; mientras esos adaptadores no
existan, el motor falla cerrado. Para SanPlat se debe conservar la ventana corporativa completa:
captura de estado, preparación, autorización, maintenance/pause, drain,
migraciones aplicables, promoción de la pareja exacta, validación funcional real
y reanudación de lo que estaba activo.

El runner local bajo `scripts/ops/local-release-runner/` es una contingencia
para Actions bloqueado y no se reemplaza ni se confunde con este motor
primario de release local.
