# Consola PostgreSQL de solo lectura

Observación de código: 2026-10-04, basada en `dcdd25a` más los cambios de esta
funcionalidad. La consola vive en **Operate → BD** de eng-platform y usa una
sesión real de GitHub OAuth. La función permanece desactivada por defecto.

## Activación privada

Antes de activar, un administrador de cada base debe preparar un usuario
dedicado con `LOGIN`, sin `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`,
`BYPASSRLS`, membresías ni propiedad de objetos. Necesita únicamente `CONNECT`
a la base, `USAGE` en los esquemas aprobados y `SELECT` en las tablas permitidas.
Los privilegios efectivos heredados de `PUBLIC` también cuentan: el rol no
puede tener `CREATE` o `TEMP` en la base ni `CREATE`, propiedad, escritura,
`REFERENCES`, `TRIGGER`, permisos de escritura por columna o `USAGE`/`UPDATE`
sobre secuencias en ningún esquema de usuario. Conviene configurar también
`default_transaction_read_only=on` para el rol como defensa adicional.

Estas son condiciones para una futura configuración administrada; este cambio
no crea usuarios, no concede ni revoca privilegios y no modifica Cloud SQL.
La instancia CGM observada es `cgm-assistant-prod:us-central1:cgm-sanplat-pg`;
`cgm_sanplat` puede ser la primera conexión una vez exista su lector dedicado.
Las credenciales de los servicios y el usuario `postgres` no sirven para esta
consola. No se reutilizan credenciales de aplicaciones con permisos de escritura.

Configurar en el servidor:

| Variable | Valor / finalidad |
| --- | --- |
| `ENG_PLATFORM_MOCK_MODE` | `false` |
| `ENG_PLATFORM_DATABASES_ENABLED` | `true`, únicamente después de cumplir los requisitos |
| `ENG_PLATFORM_DATABASES_ALLOWED_LOGINS` | Logins GitHub autorizados, separados por comas |
| `ENG_PLATFORM_DATABASES_REGISTRY_PATH` | Ruta absoluta a un JSON privado montado |
| `ENG_PLATFORM_DATABASES_REGISTRY_SHA256` | SHA-256 exacto del contenido del JSON |
| `ENG_PLATFORM_DATABASE_DSN_CGM_SANPLAT` | DSN del lector dedicado, inyectado desde Secret Manager |
| Variables existentes de GitHub OAuth | Client ID, client secret y secreto de sesión de al menos 32 caracteres |

Ejemplo de registro **privado**, sin contraseña ni DSN literal:

```json
{
  "databases": [
    {
      "id": "cgm-sanplat",
      "name": "CGM SanPlat · PostgreSQL",
      "schemas": ["public"],
      "allowed_logins": ["diegomad14"],
      "dsn_env": "ENG_PLATFORM_DATABASE_DSN_CGM_SANPLAT"
    }
  ]
}
```

El archivo admite como máximo 64 conexiones y 256 KiB. Tanto el digest como
todo el documento se validan en cada operación. JSON duplicado, fuente ausente,
montaje no regular, esquemas de sistema, campos adicionales o digest incorrecto
invalidan el registro completo. No hay catálogo de ejemplo ni conexión simulada
para consultas. Se necesita autorización global **y** autorización por conexión;
los permisos para desplegar o leer logs no conceden acceso a BD.

El registro admite asociaciones **curadas** opcionales: `service_names` en cada
conexión y `table_services`, una lista de objetos `{schema, name, service_names}`.
Una conexión puede declarar hasta 64 IDs de servicio de Cloud Run únicos; cada
tabla puede asociarse únicamente a servicios ya declarados en esa conexión.
Hasta 2.048 asociaciones de tabla usan nombres exactos y únicos de esquema/tabla,
y sus esquemas deben estar autorizados en la conexión. Por ejemplo, una
asociación ilustrativa sería:

```json
{
  "service_names": ["example-service"],
  "table_services": [
    {"schema": "public", "name": "tabla_aprobada", "service_names": ["example-service"]}
  ]
}
```

