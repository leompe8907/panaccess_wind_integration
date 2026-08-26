# Rotación de secretos comprometidos (SECRET_KEY / ENCRYPTION_KEY / DB_PASSWORD)

Fecha: 2026-08-25 (herramienta) / 2026-08-26 (ejecución real en producción)
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Urgente #2
Estado: **RESUELTO.** Los 3 valores están rotados en producción. Ver "Ejecución real (2026-08-26)" más abajo para el detalle completo, incluido un incidente breve durante el proceso.

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

## Hallazgo importante durante la ejecución real: ENCRYPTION_KEY no es como los otros dos

`SECRET_KEY` y `DB_PASSWORD` son secretos de **verificación** -- se usan para
firmar/comprobar algo en el momento (una sesión, una conexión a la base).
Rotarlos no vuelve ilegible nada que ya estuviera guardado, solo cambia qué
valor hace falta de ahí en adelante. Se rotaron sin ningún efecto colateral,
como estaba previsto.

`ENCRYPTION_KEY` es distinta: es una llave de **cifrado simétrico** (Fernet).
Se usa para guardar disfrazados (cifrados) tres campos que ya tenían datos
reales de suscriptores:

- `SubscriberLoginInfo.password_hash`
- `SubscriberInfo.password_hash`
- `SubscriberInfo.pin_hash`

Cambiar `ENCRYPTION_KEY` sin más deja indescifrable (con la llave nueva)
todo lo que ya estaba cifrado con la vieja -- no es un problema de
"invalidar sesiones", es un problema de **datos que se vuelven ilegibles**.
Se confirmó con una consulta directa a producción: 60 filas de
`SubscriberLoginInfo.password_hash` tenían datos reales cifrados (0 en
`SubscriberInfo.password_hash`/`pin_hash`, así que el PIN y el pareo de TV
nunca estuvieron en riesgo).

El efecto práctico de dejar la llave nueva activa sin re-encriptar antes:
para esos 60 usuarios, el *login con la contraseña correcta* seguía
funcionando bien (porque usa el password ya cacheado en Django, un
mecanismo aparte que no depende de esta llave), pero un intento con la
contraseña **incorrecta** para cualquiera de esos 60 usuarios caía en un
camino de respaldo que sí intenta abrir el dato cifrado viejo -- y en vez de
responder "contraseña incorrecta" normal, tiraba un error de servidor
(500), porque el intento de descifrado explota antes de llegar a comparar
nada. No era una brecha de seguridad (nunca dejaba entrar sin la clave
correcta), pero sí un comportamiento roto y silencioso, difícil de notar
con pruebas manuales de uso normal.

## Herramienta nueva: `reencrypt_credentials`

Para poder rotar `ENCRYPTION_KEY` sin perder esos datos, se agregó
`wind/management/commands/reencrypt_credentials.py`, con el mismo patrón de
dos pasos que `rotate_secrets`:

```bash
# 1) Descifra todo con la llave activa (vieja), genera una llave nueva,
#    y deja todo en staging -- no toca la base ni el .env todavía.
python manage.py reencrypt_credentials --generate

# 2) Simula la migración completa sin escribir nada, para confirmar antes.
python manage.py reencrypt_credentials --apply --dry-run

# 3) Aplica de verdad: re-encripta cada fila con la llave nueva y la
#    verifica (round-trip) antes de guardar.
python manage.py reencrypt_credentials --apply

# En cualquier momento, ver si hay una migración generada pendiente.
python manage.py reencrypt_credentials --status
```

Detalles de seguridad del comando:

- El staging (`.reencrypt_credentials_pending.json`, permisos 600,
  gitignored) contiene los valores en texto plano temporalmente -- es
  inevitable, hace falta el texto plano para poder re-cifrarlo con la
  llave nueva. Se borra automáticamente si `--apply` termina sin fallos.
- `--apply` exige que la `ENCRYPTION_KEY` activa en ese momento sea
  exactamente la misma que estaba activa cuando se corrió `--generate` --
  si no coincide, se niega a correr (evita que cada fila falle por usar la
  llave equivocada, sin forma de distinguir eso de una fila corrupta de
  verdad).
- Cada fila se verifica con un round-trip (cifra con la nueva, descifra de
  vuelta, compara) antes de guardarse -- si algo no cuadra, esa fila
  específica se reporta como fallida y no se toca, sin frenar el resto.
- Fue agregado a `_SKIP_PANACCESS_INIT_COMMANDS` en `wind/apps.py`, mismo
  motivo que `rotate_secrets`.

## Ejecución real (2026-08-26)

