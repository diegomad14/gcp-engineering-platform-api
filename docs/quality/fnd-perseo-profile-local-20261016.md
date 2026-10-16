# Perfil FND Perseo: integración local sobre main

Base `0170013567b80f6cd13a4c2e477f6c1276538229`, rama aislada
`integrate/fnd-platform-20261016`. Se integra únicamente el delta FND de
`22c16918f84d50fbd0e57b76723282fa40c83ad0`; el commit recuperado original permanece
intacto en otro worktree. Su antecedente `eb12835` no se incorpora: el main moderno
ya usa el prefijo corto `c-` y contiene cambios posteriores del ejecutor.

El backend y el ejecutor declaran únicamente para
`cgm-artemis-fnd-observation-worker`:

- `PERSEO_DB_GCS_BUCKET=cgm-artemis-data`
- `PERSEO_DB_GCS_OBJECT=kpi_cgm.db`

La whitelist permite sólo esos pares exactos en ese recurso. No permite activar
el publicador, elegir otro objeto/bucket ni extender la env a otros recursos.
El mecanismo existente conserva `--update-env-vars`, la definición previa y el
rollback. No se cambian schedules, tiempos, retries, identidades, secretos o flags.
Este candidato local no modifica ninguna definición real.

Se compararon los 20 perfiles con el main actual: sólo FND cambia. Backend y
ejecutor coinciden en los fingerprints:

- Baseline: `1c575946100b89b87eaee41ff794915407b12c2519e1de90da3df01024628436`.
- FND nuevo: `7ac521cae558f4a84da4070c77356f90d96a3ad7a0a8677ad181bb8cf5ec4048`.

El workflow moderno verifica además el SHA-256 del código del ejecutor antes de
montar credenciales en fases staged. Los dos pins locales se alinean al archivo
integrado: `1b86dffecd414fe3840c0e248cadc41f7c1171321e24c96705a3975430ced09e`.
Se conserva el rechazo de código antiguo/manipulado y todos los guards. No se
cambia un digest de imagen ni una variable de repositorio. Una activación futura
necesitará construir/publicar por el proceso autorizado una imagen con ese código
y coordinar sus pins y fingerprint; este encargo no ejecuta esa activación.

## Validación local y límites

- 55 contratos focales aprobados: aprobación staged, bootstrap, perfiles,
  fingerprints, whitelist negativa, deploy/rollback simulado y tooling.
- 18 contratos de comandos de perfiles aprobados con el venv en `PATH`.
- Mypy de `src`: aprobado, 89 archivos. Ruff check/format de fuentes, tests,
  scripts de release, action de calidad y ejecutor: aprobado.
- Bandit high/critical de `src` y del ejecutor: sin hallazgos.
- Las suites completas, cobertura, JUnit y comparación independiente de perfiles
  se registran en la entrega cloud junto con las incidencias iniciales de entorno.
  La evidencia histórica del paquete no se presenta como una ejecución nueva.

No se ejecutaron push, PR, CI, builds, deploy ni llamadas a GCP para esta
integración. `AGENTS.md` exige el gate exact-commit `oss-v2` para una release;
ese gate no se ejecuta ni se declara aprobado bajo este encargo exclusivamente
local. No se redujeron umbrales ni se suprimieron hallazgos.
