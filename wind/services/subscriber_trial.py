"""
Elegibilidad y registro del periodo de prueba (trial) por email/documento.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from appConfig import PanaccessConfig
from wind.models import SubscriberDocumentRegistry, SubscriberEmailRegistry

DEFAULT_TRIAL_DAYS = 30


def registration_trial_days() -> int:
    raw = getattr(PanaccessConfig, "REGISTRATION_TRIAL_DAYS", None)
    try:
        days = int(raw) if raw is not None else DEFAULT_TRIAL_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_TRIAL_DAYS
    return max(1, days)


def is_account_closed(registry) -> bool:
    return bool(registry and registry.account_closed_at)


def is_eligible_for_trial(
    *,
    email: str | None = None,
    document: str | None = None,
) -> bool:
    """
    True solo si el email/documento pueden recibir el producto trial de registro.
    Cuentas cerradas (re-registro Opción B) y trials previos → False.
    """
    email_norm = (email or "").strip().lower()
    document_norm = (document or "").strip().upper()

    if email_norm:
        reg = SubscriberEmailRegistry.objects.filter(email__iexact=email_norm).first()
        if reg:
            if reg.has_purchased:
                return bool(reg.eligible_for_trial)
            if is_account_closed(reg) or reg.trial_used or not reg.eligible_for_trial:
                return False

    if document_norm:
        reg = SubscriberDocumentRegistry.objects.filter(document=document_norm).first()
        if reg:
            if reg.has_purchased:
                return bool(reg.eligible_for_trial)
            if is_account_closed(reg) or reg.trial_used or not reg.eligible_for_trial:
                return False

    return True


def mark_trial_granted(
    *,
    email: str,
    document: str | None,
    subscriber_code: str,
    granted_at=None,
) -> None:
    """Marca trial concedido en ambos registry (tombstone antifraude)."""
    granted_at = granted_at or timezone.now()
    expires_at = granted_at + timedelta(days=registration_trial_days())
    email_norm = email.strip().lower()
    document_norm = (document or "").strip().upper() or None

    defaults = {
        "subscriber_code": subscriber_code,
        "trial_used": True,
        "trial_granted_at": granted_at,
        "trial_expires_at": expires_at,
        "eligible_for_trial": False,
        "account_closed_at": None,
        "closed_subscriber_code": None,
    }

    SubscriberEmailRegistry.objects.update_or_create(
        email=email_norm,
        defaults={**defaults, "document": document_norm},
    )

    if document_norm:
        SubscriberDocumentRegistry.objects.update_or_create(
            document=document_norm,
            defaults={**defaults, "email": email_norm},
        )