Orden real seguido en el servidor de producción:

1. `rotate_secrets --generate` → `ALTER USER` en Postgres real → `rotate_secrets --apply --db-password-already-changed`. `SECRET_KEY` y `DB_PASSWORD` quedaron rotados sin incidentes.
2. Se aplicó también `ENCRYPTION_KEY` nueva en el mismo paso (antes de tener `reencrypt_credentials` armado) -- esto expuso el problema de arriba en producción real (no solo en teoría).
3. **Incidente corto:** al revertir manualmente `ENCRYPTION_KEY` al valor viejo (editando `.env` a mano, sin pasar por el comando), una edición dejó la línea `ENCRYPTION_KEY=` vacía. Django valida esa variable al arrancar (`appConfig.py:849`), así que 5 de las 8 instancias Daphne quedaron en loop de reinicio fallido (`OSError: Faltan variables de entorno: ENCRYPTION_KEY`) durante unos minutos. Se detectó con `journalctl -u panaccess-wind@8000.service`, se corrigió completando el valor correcto, y las 8 instancias volvieron a `active (running)`. `/health/`/`/ready/` no lo hubieran mostrado (no prueban esa variable específica) -- el `journalctl` fue lo que lo confirmó.
4. Con `ENCRYPTION_KEY` vieja restaurada y confirmada (`git show bc6b9ff^:.env`, la misma llave que estaba filtrada), se corrió `reencrypt_credentials --generate` → `--apply --dry-run` → `--apply`, migrando las 60 filas reales sin fallos. El comando borró el staging automáticamente al terminar sin fallos (comportamiento esperado) -- la `ENCRYPTION_KEY` nueva que había generado solo quedó impresa una vez en la terminal.
5. **Segundo incidente corto:** esa impresión en pantalla se perdió (no se guardó en ningún archivo aparte del staging ya borrado). Se intentó recuperarla del scroll de la terminal y de `tmux capture-pane` sin éxito (no había sesión `tmux`). Se verificó con una prueba de descifrado directa (`Fernet(candidate).decrypt(...)`) que ni la llave vieja ni una llave guardada de un intento anterior coincidían con lo que había quedado escrito en los 60 registros -- es decir, esos 60 valores de `password_hash` quedaron cifrados con una llave que ya no existe en ningún lado.
6. **Resolución final:** en vez de seguir buscando la llave perdida, se limpiaron esos 60 registros (`SubscriberLoginInfo.objects...update(password_hash=None)`) -- un estado ya soportado de forma segura por el código (`check_password()`/`get_password()` no intentan descifrar si el campo está vacío, es el mismo caso que un suscriptor que nunca inició sesión). Esto no afecta el acceso real de esos 60 suscriptores -- es solo un caché local interno, no su contraseña real en PanAccess; el caché se vuelve a poblar solo, de forma transparente, la próxima vez que ese suscriptor inicie sesión por el camino de respaldo. Con el campo ya vacío, no quedaba ningún dato dependiendo de ninguna llave vieja, así que se generó una `ENCRYPTION_KEY` final nueva (independiente, sin necesidad de migrar nada) y se aplicó directo en `.env`. `/health/`/`/ready/` quedaron en verde con las 8 instancias activas.

Resultado final: los 3 valores filtrados en git (`SECRET_KEY`, `ENCRYPTION_KEY`, `DB_PASSWORD`) ya no tienen ningún efecto sobre el sistema en producción. Los 60 registros de `password_hash` no se migraron con sus valores originales (esa llave intermedia se perdió), pero quedaron en un estado limpio y seguro, sin ningún riesgo de error -- no hizo falta pedirle a ningún suscriptor que resetee su contraseña.

**Lección para la próxima vez que se use `reencrypt_credentials` (o cualquier comando que imprima un secreto una sola vez):** copiar el valor impreso a un archivo aparte (o al vault) inmediatamente, antes de correr cualquier otro comando en esa misma terminal -- no confiar en el scroll/buffer de la terminal como respaldo.

## Qué queda explícitamente afuera de este cambio

- **No** reescribe el historial de git (los valores viejos, ya inútiles después de rotar, van a seguir visibles ahí -- se puede evaluar aparte si vale la pena limpiar el historial, pero no es bloqueante).
- **No** es una política de rotación periódica. Automatizar eso de verdad (sobre todo para `SECRET_KEY`) requeriría que `SIMPLE_JWT` soporte una lista de claves de firma con solapamiento, para no desloguear a todos los usuarios cada vez que rota -- es un proyecto de diseño aparte, no algo que se resuelva con este comando.