Agregar una asociación requiere evidencia de configuración, migraciones o código
del servicio. La consola no infiere relaciones por nombres similares ni convierte
los servicios de una conexión en propietarios de todas sus tablas. Una tabla sin
asociación devuelve `service_names: []` y debe mostrarse como servicio sin
confirmar. Las asociaciones solo acompañan tablas que PostgreSQL realmente
expone al lector y que pasan las comprobaciones de seguridad; el registro no
crea entradas para tablas ausentes, invisibles o excluidas. Todos estos metadatos
siguen la misma ACL de BD que la conexión y sus resultados.

Cloud Run debe montar la conexión Cloud SQL o un transporte privado equivalente,
inyectar el secreto sin publicarlo en el frontend y usar una cuenta de servicio
con acceso únicamente a esa instancia y a los secretos necesarios. Un DSN para
socket Cloud SQL debe indicar la base y el lector dedicado; el secreto completo
solo existe en el entorno del backend. Las URL públicas y respuestas del API
nunca contienen la referencia del secreto, DSN o listas de lectores.

Cada proceso admite cuatro operaciones simultáneas y abre una conexión nueva
por operación. Dimensionar trabajadores, concurrencia y máximo de instancias
de Cloud Run para respetar el presupuesto total de conexiones de Cloud SQL.
No se usan pools, reintentos ni consultas automáticas periódicas.

## Uso y límites del contrato legacy

Primero elegir una conexión y consultar su esquema; después escribir una sola
sentencia `SELECT`, usando nombres de tabla calificados con el esquema:

```sql
SELECT id, nombre
FROM public.tabla_aprobada
ORDER BY id DESC
LIMIT 100;
```

Se permiten filtros, agregaciones comunes, joins, subconsultas, CTE de lectura,
operaciones de conjuntos y funciones de ventana admitidas. La lista de funciones
es deliberadamente pequeña (`count`, `sum`, `avg`, `min`, `max`, `date_trunc`,
`lower`, entre otras); la autoridad exacta está en
`src/eng_platform_api/services/database_sql.py`. Funciones personalizadas,
funciones de archivos/red, `nextval`, `set_config`, operadores calificados,
casts personalizados, bloqueos, `SELECT INTO` y CTE de escritura se rechazan.
`search_path` contiene únicamente `pg_catalog`.

Solo se consultan tablas normales o particionadas con columnas de tipos base
de PostgreSQL. Se excluyen vistas, vistas materializadas, tablas foráneas,
tablas con RLS y columnas con tipos personalizados/dominios. Se comprueban
también los descendientes de herencia/partición para impedir lecturas fuera de
los esquemas autorizados. Se toman bloqueos `ACCESS SHARE` y se repite la
validación antes de ejecutar, evitando cambios concurrentes de esquema/RLS.

| Límite | Valor |
| --- | --- |
| Filas solicitadas | 100 por defecto; entre 1 y 500 |
| Columnas | 100 |
| SQL | 30.000 caracteres; petición JSON completa ≤ 32 KiB |
| Conexión / sentencia / bloqueo | 5 s / 5 s / 1 s |
| Espera HTTP de operación / recepción del cuerpo | 15 s / 5 s |
| Celda / fila / respuesta | 16 KiB / 64 KiB / 1 MiB |
| Introspección de columnas / descendientes | 10.000 registros |

Se describe la consulta sin recuperar filas y se usa un cursor del servidor
con `fetchmany(max_rows + 1)`. Un envoltorio SQL limita el tamaño de cada fila
antes de transferirla al proceso API. La primera fila que exceda el límite de
celda, fila o respuesta detiene el resultado y marca `truncated=true`; se
devuelven únicamente filas completas. El límite de filas limita la transferencia,
pero una agregación puede necesitar leer más datos en PostgreSQL y está sujeta
al timeout. Cada camino termina con rollback y cierre, también los errores.
Si vence la espera HTTP, el propietario de la conexión conserva su plaza hasta
cerrar; una petición adicional puede recibir `429`.

Los valores `numeric`/decimales y enteros mayores que la precisión exacta de
JavaScript se devuelven como texto, también dentro de JSON, para conservar su
valor. Fechas, UUID y bytes siguen la representación JSON de PostgreSQL; los
bytes se representan en hexadecimal. Los nombres de columna duplicados y su
orden se conservan. El contrato legacy no guarda historial de SQL ni de resultados en el servidor.
La captura privada temporal de la nueva capacidad se describe más abajo.

