# Rotación de secretos comprometidos (SECRET_KEY / ENCRYPTION_KEY / DB_PASSWORD)

Fecha: 2026-08-25
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Urgente #2
Estado: **Herramienta lista (`manage.py rotate_secrets`). Ejecución en el servidor real: pendiente.**

## De qué se trata

`SECRET_KEY`, `ENCRYPTION_KEY` y `DB_PASSWORD` del `.env` actual son idénticos, byte a byte, a los valores que quedaron committeados en claro en el historial de git hasta el commit `bc6b9ff` ("Dejar de versionar .env"). Se confirmó comparando directamente `.env` contra `git show bc6b9ff^:.env` -- nunca se rotaron después de sacar el archivo del tracking.

No es una rotación de rutina: es la respuesta a que esos tres valores concretos están comprometidos. Ver `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` para el detalle de por qué cada uno importa (`SECRET_KEY` firma JWT y los tokens de "olvidé mi contraseña"; `ENCRYPTION_KEY` es la clave Fernet que cifra el `sessionId` de PanAccess en Redis; `DB_PASSWORD` es acceso directo a Postgres).

## Por qué semi-automático y no un script que hace todo solo

El paso de cambiar el password en Postgres (`ALTER USER`) necesita permisos de superusuario que el propio proceso de Django no tiene ni debería tener, y este entorno de trabajo no tiene acceso de red a la base de datos real de producción. Por diseño, la herramienta **nunca ejecuta ese `ALTER USER` por sí misma** -- lo imprime para que un operador lo corra a mano, mirando la pantalla, y solo continúa cuando ese mismo operador confirma explícitamente que ya lo hizo.

## Qué se construyó

`wind/management/commands/rotate_secrets.py` -- comando de Django en dos pasos:

```bash
# 1) Genera los 3 valores nuevos, los deja en staging (no toca .env)
python manage.py rotate_secrets --generate

# 2) (a mano) correr el ALTER USER que imprime, contra Postgres real

# 3) Aplica el staging al .env real, con backup automático
python manage.py rotate_secrets --apply --db-password-already-changed

# En cualquier momento, ver si hay una rotación generada sin aplicar
python manage.py rotate_secrets --status
```

### `--generate`

- Genera `SECRET_KEY` nuevo (`django.core.management.utils.get_random_secret_key()`), `ENCRYPTION_KEY` nuevo (`Fernet.generate_key()`) y un `DB_PASSWORD` aleatorio de 32 caracteres alfanuméricos (sin símbolos, a propósito -- el password actual, `4b%N#9zX$2wL`, ya mostró que caracteres como `%`/`$` complican el escape en SQL/`.env`).
- Los guarda en `.secrets_rotation_pending.json`, en la raíz del proyecto, con permisos `600` (agregado a `.gitignore`).
- Imprime los 3 valores y el `ALTER USER "<DB_USER>" WITH PASSWORD '...';` exacto para correr en `psql`.
- Si ya hay una rotación generada sin aplicar, se niega a generar otra (para no perder de vista cuál password real quedó configurado en Postgres).

### `--apply --db-password-already-changed`

- Se niega a correr si falta `--db-password-already-changed` -- es la salvaguarda contra el escenario "el `.env` ya tiene el password nuevo pero Postgres todavía tiene el viejo", que dejaría a la app sin poder conectarse a la base.
- Hace `.env.bak.<timestamp UTC>` antes de tocar nada (también cubierto por `.env.*` en `.gitignore`).
- Reemplaza las 3 líneas en el `.env` real (o las agrega al final si no existían con ese nombre exacto).
- Borra el staging.
- Imprime el checklist de qué reiniciar.

### Detalle de implementación que vale la pena señalar

`rotate_secrets` se agregó a `_SKIP_PANACCESS_INIT_COMMANDS` en `wind/apps.py` -- sin esto, cada invocación (incluido `--status` o `--help`) dispara el intento de login a PanAccess de `wind/apps.py` (~46s de reintentos si no hay red hacia `middleware.wind.do`, como en este entorno de trabajo). No tiene sentido que un comando que no toca PanAccess para nada se demore por eso, y menos en medio de un incidente.

## Verificación hecha en este entorno (sin tocar nada real)

- `python -m py_compile` sobre el archivo -- sin errores de sintaxis.
- `manage.py help rotate_secrets` -- confirma que el comando se registra y el `--help` se ve bien.
- `manage.py rotate_secrets --status` (sin staging) -- responde correctamente que no hay nada pendiente.
- `manage.py rotate_secrets --apply --db-password-already-changed` (sin haber corrido `--generate` antes) -- falla con el error esperado, no toca `.env`.
- `manage.py rotate_secrets --generate` -- generó valores nuevos reales (de prueba, jamás aplicados a ningún lado) y los guardó en el staging con permisos `600`; se verificó que el `.env` no cambió (mismo hash antes y después).
- `manage.py rotate_secrets --generate` corrido una segunda vez con el staging ya presente -- se negó correctamente, sin sobreescribir.
- `manage.py rotate_secrets --apply` sin `--db-password-already-changed` -- falla con el error esperado.
- El archivo de staging de esta prueba se borró al terminar (no queda ninguna rotación a medias en este entorno).

**No se ejecutó `--apply` de verdad en ningún momento** -- eso queda para cuando el operador corra esto contra el servidor de producción real, con acceso a la base de datos real.

## Cómo usarlo en el servidor real (checklist para el operador)

1. `cd` al directorio del proyecto en el servidor, con el virtualenv activado.
2. `python manage.py rotate_secrets --generate`.
3. Copiar el `ALTER USER ...` que imprime y correrlo en `psql` contra la base real. Confirmar que responde `ALTER ROLE` sin error.
4. `python manage.py rotate_secrets --apply --db-password-already-changed`.
5. Reiniciar Daphne (las 8 instancias, `deploy/manage_daphne.sh`) y los workers de Celery.
6. Avisar al equipo: todos los usuarios (web y apps) van a perder sesión por el `SECRET_KEY` nuevo -- es esperado, no un bug.
7. Guardar los 3 valores nuevos en el gestor de contraseñas/vault que use el equipo para credenciales de infraestructura -- **no** por correo, no en un documento suelto (ver la conversación que dio origen a esta herramienta: es el mismo tipo de riesgo que se está resolviendo).

## Qué queda explícitamente afuera de este cambio

- **No** reescribe el historial de git (los valores viejos, ya inútiles después de rotar, van a seguir visibles ahí -- se puede evaluar aparte si vale la pena limpiar el historial, pero no es bloqueante).
- **No** es una política de rotación periódica. Automatizar eso de verdad (sobre todo para `SECRET_KEY`) requeriría que `SIMPLE_JWT` soporte una lista de claves de firma con solapamiento, para no desloguear a todos los usuarios cada vez que rota -- es un proyecto de diseño aparte, no algo que se resuelva con este comando.
