"""
Política de contraseña de PanAccess, replicada localmente.

Motivo (ver auditoría "BACKEND_CHANGE_PASSWORD_VALIDATION_ISSUE"): antes,
cualquier contraseña que no cumpliera la política de PanAccess se rechazaba
recién en `resetSubscriberPassword`, y ese rechazo (un error de *input* del
cliente) se devolvía como HTTP 502 -- código reservado para fallas reales
de conectividad/disponibilidad hacia PanAccess. Un 502 le dice al cliente
"no es tu culpa, reintenta más tarde", cuando en realidad el problema es
corregible de inmediato (la contraseña no cumple el formato).

Esta validación local permite rechazar con 400 + código estable ANTES de
llamar a PanAccess, sin depender de parsear el texto en español que
PanAccess devuelve. La regla se copió del mensaje de error real observado
en producción para `resetSubscriberPassword`:

    "La clave ingresada es incorrecta. Debería ser (a-z, A-Z, 0-9, -, _).
     La clave debe tener al menos un número y una letra mayúscula, Tambien
     la clave tiene que estar comprendida entre 8 y 255 caracteres."

IMPORTANTE -- charset de caracteres especiales (ver conversación): ese
mensaje de PanAccess NO incluye caracteres especiales en el charset
permitido, solo `a-z, A-Z, 0-9, -, _`. Se amplió el charset local (abajo,
`SPECIAL_CHARS`) a pedido, pero esto SOLO relaja la validación local -- no
cambia lo que PanAccess realmente acepta. Si PanAccess sigue rechazando
esos caracteres, una contraseña que pase esta validación local de todos
modos va a volver con 400 `code=password_rejected_by_panaccess` (no es un
bug: es exactamente para este caso que existe ese camino, ver
`profile_password_view`/`change_password_view`). Antes de anunciar al
equipo Android/iOS que ya se pueden usar caracteres especiales, confirmar
con `deploy/test_password_policy_probe.py` que PanAccess de verdad los
acepta.

Importante (independiente de lo anterior): esta validación sigue siendo
un ATAJO para el caso común, no un reemplazo de la validación real de
PanAccess. Si PanAccess cambia su política sin avisar, la llamada real
sigue siendo la fuente de verdad -- por eso las vistas que usan esto
igual deben seguir manejando el rechazo que pueda venir de PanAccess (ver
`PanAccessAPIError` en wind/api/profile/views.py y
wind/functions/change_password.py).
"""
from __future__ import annotations

import re

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 255

# Caracteres especiales agregados al charset local (ver aviso arriba). No
# incluye espacio ni caracteres de control -- ninguno de los dos tiene
# sentido en una contraseña y complicarían el copy/paste en apps móviles.
# (No es raw-string a propósito: un raw-string no puede terminar en un
# backslash literal sin escaparlo, y aquí hace falta uno.)
SPECIAL_CHARS = "!@#$%^&*()+=[]{};:'\",.<>/?~`|\\"

# Construcción de la clase de caracteres del regex: backslash y corchetes
# escapados explícitamente, guion al final (fuera de eso se interpretaría
# como rango en vez de literal).
_SPECIAL_CHARS_FOR_REGEX = (
    SPECIAL_CHARS.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
)
_ALLOWED_CHARS_RE = re.compile(
    r"^[a-zA-Z0-9_" + _SPECIAL_CHARS_FOR_REGEX + r"-]+$"
)
_HAS_UPPER_RE = re.compile(r"[A-Z]")
_HAS_DIGIT_RE = re.compile(r"[0-9]")

PASSWORD_POLICY_CODE = "password_policy_violation"

PASSWORD_POLICY_MESSAGE = (
    "La contraseña debe tener entre 8 y 255 caracteres, incluir al menos "
    "una letra mayúscula y un número, y solo puede contener letras, "
    "números, y los caracteres " + SPECIAL_CHARS + " (incluye - y _)."
)


def validate_password_policy(password: str | None) -> str | None:
    """
    Valida `password` contra la política local (copia de la de PanAccess).

    Devuelve None si cumple, o `PASSWORD_POLICY_MESSAGE` si no. No lanza
    excepción -- el llamador decide qué hacer con el resultado (permite
    usarla tanto desde un serializer de DRF como desde una vista que no
    use serializer, p. ej. `change_password_view`).
    """
    if not password:
        return PASSWORD_POLICY_MESSAGE
    if not (PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH):
        return PASSWORD_POLICY_MESSAGE
    if not _ALLOWED_CHARS_RE.match(password):
        return PASSWORD_POLICY_MESSAGE
    if not _HAS_UPPER_RE.search(password):
        return PASSWORD_POLICY_MESSAGE
    if not _HAS_DIGIT_RE.search(password):
        return PASSWORD_POLICY_MESSAGE
    return None
