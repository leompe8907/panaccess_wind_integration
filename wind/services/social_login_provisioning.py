"""
Aprovisionamiento PanAccess para login social (Google/Facebook).
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

from appConfig import FeatureConfig
from wind.functions.create_subscriber import _create_subscriber_core
from wind.functions.getSubscriberLoginInfo import CallGetSubscriberLoginInfo
from wind.models import ListOfSubscriber, SubscriberEmailRegistry
from wind.services.subscriber_catalog import resolve_subscriber_code_for_user

logger = logging.getLogger(__name__)

User = get_user_model()


class SocialLoginSubscriberNotFound(Exception):
    """
    Se lanza cuando `FeatureConfig.SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER`
    está activo y el correo del login social no tiene un suscriptor
    existente -- distinta de un fallo real de PanAccess/red, para que el
    caller (wind/adapters.py) pueda devolver un aviso específico ("el
    suscriptor no existe") en vez del error genérico.
    """


def create_subscriber_in_panaccess(
    email,
    first_name,
    last_name,
    auto_generate_code=True,
    comment="",
    is_social_account=False,
    social_provider=None,
):
    """
    Crea el suscriptor invocando directamente la lógica de negocio
    (`_create_subscriber_core`), sin pasar por la vista HTTP pública ni por
    su throttle -- esta es una llamada interna server-to-server (login
    social ya autenticado contra Google/Facebook), no un registro anónimo,
    así que no debe competir por ni depender de saltarse el límite de tasa
    pensado para ese caso. Antes se simulaba un HttpRequest con
    RequestFactory y se marcaba con `wind_internal_create=True` para que
    `RegisterThrottle` lo dejara pasar; con la llamada directa ese atributo
    ya no existe ni hace falta.
    """
    data = {
        "lastName": last_name,
        "firstName": first_name,
        "email": email,
        "comment": comment,
    }
    response = _create_subscriber_core(
        data, is_social_account=is_social_account, social_provider=social_provider,
    )
    return response.data


def normalize_social_email(email: str) -> str:
    return (email or "").strip().lower()


def _link_registry_from_list_subscriber(email: str) -> str | None:
    """Si el email ya está en ListOfSubscriber, crea SubscriberEmailRegistry."""
    subscriber = (
        ListOfSubscriber.objects.filter(emails__iexact=email)
        .exclude(status=ListOfSubscriber.STATUS_CLOSED)
        .first()
    )
    if not subscriber or not subscriber.code:
        return None

    SubscriberEmailRegistry.objects.update_or_create(
        email=email,
        defaults={
            "subscriber_code": subscriber.code,
            "has_purchased": False,
        },
    )
    logger.info(
        "SubscriberEmailRegistry vinculado desde ListOfSubscriber: %s -> %s",
        email,
        subscriber.code,
    )
    return subscriber.code


def ensure_subscriber_for_social_email(
    email: str,
    *,
    first_name: str = "",
    last_name: str = "",
    comment: str = "Creado vía Social Login",
    social_provider: str | None = None,
) -> str | None:
    """
    Garantiza que el email tenga un subscriber_code en SubscriberEmailRegistry.
    Crea el abonado en PanAccess solo si no existe localmente.
    """
    email = normalize_social_email(email)
    if not email:
        return None

    registry = SubscriberEmailRegistry.objects.filter(email__iexact=email).first()
    if registry and registry.subscriber_code:
        return registry.subscriber_code

    code = _link_registry_from_list_subscriber(email)
    if code:
        return code

    if FeatureConfig.SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER:
        # Bandera activa: no auto-registrar. A este punto ya se buscó en
        # SubscriberEmailRegistry y en ListOfSubscriber sin encontrar nada,
        # así que el correo definitivamente no tiene suscriptor todavía.
        logger.info(
            "Login social bloqueado (SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER activo): "
            "no existe suscriptor para %s, no se auto-registra",
            email,
        )
        raise SocialLoginSubscriberNotFound(email)

    if not last_name:
        last_name = "Social Login"
    if not first_name:
        first_name = email.split("@")[0]

    result = create_subscriber_in_panaccess(
        email=email,
        first_name=first_name,
        last_name=last_name,
        auto_generate_code=True,
        comment=comment,
        is_social_account=True,
        social_provider=social_provider,
    )

    if result.get("success"):
        return result.get("subscriber_code")

    logger.warning(
        "create_subscriber falló en login social para %s: %s",
        email,
        result.get("message") or result,
    )

    # Puede fallar por email duplicado si ListOfSubscriber se actualizó entre consultas.
    return _link_registry_from_list_subscriber(email)


def build_panaccess_credentials(subscriber_code: str) -> dict | None:
    """Obtiene login1/password/login2 desde PanAccess."""
    if not subscriber_code:
        return None
    try:
        login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)
    except Exception as exc:
        logger.error(
            "No se pudo obtener login info para %s: %s",
            subscriber_code,
            exc,
            exc_info=True,
        )
        return None

    password = login_info.get("password")
    login1 = login_info.get("login1")
    if not login1 and not password:
        return None

    return {
        "login1": login1,
        "password": password,
        "login2": login_info.get("login2", ""),
        "subscriberCode": subscriber_code,
    }


def resolve_subscriber_code_for_social_user(
    user,
    *,
    first_name: str = "",
    last_name: str = "",
    comment: str = "Creado vía Social Login",
    social_provider: str | None = None,
) -> str | None:
    """
    Resuelve (o crea, siguiendo el flujo de prueba gratis de siempre vía
    `_create_subscriber_core`) el `subscriber_code` PanAccess para un User
    Django tras login social. Extraído de `resolve_panaccess_credentials_for_user`
    para poder reutilizarlo en flujos que NO necesitan (ni deben) obtener el
    password real -- por ejemplo, el pareo de TV por login social (Fase 2):
    ahí solo hace falta saber a qué suscriptor asociar el pareo, el password
    real se cifra y entrega directo a la TV, nunca al celular que hizo login.
    """
    if not user or not getattr(user, "email", None):
        return None

    email = normalize_social_email(user.email)
    subscriber_code = resolve_subscriber_code_for_user(user)

    if not subscriber_code:
        subscriber_code = ensure_subscriber_for_social_email(
            email,
            first_name=first_name or (user.first_name or ""),
            last_name=last_name or (user.last_name or ""),
            comment=comment,
            social_provider=social_provider,
        )

    if not subscriber_code:
        registry = SubscriberEmailRegistry.objects.filter(email__iexact=email).first()
        if registry:
            subscriber_code = registry.subscriber_code

    return subscriber_code


def resolve_panaccess_credentials_for_user(
    user,
    *,
    first_name: str = "",
    last_name: str = "",
    comment: str = "Creado vía Social Login",
    social_provider: str | None = None,
) -> dict | None:
    """
    Resuelve credenciales PanAccess para un User Django tras login social.
    Crea/vincula el suscriptor si hace falta.
    """
    subscriber_code = resolve_subscriber_code_for_social_user(
        user,
        first_name=first_name,
        last_name=last_name,
        comment=comment,
        social_provider=social_provider,
    )
    return build_panaccess_credentials(subscriber_code) if subscriber_code else None
