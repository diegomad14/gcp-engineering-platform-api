# Consola PostgreSQL de solo lectura

Observación de código: 2026-10-04, basada en `1abb3a9` más los cambios de esta
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

## Uso y límites

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
orden se conservan. No se guarda historial de SQL ni de resultados en el servidor.

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
marca tres llamadas de Psycopg como SQL dinámico de SQLAlchemy. Se revisaron
individualmente: el lock compone nombres exclusivamente con `Identifier`; el
describe y el cursor ejecutan el SELECT normalizado por pglast después de la
validación recursiva de AST y las auditorías de relaciones. Los aliases y límites
del envoltorio usan `Identifier` y `Literal`. Una consulta SQL completa no puede
ser un parámetro bind; estas ejecuciones intencionales también requieren el rol
restringido y la transacción `READ ONLY`.

Solo esas tres llamadas llevan una exclusión inline de esa regla, con su
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