## Contrato HTTP y privacidad

`GET /api/databases` devuelve únicamente las conexiones visibles con
`id`, `name`, `schemas`, `max_rows`, `timeout_seconds` y `service_names`.

`POST /api/databases/{id}/schema`, con cuerpo `{}`, devuelve
`{ "tables": [{ "schema": "public", "name": "tabla", "service_names": [], "columns": [{ "name": "id", "data_type": "integer" }] }] }`.

`POST /api/databases/{id}/query`, con cuerpo
`{ "sql": "SELECT 1", "max_rows": 100 }`, devuelve
`columns`, `rows`, `row_count`, `truncated` y `elapsed_ms`.

Los POST requieren `Content-Type: application/json`, el `Origin` configurado
del frontend y `X-Requested-With: EngineeringPlatform`. No admiten filtros SQL
en parámetros de URL. Las sesiones de desarrollo, IAP y sesiones sin procedencia
GitHub OAuth se rechazan. Se revalidan las ACL antes/después de la conexión y
al devolver la respuesta. Las conexiones ocultas y desconocidas reciben el mismo
`404`. Éxitos y errores incluyen `Cache-Control: no-store` y `Pragma: no-cache`.

Los errores de sintaxis, validación y PostgreSQL se sustituyen por mensajes
genéricos; el API no devuelve ni registra SQL, valores del cuerpo o diagnósticos
del proveedor. Los registros propios de PostgreSQL/Cloud SQL se administran
por separado y deben tener acceso restringido: la política del API no cambia la
configuración de logging del servidor de base de datos.

## Revisión del análisis estático

La regla Semgrep `python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query`
marca seis llamadas de Psycopg como SQL dinámico de SQLAlchemy. Se revisaron
individualmente: el lock compone nombres exclusivamente con `Identifier`; el
describe y el cursor ejecutan el SELECT normalizado por pglast después de la
validación recursiva de AST y las auditorías de relaciones. Los aliases y límites
del envoltorio usan `Identifier` y `Literal`. Una consulta SQL completa no puede
ser un parámetro bind; estas ejecuciones intencionales también requieren el rol
restringido y la transacción `READ ONLY`.

Solo esas seis llamadas llevan una exclusión inline de esa regla, con su
justificación específica. No se excluye el archivo ni se cambian reglas globales,
umbrales o pruebas de privilegios. El gate conserva las excepciones existentes,
acotadas por ruta, para los tres Dockerfiles de ejecución de calidad/release en
`.trivyignore.yaml`; no añade excepciones de Trivy para esta funcionalidad.

## Validación en un PostgreSQL desechable

Las pruebas unitarias normales no necesitan una base. Para validar el adaptador
real, preparar PostgreSQL 16 desechable con la base `readonly_test`, un lector
dedicado y `sample.items(id integer, name text, amount numeric, payload jsonb)`
con 12 filas: `id=1..12`, `name='item-' || id`, `amount=id*1.25`,
`payload={"safe":true}`. El lector debe cumplir los privilegios anteriores.

Solo apuntar estas variables al entorno desechable:

```sh
export ENG_PLATFORM_DATABASE_TEST_DSN='<DSN del lector de pruebas>'
export ENG_PLATFORM_DATABASE_TEST_ADMIN_DSN='<DSN del administrador de pruebas>'
uv run pytest tests/test_databases.py tests/test_databases_postgres.py -q
```

Las pruebas administrativas crean y eliminan objetos de prueba y permisos
temporales para comprobar vistas con efectos secundarios, RLS, tipos propios,
herencia fuera del esquema, permisos de columnas/secuencias, permisos externos
y membresías. Nunca usar un DSN de CGM o producción en estas variables.

## Ejecuciones completas y capturas temporales

