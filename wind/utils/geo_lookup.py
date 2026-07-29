"""
Resolución de país/ciudad a partir de una IP (MaxMind GeoLite2, base .mmdb
local) -- ver `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 4.2.

Uso: mostrar una ubicación aproximada ("Ciudad, País") junto a cada
"dispositivo vinculado" en `GET /wind/devices/`. Es puramente informativo --
NO se usa para ningún control de acceso ni decisión de seguridad, y la
precisión es la típica de geolocalización por IP (aproximada; puede fallar
con VPN, proxies o NAT de operadores móviles).

Requiere `GEOIP_CITY_DB_PATH` configurado (ver `appConfig.GeoIPConfig`)
apuntando a un archivo `GeoLite2-City.mmdb` descargado manualmente --
MaxMind exige una cuenta gratuita + license key para obtenerlo, no se puede
incluir en el repo. Sin esa variable, o si el archivo no existe/no se puede
abrir, `resolve_ip_location()` devuelve país/ciudad en `None` sin lanzar
ninguna excepción -- nunca debe romper la respuesta de un endpoint por esto.
"""
import logging
import threading
from ipaddress import ip_address

from appConfig import GeoIPConfig

logger = logging.getLogger(__name__)

_reader = None
_reader_lock = threading.Lock()
_load_attempted = False

_EMPTY_LOCATION = {"country": None, "city": None}


def _get_reader():
    """
    Abre (una sola vez, con lock) el lector de la base GeoLite2. Si falla o
    no está configurada, se recuerda el intento para no volver a intentar
    abrir un archivo roto/ausente en cada request.
    """
    global _reader, _load_attempted

    if _reader is not None:
        return _reader
    if _load_attempted:
        return None

    with _reader_lock:
        if _reader is not None or _load_attempted:
            return _reader
        _load_attempted = True

        db_path = GeoIPConfig.CITY_DB_PATH
        if not db_path:
            return None

        try:
            import maxminddb

            _reader = maxminddb.open_database(db_path)
        except Exception:
            logger.warning(
                "No se pudo abrir la base GeoLite2 en '%s' -- la ubicación "
                "de dispositivos quedará vacía hasta que se corrija (ver "
                "GEOIP_CITY_DB_PATH en appConfig.GeoIPConfig).",
                db_path,
                exc_info=True,
            )
            _reader = None

    return _reader


def resolve_ip_location(ip: str) -> dict:
    """
    Devuelve {"country": str|None, "city": str|None} para una IP dada.

    Nunca lanza -- ante cualquier problema (IP vacía/inválida/privada, base
    no configurada/no disponible, IP no encontrada en la base, formato
    inesperado) devuelve ambos campos en None en vez de propagar el error.
    """
    if not ip:
        return dict(_EMPTY_LOCATION)

    try:
        parsed = ip_address(ip)
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved:
            # IPs de red privada/local (desarrollo, o un proxy interno que
            # nunca debió llegar hasta acá) no resuelven a ninguna ubicación
            # real -- mejor un campo vacío que una ubicación falsa del rango.
            return dict(_EMPTY_LOCATION)
    except ValueError:
        return dict(_EMPTY_LOCATION)

    reader = _get_reader()
    if reader is None:
        return dict(_EMPTY_LOCATION)

    try:
        result = reader.get(ip)
    except Exception:
        logger.warning("Fallo al resolver geo-IP para una IP dada.", exc_info=True)
        return dict(_EMPTY_LOCATION)

    if not result:
        return dict(_EMPTY_LOCATION)

    try:
        country_names = (result.get("country") or {}).get("names") or {}
        country = country_names.get("es") or country_names.get("en")
        city_names = (result.get("city") or {}).get("names") or {}
        city = city_names.get("es") or city_names.get("en")
    except Exception:
        logger.warning("Formato inesperado en respuesta GeoLite2.", exc_info=True)
        return dict(_EMPTY_LOCATION)

    return {"country": country, "city": city}
