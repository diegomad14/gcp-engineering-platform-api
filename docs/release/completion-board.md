# Tablero de completitud local del motor de release

Este tablero pertenece a la sesión Lead. Registra trabajo local revisable y no
autoriza publicación, despliegue, tráfico, migraciones, IAM, dispatches ni
cambios administrativos. La activación remota continúa fijada en `False` y las
fases 0–5 no se declaran cerradas por este documento.

| ID | Tanda | Repo/worktree | Base | Propietario | Archivos exclusivos | Depende de | Aceptación | Estado | Commit/patch | Evidencia/bloqueo |
|---|---|---|---|---|---|---|---|---|---|---|
| T1 | Control + emulador | API / `.reconciled-engine-api-final` | `6f502ca8ca65cf7de019ac17c22ceadde57e7542` | Lead | `scripts/release/firestore-control-integration`, `tests/integration_firestore_control.py`, control compartido | SDK oficial + emulador local | Dos API, dos clientes, consumo/lease/UNKNOWN/restart/fencing y cero efectos remotos | VERIFIED_LOCAL | `e8abc802fbe2496cf3c9a2abb5ed78b0c4ef4ae0` | `release-control-evidence/e8abc802fbe2496cf3c9a2abb5ed78b0c4ef4ae0/`; Firestore productivo y Actions siguen bloqueados |
| T2-B | Registro + recuperación | `/private/tmp/cgm-release-t2-backend` | `e8abc802fbe2496cf3c9a2abb5ed78b0c4ef4ae0` | cgm_backend | routers/stores de release y pruebas focalizadas | T1 integrado | Identidad, idempotencia, conflictos SHA/digest/revisión y recuperación durable | VERIFIED_LOCAL | `1a572fe` + gate final | Firestore multi-servicio reservado en una transacción; 35 focalizadas del agente y suite final |
| T2-P | Providers locales + lifecycle | `/private/tmp/cgm-release-t2-platform` | `e8abc802fbe2496cf3c9a2abb5ed78b0c4ef4ae0` | cgm_platform | lifecycle/CLI, fixtures locales y pruebas focalizadas | T1 integrado | Dry-run y fixtures reproducen publicación/candidate/promote/rollback/resume sin repetir efectos | VERIFIED_LOCAL | `53f87cb` | HTTP loopback y subprocess sintético; `78` focalizadas; no build/push/deploy |
| T2-R | Revisión de carreras | `/private/tmp/cgm-release-t2-review` | `e8abc802fbe2496cf3c9a2abb5ed78b0c4ef4ae0` | cgm_reviewer | ninguno | SHA T1 | Hallazgos reproducibles con archivo/línea y frontera cubierta | REVIEWED | `6e02bf` | P1: lifecycle aún no conecta lease/intent; P2: build local y SanPlat coordinado siguen pendientes/bloqueados |
| T3 | SanPlat | API/Web según contrato | posterior a T2 | Lead + propietarios asignados | adaptador corporativo y pruebas aisladas | proceso corporativo y ventana concreta | Contrato de grupo, orden operativo, interrupciones y rechazo genérico | BLOCKED | — | Sin adaptador revisado, autorización real ni ventana corporativa |
| T4 | Adopción + transición | API/Web | posterior a T2/T3 | Lead | UI mínima, runbooks y diff administrativo | contratos integrados | Evidencia final, transición sin doble ejecución y checks preservados | PENDING | — | No se aplican workflows, triggers, checks ni permisos |

## Condición de integración

Solo el Lead integra commits después de revisar diff, archivos permitidos y
pruebas. Cualquier cambio posterior al gate canónico invalida la evidencia del
SHA anterior y exige nueva acreditación. `UNIT_DOUBLE`, `LOCAL_FIXTURE`,
`LOCAL_EMULATOR` y `LIVE` se reportan por separado; ningún fixture o emulador
acredita la frontera productiva.
