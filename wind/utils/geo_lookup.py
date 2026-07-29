"""
Resolución de país/ciudad a partir de una IP -- ver
`docs/GUIA_INTEGRACION_UNIFICADA.md` sección 4.2.

Uso: mostrar una ubicación aproximada ("Ciudad, País") junto a cada
"dispositivo vinculado" en `GET /wind/devices/`. Es puramente informativo --
NO se usa para ningún control de acceso ni decisión de seguridad, y la
precisión es la típica de geolocalización por IP (aproximada; puede fallar
con VPN, proxies o NAT de operadores móviles).

Dos fuentes, en orden:

1. Base local .mmdb (GeoLite2-City de MaxMind, o DB-IP City Lite -- mismo
   formato/esquema de campos). Requiere `GEOIP_CITY_DB_PATH` configurado
   (ver `appConfig.GeoIPConfig`). Sin esa variable, o si el archivo no
   existe/no se puede abrir, se pasa directo al paso 2 sin lanzar ninguna
   excepción.
2. Fallback opcional: ip-api.com Pro, SOLO si el paso 1 no encontró nada
   para esa IP (caso raro -- las bases GeoLite2/DB-IP cubren casi todo el
   espacio de IPs anunciadas) y hay `IP_API_KEY` configurada. Es una llamada
   de red con timeout corto -- nunca debe colgar ni romper la respuesta del
   endpoint que la usa.

`resolve_ip_location()` nunca lanza en ningún punto de esta cadena -- ante
cualquier problema (IP vacía/inválida/privada, ninguna fuente configurada o
disponible, IP no encontrada en ninguna, formato inesperado, timeout/error de
red) devuelve ambos campos en `None` en vez de propagar el error.
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
    Abre (una sola vez, con lock) el lector de la base local. Si falla o no
    está configurada, se recuerda el intento para no volver a intentar abrir
    un archivo roto/ausente en cada request.
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
                "No se pudo abrir la base local de geo-IP en '%s' -- se "
                "seguirá usando solo el fallback de ip-api.com (si está "
                "configurado) hasta que se corrija (ver GEOIP_CITY_DB_PATH "
                "en appConfig.GeoIPConfig).",
                db_path,
                exc_info=True,
            )
            _reader = None

    return _reader


def _lookup_local(ip: str) -> dict:
    """Intenta resolver `ip` contra la base local. Nunca lanza."""
    reader = _get_reader()
    if reader is None:
        return dict(_EMPTY_LOCATION)

    try:
        result = reader.get(ip)
    except Exception:
        logger.warning("Fallo al resolver geo-IP local para una IP dada.", exc_info=True)
        return dict(_EMPTY_LOCATION)

    if not result:
        return dict(_EMPTY_LOCATION)

    try:
        country_names = (result.get("country") or {}).get("names") or {}
        country = country_names.get("es") or country_names.get("en")
        city_names = (result.get("city") or {}).get("names") or {}
        city = city_names.get("es") or city_names.get("en")
    except Exception:
        logger.warning("Formato inesperado en respuesta de la base local de geo-IP.", exc_info=True)
        return dict(_EMPTY_LOCATION)

    return {"country": country, "city": city}


def _lookup_ip_api_pro(ip: str) -> dict:
    """
    Fallback opcional vía ip-api.com Pro -- solo se llama cuando la base
    local no encontró nada para esa IP. Nunca lanza: cualquier fallo (sin
    key configurada, timeout, error de red, respuesta con formato
    inesperado) devuelve país/ciudad en `None`.
    """
    if not GeoIPConfig.IP_API_KEY:
        return dict(_EMPTY_LOCATION)

    try:
        import requests

        response = requests.get(
            f"{GeoIPConfig.IP_API_BASE_URL}/{ip}",
            params={
                "key": GeoIPConfig.IP_API_KEY,
                "fields": "status,message,country,city",
                "lang": "es",
            },
            timeout=GeoIPConfig.IP_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning(
            "Fallo consultando ip-api.com Pro como fallback de geo-IP -- se "
            "deja país/ciudad vacíos para esta IP.",
            exc_info=True,
        )
        return dict(_EMPTY_LOCATION)

    if not isinstance(data, dict) or data.get("status") != "success":
        return dict(_EMPTY_LOCATION)

    return {
        "country": data.get("country") or None,
        "city": data.get("city") or None,
    }


def resolve_ip_location(ip: str) -> dict:
    """
    Devuelve {"country": str|None, "city": str|None} para una IP dada,
    intentando primero la base local y, solo si esa no encontró nada,
    ip-api.com Pro como respaldo (si está configurado). Nunca lanza.
    """
    if not ip:
        return dict(_EMPTY_LOCATION)

    try:
        parsed = ip_address(ip)
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved:
            # IPs de red privada/local (desarrollo, o un proxy interno que
            # nunca debió llegar hasta acá) no resuelven a ninguna ubicación
            # real -- mejor un campo vacío que mandarla de más a un tercero
            # (ip-api) o inventar una ubicación falsa del rango.
            return dict(_EMPTY_LOCATION)
    except ValueError:
        return dict(_EMPTY_LOCATION)

    local_result = _lookup_local(ip)
    if local_result["country"] or local_result["city"]:
        return local_result

    return _lookup_ip_api_pro(ip)