La capacidad `workspace_enabled` habilita la segunda versión de BD. La ruta
legacy `/query` conserva su contrato de 500 filas y cinco segundos para permitir
el despliegue API → Web; las consultas del workspace usan ejecuciones duraderas.
`GET /api/databases` anuncia `workspace_enabled` y, al habilitarlo, `workspace_limits` con
`timeout_seconds: 240`, `page_sizes: [25, 50, 100]` y `retention_seconds: 3600`.
Las conexiones conservan `max_rows: 500` y `timeout_seconds: 5` del contrato legacy.
No existe un límite fijo de filas para la ejecución completa. La primera consulta
puede requerir renovar GitHub OAuth: `database_session_required` lo indica en
`/api/auth/me`. El nuevo nonce opaco solo se crea en un callback OAuth real, está
vinculado a un registro servidor y se revoca antes de borrar la cookie al salir.

Cada pestaña crea un workspace. Su vida máxima es la de la sesión OAuth (12 h);
una captura completa caduca una hora después de terminar, aunque el workspace
puede ejecutar nuevas consultas. Otra ejecución conserva la captura anterior
hasta que el usuario la sustituye o limpia. No se guarda historial persistente.
El SQL se deposita brevemente en un objeto privado y se borra **antes** de abrir
la consulta PostgreSQL. Un trigger de limpieza se programa antes del upload,
para cubrir una caída entre el upload y la creación del registro de control.

Cloud Run debe mantener un timeout HTTP máximo de 300 s: los leases de
admisión caducan a los 330 s, después del deadline del servicio. La descarga
aplica además 270 s de tiempo de aplicación y revalida inmediatamente antes
y después de cada lectura GCS, antes de enviar el chunk.

El API existente recibe trabajos Cloud Tasks con OIDC de una cuenta dedicada.
El cuerpo contiene únicamente `{ "id": "<32 hex>" }`; la tarea no lleva SQL,
cookies, DSN ni credenciales de servicios. Firestore reclama el trabajo mediante
CAS y un token único. Un outbox reconcilia únicamente trabajos aún `queued` con nombres de tarea
deterministas. Si no arrancan en cinco minutos, fallan con `DISPATCH_FAILED`.
Una entrega repetida o un trabajador perdido nunca vuelve a ejecutar SQL; el usuario debe iniciar otra ejecución expresamente. El worker
consulta la revocación, ACL, workspace y política activa cada segundo; los
callbacks de cada lote consultan primero el estado local y reutilizan esa
comprobación breve. Antes de publicar se fuerza la comprobación y un CAS vuelve
a comprobar sesión, workspace y política. Una revocación o un fallo de lectura
impide publicar; el watcher cancela también un FETCH bloqueado.

| Recurso | Presupuesto de ejecución completa |
| --- | --- |
| Tiempo total consulta/exportación | 240 s, incluyendo RPC, lectura y upload |
| Sentencia / FETCH | El menor de 240 s y el tiempo total restante |
| Conexión / auditoría / bloqueo | 5 s / 5 s / 1 s |
| Worker / tarea / servicio Cloud Run | 270 s / 275 s / 300 s |
| Columnas / celda / fila | 100 / 16 KiB / 64 KiB |
| Chunk NDJSON | 1 MiB; filas completas y contiguas |
| Captura, incluyendo manifiesto | 256 MiB |
| XLSX comprimido / XML generado | 256 MiB / 1 GiB |
| Almacenamiento por login / global | 512 MiB / 2 GiB, incluyendo reservas |
| Consulta larga | 2 globales; 1 por login; 1 por rol PostgreSQL real |
| Introspección | 2 globales; 1 adicional por rol, independiente de la consulta |
| Exportación / lectura | 1 exportación global; 2 lecturas globales, permitiendo paginar durante una descarga |
| Paginación | 25/50/100 filas, índice cero; respuesta ≤ 8 MiB |
| Metadatos temporales | 32 workspaces por sesión; 256 ejecuciones por workspace; 8 intentos de exportación por captura |

El snapshot incluye columnas con clave estable `{key, name, data_type}`, filas
ordenadas y un manifiesto completo. Cada objeto usa creación exclusiva, generación
GCS exacta y SHA-256; el manifiesto verifica namespace, tamaño, columnas, orden,
cuentas y referencias. Solo se publican capturas completas. Una consulta que
excede tiempo, celda, fila, almacenamiento o capacidad falla explícitamente;
no ofrece un conjunto parcial como resultado completo. Las páginas usan
`offset = page_index * page_size` y el total exacto de filas de esa captura;
no vuelven a abrir PostgreSQL ni introducen huecos al cambiar el tamaño de página.

