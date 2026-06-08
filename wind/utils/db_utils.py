"""
Utilidades para manejo de conexiones de base de datos con reconexión automática.
"""
import logging
import time
from django.db import connection
from django.db.utils import OperationalError, DatabaseError

logger = logging.getLogger(__name__)

MYSQL_CONNECTION_ERRORS = [
    '2006',
    '2013',
    'Server has gone away',
    'Lost connection',
    'Connection lost',
    'Broken pipe',
]

POSTGRESQL_CONNECTION_ERRORS = [
    'server closed the connection',
    'connection to server was lost',
    'terminating connection due to administrator command',
    'connection unexpectedly closed',
    'could not receive data from server',
    'connection refused',
    'FATAL: terminating connection',
]

ALL_CONNECTION_ERRORS = MYSQL_CONNECTION_ERRORS + POSTGRESQL_CONNECTION_ERRORS


def is_connection_error(error):
    error_str = str(error).lower()
    for error_pattern in ALL_CONNECTION_ERRORS:
        if error_pattern.lower() in error_str:
            return True
    return False


def reconnect_database():
    try:
        connection.close()
        logger.debug("🔌 Conexión a BD cerrada, se reconectará automáticamente")
    except Exception as e:
        logger.warning(f"Error al cerrar conexión: {str(e)}")


def execute_with_reconnect(func, max_retries=3, retry_delay=2, *args, **kwargs):
    retry_count = 0
    while retry_count < max_retries:
        try:
            return func(*args, **kwargs)
        except (OperationalError, DatabaseError) as e:
            if is_connection_error(e):
                retry_count += 1
                logger.warning(
                    f"🔌 Conexión a BD perdida (intento {retry_count}/{max_retries}). Reconectando..."
                )
                reconnect_database()
                if retry_count < max_retries:
                    delay = retry_delay * retry_count
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"❌ No se pudo reconectar a la BD después de {max_retries} intentos"
                    )
                    raise DatabaseError(
                        f"No se pudo reconectar a la BD después de {max_retries} intentos: {str(e)}"
                    )
            else:
                logger.error(f"❌ Error de base de datos: {str(e)}")
                raise
        except Exception:
            raise
    raise DatabaseError(f"No se pudo ejecutar la operación después de {max_retries} intentos")
