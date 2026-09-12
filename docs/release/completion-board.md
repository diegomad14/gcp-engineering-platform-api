# Tablero de completitud local del motor de release

> Corte de continuación: 2026-09-12. La tabla inferior conserva evidencia de la
> ronda anterior, no acredita los cambios posteriores. La continuación de
> releases individuales requiere nuevo gate exacto, emulador, revisión y
> validación productiva. No se declara terminada con esta tabla.

## Continuación integrada

| Tramo | Implementación local | Acreditación requerida |
|---|---|---|
| Control durable | Grant único por intent, fencing del resultado y reconciliación con lease renovada | Tests críticos + Firestore oficial con dos API/clientes |
| Lifecycle | Identidad exacta publish/candidate/promote/rollback y registro por fase | Fixtures de comandos/HTTP y recuperación sin replay |
| Releases individuales | Capability por servicio/operación, control durable y reconciliación sin replay | Tests locales + validación productiva pendiente |
| Build | Exclusión multiproceso por identidad inmutable, recuperación sin rebuild ciego | Constructor sintético, barrera y contador |
| Calidad | `oss-v2`, `src + scripts/release`, global 70%, diferencial 80% | Reporte normalizado sobre snapshot limpio exacto |

`IMPLEMENTADO` describe código. `VERIFICADO LOCALMENTE` se atribuye únicamente
a las pruebas incluidas en el paquete del snapshot correspondiente. Todas las
operaciones productivas permanecen `LIVE NO EJECUTADO`; la activación está
`PENDIENTE` de gate final, sesión OAuth accesible y validación operativa.
El usuario autorizó desplegar la implementación por la vía normal, sin bypass.
No se modifican globalmente permisos ni administración de Actions/Cloud Build.

## Evidencia histórica de la ronda anterior

Este tablero pertenece a la sesión Lead. Registra trabajo local revisable y no
autoriza publicación, despliegue, tráfico, migraciones, IAM, dispatches ni
cambios administrativos. La activación remota continúa fijada en `False` y las
fases 0–5 no se declaran cerradas por este documento.

| ID | Tanda | Repo/worktree | Base | Propietario | Archivos exclusivos | Depende de | Aceptación | Estado | Commit/patch | Evidencia/bloqueo |
|---|---|---|---|---|---|---|---|---|---|---|
| T1 | Control + emulador | API / `.reconciled-engine-api-final` | `6f502ca8ca65cf7de019ac17c22ceadde57e7542` | Lead | `scripts/release/firestore-control-integration`, `tests/integration_firestore_control.py`, control compartido | SDK oficial + emulador local | Dos API, dos clientes, consumo/lease/UNKNOWN/restart/fencing y cero efectos remotos | VERIFICADO LOCALMENTE | `HEAD d898059` + patch de working tree | Snapshot limpio `f0471c9`; evidencia Firestore local; Firestore productivo y Actions siguen bloqueados |
| T2-B | Registro + recuperación | API / `.reconciled-engine-api-final` | `6f502ca8ca65cf7de019ac17c22ceadde57e7542` | Lead | routers/stores de release, reconciliación GET y pruebas | T1 integrado | Identidad, idempotencia, conflictos SHA/digest/revisión y recuperación durable | VERIFICADO LOCALMENTE | `HEAD d898059` + patch de working tree | 471 pruebas; intent before effect; UNKNOWN no re-POST |
| T2-P | Providers locales + lifecycle | API / `.reconciled-engine-api-final` | `6f502ca8ca65cf7de019ac17c22ceadde57e7542` | Lead | lifecycle/CLI, fixtures locales, adapter SanPlat y pruebas | T1 integrado | Fixtures reproducen register/SanPlat ordenado sin repetir efectos | VERIFICADO LOCALMENTE | `HEAD d898059` + patch de working tree | Loopback + subprocess sintético; no build/push/deploy |
| T2-R | Revisión de carreras | API / `.reconciled-engine-api-final` | `6f502ca8ca65cf7de019ac17c22ceadde57e7542` | Lead | build determinista y fencing | T1 integrado | Carreras reproducibles, lease versionada y cobertura diferencial | VERIFICADO LOCALMENTE | `HEAD d898059` + patch de working tree | 83,85% diferencial; 0 findings Semgrep/Trivy |
| T3 | Releases individuales | API según contrato | posterior a T2 | Lead + propietarios asignados | capability y control por servicio | autorización OAuth, Firestore y gate `oss-v2` exacto | Sin grupos nuevos, actor/destino ligado y sin replay | VALIDACIÓN EN CURSO | `HEAD d898059` + patch de working tree | PENDIENTE DE VALIDACIÓN PRODUCTIVA; no deploy ejecutado |
| T4 | Adopción + transición | API/Web | posterior a T2/T3 | Lead | UI mínima, runbooks y diff administrativo | contratos integrados | Evidencia final, transición sin doble ejecución y checks preservados | PENDING | — | No se aplican workflows, triggers, checks ni permisos |

## Condición de integración

Solo el Lead integra commits después de revisar diff, archivos permitidos y
pruebas. Cualquier cambio posterior al gate canónico invalida la evidencia del
SHA anterior y exige nueva acreditación. `UNIT_DOUBLE`, `LOCAL_FIXTURE`,
`LOCAL_EMULATOR` y `LIVE` se reportan por separado; ningún fixture o emulador
acredita la frontera productiva.