La exportación lee **esa misma captura**. Genera ZIP/XML de forma incremental
hacia GCS con un buffer de 8 MiB, sin archivo temporal ni workbook completo en
memoria. Usa celdas de texto para conservar números exactos, valores JSON,
encabezados duplicados y cadenas que parecen fórmulas. Divide las filas en hojas
cuando se alcanza el máximo de Excel: 1.048.576 filas por hoja, incluyendo el
encabezado; hasta 100 columnas en esta función. Respeta también 32.767 unidades
UTF-16 y 253 saltos de línea por celda; una incompatibilidad produce un error de
recursos explícito. Excel tiene precisión numérica de 15 dígitos, por lo que los
valores exactos se conservan como texto. Un export fallido se puede volver a
intentar cuando se confirma el borrado de su artefacto y se libera la reserva;
ese reintento no ejecuta SQL.

## Contrato de workspace

Todas las rutas se encuentran bajo `/api/databases/{id}/workspaces` y respetan
la autorización global y por conexión. Los POST usan los mismos headers privados
que `/query`; los GET rechazan parámetros de URL.

| Método / sufijo | Cuerpo / respuesta |
| --- | --- |
| `POST /` | `{}` → `workspace_id`, `database_id`, `expires_at`, `execution_id` |
| `GET /{wid}` | Estado del workspace |
| `POST /{wid}/executions` | `{sql, client_request_id}` → ejecución; mismo ID/cuerpo es idempotente, un SQL distinto con el mismo ID recibe `409` |
| `GET /{wid}/executions/{eid}` | `status`, `columns`, `row_count`, `elapsed_ms`, `expires_at`, `error_code` y IDs |
| `POST /{wid}/executions/{eid}/pages` | `{page_index, page_size, view_id?}` → `columns`, `rows`, índices, `row_count`, `total_pages` y `view_id` si se seleccionó una vista |
| `POST /{wid}/executions/{eid}/cancel` | `{}` → cancelación lógica inmediata |
| `POST /{wid}/purge` | `{}` → invalida el workspace y encola borrado |
| `POST /{wid}/executions/{eid}/exports` | `{view_id?}` → `export_id`, `status`, `expires_at`, `error_code`, `download_path` y `source_view_id` para una vista |
| `POST /{wid}/executions/{eid}/views` | `{column_key, direction: "asc"|"desc", client_request_id}` → `202`, estado de vista |
| `GET /{wid}/executions/{eid}/views/{vid}` | `view_id`, `execution_id`, `status`, `column_key`, `direction`, `expires_at`, `row_count`, `elapsed_ms`, `error_code` |
| `DELETE /{wid}/executions/{eid}/views/{vid}` | `{}` con headers privados → `{purged: true}`; invalida y encola borrado |
| `GET /{wid}/executions/{eid}/exports/{xid}` | Estado del export |
| `GET /{wid}/executions/{eid}/exports/{xid}/file` | Descarga nativa autenticada, attachment y `no-store`; sin URL firmada ni fetch de todo el fichero |

Estados de ejecución: `queued`, `running`, `completed`, `failed`, `cancelled`,
`expired`, `purged`. Los errores de trabajo son códigos cerrados, como
`QUERY_REJECTED`, `RESOURCE_BUSY`, `RESOURCE_EXCEEDED`, `QUERY_STOPPED`,
`QUERY_FAILED`, `EXPORT_FAILED`, `DISPATCH_FAILED` o `WORKER_LOST`, sin SQL ni
diagnósticos del proveedor. La descarga admite navegación nativa same-origin y
valida cookie revocable y `Sec-Fetch-Site`/`Origin` cuando se presentan.

## Ordenación global de una captura

La Web presenta valores completos sin inspectores al seleccionar celdas. JSON y
JSONB se formatean para lectura conservando números y escapes; la captura y el
Excel mantienen su representación original. Las filas crecen con el contenido,
incluidas columnas estrechas. Los controles de altura de fila y ancho de columna
admiten ratón, táctil y teclado; ajustar el tamaño nunca recorta datos. SQL NULL,
texto NULL y cadenas vacías tienen representaciones distintas.

