# Migración a AEAD del esquema legado de credenciales por app_type (Medio #8)

Fecha: 2026-09-02
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Medio #8), `wind/utils/crypto_tv.py`.

## Contexto

`hybrid_encrypt_for_app()` cifra las credenciales que la TV recibe para auto-loguearse, usando una llave RSA **estática** por `app_type` (`AppCredentials`). El AES usado para el cuerpo del mensaje era siempre AES-256-CBC, sin autenticación (sin HMAC/AEAD) -- vulnerable en teoría a bit-flipping/padding oracle, aunque mitigado por ir siempre sobre HTTPS.

No se podía cambiar el algoritmo/formato de un plumazo: el esquema `AES-256-CBC + RSA-OAEP` tiene clientes reales hoy (bromteck/cableatlantico, vía sus brands en `appVideo`) con TVs ya desplegadas que desencriptan ese formato exacto. Cambiarlo sin coordinar rompe sus apps en producción.

El núcleo de cifrado (`_hybrid_encrypt_with_public_key`) ya soportaba un modo AEAD (AES-256-GCM, con tag de integridad) desde antes -- reservado hasta ahora para `hybrid_encrypt_for_device_public_key` (el esquema nuevo de llave efímera por pareo, sin clientes legados). Este cambio conecta ese modo también a `hybrid_encrypt_for_app`, pero **apagado por default**.

## Qué se implementó

1. **`wind/models.py`** -- nuevo campo `AppCredentials.supports_aead` (`BooleanField`, default `False`).
2. **`wind/migrations/0011_aead_support_flag.py`** -- migración generada con `makemigrations` (no escrita a mano).
3. **`wind/utils/crypto_tv.py`** -- `hybrid_encrypt_for_app()` ahora pasa `use_aead=app_credentials.supports_aead` (antes era `False` fijo, implícito). Docstrings de ambas funciones actualizados para reflejar que el modo ya no es fijo por función, sino por credencial.

## Por qué es seguro para bromteck/cableatlantico tal como está hoy

- El default de la columna nueva es `False` -- **ninguna fila existente de `AppCredentials` cambia de comportamiento** sin una acción explícita (UPDATE manual de esa columna para una fila puntual).
- Confirmado con test (`wind/tests/test_crypto_tv_aead.py::test_default_supports_aead_false_keeps_legacy_cbc_payload`): con el flag en `False`, el payload sigue siendo exactamente `{"encrypted_data", "encrypted_key", "iv", "algorithm": "AES-256-CBC + RSA-OAEP", "app_type"}`, sin el campo `tag`, IV de 16 bytes (bloque CBC) -- indistinguible del comportamiento anterior a este cambio.
- La migración solo agrega una columna con default -- no toca datos existentes, no requiere backfill.

## Cómo se verificó

- `py_compile` + `pyflakes` sobre los 3 archivos tocados -- limpio.
- `manage.py check` -- sin problemas. `manage.py makemigrations --check --dry-run` -- sin drift (la migración generada refleja el modelo exacto).
- 3 tests nuevos (`wind/tests/test_crypto_tv_aead.py`), corridos contra Postgres real (`pgserver`, efímero):
  1. Flag en `False` (default) -- payload idéntico al legado, **desencriptado de verdad** con la clave privada correspondiente (no solo se revisa el formato, se corrobora que el mensaje round-tripea).
  2. Flag en `True` -- payload GCM con `tag`, IV de 12 bytes (nonce), desencriptado de verdad con la misma privada.
  3. Dos `app_type` distintos, uno con el flag y otro sin -- confirma que activarlo para un cliente no afecta a otro.
  - 3/3 OK.

## Runbook -- cómo activar AEAD para un `app_type` real, cuando corresponda

**No activar sin que el equipo dueño de esa integración (bromteck/cableatlantico, u otro futuro) confirme que el lado que desencripta ya soporta leer el campo `tag` y usar AES-GCM.** Con eso confirmado:

1. En Django shell (o una migración de datos puntual):
   ```python
   from wind.models import AppCredentials
   AppCredentials.objects.filter(app_type="<app_type>", is_active=True).update(supports_aead=True)
   ```
2. Idealmente, coordinar un rollout gradual: crear una **nueva fila** de `AppCredentials` (nueva `app_version`, misma `app_type`, llave nueva) con `supports_aead=True`, mientras la fila vieja sigue activa para dispositivos que no se actualizaron todavía. `hybrid_encrypt_for_app` toma la más reciente no expirada -- así que forzar el corte real es cuestión de `expires_at`/`is_active` en la fila vieja cuando el cliente confirme que ya no queda tráfico viejo.
3. Revertir es trivial: `supports_aead=False` en la fila afectada (o simplemente no activarlo en la fila nueva), sin redeploy de código.

## Fuera de alcance de este cambio

- El proyecto separado `udid`/`FrontUdid` (el sistema real que usa cableatlantico para el pareo operador-controlado, confirmado independiente de Back-Wind-V2) reimplementa el mismo patrón legado por su cuenta, con su propio `AppCredentials`. Ese código no se tocó -- vive en otro repo, gestionado por otro equipo. Documentado acá para que quede registro de la relación entre ambos hallazgos, no como parte de este cambio.
- No se activó `supports_aead=True` para ninguna credencial real -- este cambio deja la puerta lista, no fuerza la migración.
