import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException


def normalize_phone(phone_raw: str, default_region: str = "DO") -> str:
    """
    Normaliza un teléfono a formato E.164 (ej: +18095551234).

    - default_region debe ser ISO-2 (ej: "DO", "US").
    - Lanza ValueError si el número no es válido.
    """
    if phone_raw is None:
        return ""

    phone_raw = str(phone_raw).strip()
    if phone_raw == "":
        return ""

    try:
        num = phonenumbers.parse(phone_raw, default_region)
    except NumberParseException as e:
        raise ValueError("Teléfono inválido") from e

    if not phonenumbers.is_valid_number(num):
        raise ValueError("Teléfono inválido")

    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