Pulsar una tabla del explorador despliega sus columnas y tipos. Los botones de
inserción agregan únicamente el identificador de tabla calificado o el nombre
de columna entre comillas en la última posición del cursor, sin reemplazar SQL
seleccionado ni generar SELECT. Devuelven el foco al editor y admiten deshacer
en una acción. Los encabezados alternan ascendente, descendente y orden original;
su control de ordenación es independiente del divisor de ancho.

El éxito de Excel aparece en un Snackbar MUI arriba a la derecha durante ocho
segundos, con cierre y descarga. El enlace permanece disponible en la barra de
resultados hasta el vencimiento o la purga. La exportación identifica y conserva
la captura y el orden seleccionados al solicitarla, aunque cambie el borrador o
se seleccione otra vista después.

`global_sort_enabled` se anuncia en el listado únicamente cuando la política de
workspaces está vigente. La ordenación usa todas las filas ya capturadas y no
abre conexiones PostgreSQL, reejecuta SQL ni altera el manifest original. El
cliente mantiene la vista anterior hasta publicar la nueva; un fallo o una
cancelación conservan el resultado original y las otras vistas disponibles.
La ausencia de `view_id` en páginas/exports selecciona el orden original.

Se ordena por `column_key` posicional, que distingue nombres duplicados. Los
enteros, decimales y flotantes se comparan a partir de sus lexemas exactos;
no se convierten a floats de JavaScript/Python. Los booleanos ordenan false
antes de true. El texto se compara por puntos de código Unicode, sin collation
de la BD. JSON/JSONB, arrays y otros tipos se comparan como su texto capturado.
Las fechas/horas usan orden cronológico y normalizan zonas cuando corresponden.
SQL NULL aparece al final tanto en ascendente como descendente; los empates
conservan el ordinal original de captura. La exportación conserva valores y
precisión, incluidos JSON null y SQL NULL distintos.

El worker `/api/internal/database-executions/sort` usa la misma queue de export
y el mismo permiso pesado global de uno: ordenación y export no compiten por
RAM simultáneamente. El merge externo escribe runs privados GCS de forma
create-only, con generation/hash; procesa buffers acotados hasta 64 MiB y no
tiene un tope fijo de filas. La vista completa final usa el mismo formato
NDJSON/chunks de 1 MiB/manifest que páginas y Excel. Cada upload, incluidos
temporales y manifest, reserva cuota antes de iniciarse; cada run se elimina
después de materializar su reemplazo y sólo entonces se descuenta su tamaño.
Los temporales, la captura original, otras vistas y XLSX consumen las cuotas
existentes de 512 MiB por usuario y 2 GiB globales. Si no cabe o excede 240 s,
falla explícitamente y conserva la captura; no trunca ni ordena sólo una página.

Cada vista hereda el vencimiento de la captura original; ordenar no renueva la
hora de retención. CAS/fencing verifica sesión, workspace, política y captura
al publicar. La limpieza espera que los workers terminen, elimina todos los
objetos de la vista y verifica namespace vacío antes de liberar su reserva.
Hay hasta 16 vistas vivas por captura para acotar metadata; retirar/limpiar una
libera ese cupo, sin límite acumulado de comandos. El cliente retira vistas
anteriores después de seleccionar otra y mantiene las usadas por exports en
curso. Un XLSX completado vive bajo la captura original y continúa descargable
si se retira su vista, con la misma ACL, sesión revocable y TTL original.
Logout, expiración y purga del workspace eliminan también todas sus vistas.

## Infraestructura y activación de ejecuciones

Además del registro y OAuth ya descritos, configurar:

