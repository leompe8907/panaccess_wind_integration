"""
Verificación server-side de Google reCAPTCHA v3.

Mitigación contra bots/scripts/IA creando abonados masivamente vía
/wind/create-subscriber/ (endpoint público, ver auditoría). Opt-in: si
RECAPTCHA_SECRET_KEY no está configurado, `verify_recaptcha` no bloquea nada
-- así no rompe clientes que todavía no envían el token mientras se
coordina la integración con el frontend/app.
"""
import logging

import requests

from appConfig import RecaptchaConfig

logger = logging.getLogger(__name__)

_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


def recaptcha_required() -> bool:
    return bool(RecaptchaConfig.SECRET_KEY)


def verify_recaptcha(token: str | None, remote_ip: str | None = None) -> tuple[bool, str | None]:
    """
    Devuelve (ok, error_message).

    - Si RECAPTCHA_SECRET_KEY no está configurado: (True, None) -- feature
      deshabilitado por defecto (opt-in).
    - Si está configurado pero no llega token: rechaza.
    - Si Google no puede verificar (error de red/timeout): rechaza
      (fail-closed) en vez de dejar pasar silenciosamente un fallo que un
      atacante podría inducir.
    - Si el score v3 viene por debajo de RECAPTCHA_MIN_SCORE: rechaza.
    """
    if not recaptcha_required():
        return True, None

    if not token:
        return False, "Falta el token de reCAPTCHA."

    payload = {
        "secret": RecaptchaConfig.SECRET_KEY,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        resp = requests.post(_VERIFY_URL, data=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        logger.error("Error verificando reCAPTCHA: %s", exc, exc_info=True)
        return False, "No se pudo verificar reCAPTCHA en este momento. Intenta de nuevo."

    if not result.get("success"):
        logger.warning("reCAPTCHA rechazado: %s", result.get("error-codes"))
        return False, "Verificación reCAPTCHA fallida."

    score = result.get("score")
    if score is not None and score < RecaptchaConfig.MIN_SCORE:
        logger.warning(
            "reCAPTCHA score bajo (%.2f < %.2f), rechazando", score, RecaptchaConfig.MIN_SCORE
        )
        return False, "Verificación reCAPTCHA insuficiente."

    return True, None