| Variable | Finalidad |
| --- | --- |
| `ENG_PLATFORM_DATABASE_EXECUTIONS_ENABLED` | `true` solo tras validar política/infraestructura |
| `ENG_PLATFORM_DATABASE_PROJECT_ID` | Proyecto del control/storage/tasks |
| `ENG_PLATFORM_DATABASE_COLLECTION` | Colección dedicada; por defecto `eng_platform_database_control` |
| `ENG_PLATFORM_DATABASE_RESULT_BUCKET` | Bucket dedicado privado de capturas |
| `ENG_PLATFORM_DATABASE_QUEUE_LOCATION` | Ubicación de las tres queues; por defecto `us-central1` |
| `ENG_PLATFORM_DATABASE_QUERY_QUEUE` | Queue de consultas, máximo dos dispatches concurrentes |
| `ENG_PLATFORM_DATABASE_EXPORT_QUEUE` | Queue de exports, máximo un dispatch concurrente |
| `ENG_PLATFORM_DATABASE_CLEANUP_QUEUE` | Queue de limpieza y reintentos |
| `ENG_PLATFORM_DATABASE_WORKER_SERVICE_ACCOUNT` | Email exacto del invocador OIDC dedicado |
| `ENG_PLATFORM_DATABASE_API_ORIGIN` | Origen HTTPS canónico; URL y audience OIDC exactas |
| `ENG_PLATFORM_DATABASE_POLICY_VERSION` | Versión explícita de la política desplegada |

En la colección dedicada, `control_active_policy` debe contener `kind: control`,
`enabled: true`, `version` igual a la variable y `fingerprint` calculado por
`database_jobs.policy_fingerprint()` con el registro, ACL y configuración de
OAuth/worker del despliegue. No incluye SQL ni contraseñas. La ausencia, diferencia
u outage falla cerrado; no existe backend local de producción. Los permisos y
reservas globales están en `control_budget`, mediante transacciones Firestore.
La cuenta runtime necesita datastore, acceso a los objetos privados, encolar en
las tres queues y actuar como el SA invocador; este último solo necesita invocar
la API. No compartir el bucket con otros artefactos ni conceder acceso público.

Mantener acceso uniforme, public access prevention y eliminar soft delete,
versionado, retention lock y copias de resultados. Programar un sweeper OIDC
periódico con POST `{}` a `/api/internal/database-executions/sweep`; no contiene
credenciales de usuarios. Las queues invocan `/query`, `/export`, `/sort`, `/cleanup` en
ese prefijo interno y rechazan identidades distintas del SA exacto. No usar
Cloud Run Jobs nuevos ni ampliar el catálogo de despliegue.

`expires_at` es un epoch numérico aplicado por el API; no depende del TTL
asíncrono de Firestore. Crear índice compuesto para `kind` + `expires_at` y los
lookups de `kind` + `session_id` / `workspace_id` / `execution_id` / `state` si
Firestore los exige. El sweeper limpia ventanas acotadas y mueve los registros
limpios fuera de la ventana expirada; tombstones sin columnas, SQL ni referencias
se eliminan una hora después. Identidades idempotentes permanecen hasta que
caduca la sesión/workspace para impedir una repetición automática del SQL.

El borrado lógico impide acceso de inmediato. El físico se ejecuta con tareas y
sweeper, reintentando fallos; nunca se libera la reserva mientras no se confirma
el borrado y una relectura de ambos namespaces verifica que están vacíos.
Cada limpieza/sweeper tiene un deadline de 230 s; el borrado parcial conserva
la reserva y se reintenta, saltando registros ya limpiados para hacer progreso. Si un worker sigue cerrando o subiendo, la limpieza espera su deadline
acotado antes de borrar objetos tardíos. Logout revoca primero el nonce y solicita
borrado de todos sus workspaces. Ningún resultado completo conserva SQL.

El análisis estático revisa seis llamadas dinámicas de Psycopg: tres legacy y tres
de streaming. Sus exclusiones inline específicas corresponden a SQL compuesto
con `Identifier`/`Literal` y el SELECT validado por AST; no desactivan el escáner,
las reglas ni las comprobaciones del rol.

Validación adicional independiente: pruebas de jobs/Firestore/Cloud Tasks/GCS y
OIDC; prueba PostgreSQL de más de 500 filas, timeout y cancelación; lectura de
XLSX con `openpyxl` como consumidor independiente. Los dobles de almacenamiento
solo se inyectan expresamente en tests; no son un modo de ejecución de producción.
