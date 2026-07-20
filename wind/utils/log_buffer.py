"""
Buffer en memoria para logs de auditoría que se escriben en batch.
Reduce la latencia de requests al evitar escrituras síncronas a la BD.

Durabilidad (RPUSH en Redis): antes, si el proceso se caía entre el add() y
el siguiente flush (hasta batch_size=100 eventos o flush_interval=5s), esos
eventos de auditoría se perdían para siempre -- solo vivían en el deque en
memoria. Ahora cada add() también empuja una copia serializada a una lista
de Redis (RPUSH) antes de devolver el control; esa copia solo se retira
(LREM) cuando el bulk_create a AuthAuditLog confirma éxito. Si el proceso
muere antes de eso, la copia queda en Redis y `recover_pending_audit_logs()`
(tarea periódica `wind.tasks.recover_pending_audit_logs_task`) la recoge y
la escribe más tarde -- ver auditoría, sección "log_buffer.py puede perder
eventos si el proceso cae".
"""
import json
import logging
import threading
import time
import uuid
from collections import deque

from django.db import transaction

logger = logging.getLogger(__name__)

# Lista Redis usada como cola durable de respaldo. Un solo RPUSH por evento;
# se retira con LREM cuando ya quedó escrito en la base de datos.
REDIS_DURABLE_QUEUE_KEY = "wind:audit_log:durable_queue"


def _get_redis_client():
    """Cliente Redis para la cola durable, o None si no está disponible.

    Fallar acá no debe romper el flujo normal de auditoría en memoria --
    Redis caído degrada a "sin durabilidad extra", no a "sin logs".
    """
    try:
        from appConfig import RedisConfig
        return RedisConfig.get_client()
    except Exception as e:
        logger.warning(f"LogBuffer: Redis no disponible para cola durable: {e}")
        return None


def _serialize_log_data(log_data: dict) -> str | None:
    try:
        payload = dict(log_data)
        payload["_durable_id"] = uuid.uuid4().hex
        return json.dumps(payload, default=str)
    except Exception as e:
        logger.warning(f"LogBuffer: no se pudo serializar evento para cola durable: {e}")
        return None


def _build_model_from_durable_payload(payload: dict):
    from wind.models import AuthAuditLog

    payload = dict(payload)
    payload.pop("_durable_id", None)
    return AuthAuditLog(**payload)


class LogBuffer:
    def __init__(self, batch_size=100, flush_interval=5):
        self.buffer = deque()
        self.lock = threading.Lock()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.last_flush = time.time()
        self._shutdown = False
        self._start_flush_thread()

    def _start_flush_thread(self):
        def flush_periodic():
            while not self._shutdown:
                try:
                    time.sleep(self.flush_interval)
                    if not self._shutdown:
                        self.flush()
                except (SystemExit, KeyboardInterrupt):
                    break
                except Exception as e:
                    logger.error(f"Error in flush thread: {e}", exc_info=True)

        thread = threading.Thread(target=flush_periodic, daemon=True, name="LogBufferFlush")
        thread.start()
        logger.info(f"LogBuffer flush thread started (interval={self.flush_interval}s, batch_size={self.batch_size})")

    def add(self, log_data):
        # Empuja primero a Redis (durable) y solo después al buffer en
        # memoria -- si el proceso muere justo después de este add(), la
        # copia en Redis sobrevive aunque el deque en memoria no.
        serialized = _serialize_log_data(log_data)
        if serialized is not None:
            client = _get_redis_client()
            if client is not None:
                try:
                    client.rpush(REDIS_DURABLE_QUEUE_KEY, serialized)
                except Exception as e:
                    logger.warning(f"LogBuffer: RPUSH a cola durable falló, evento no durable: {e}")
                    serialized = None

        with self.lock:
            self.buffer.append((log_data, serialized))
            if len(self.buffer) >= self.batch_size:
                self._flush_internal()

    def flush(self):
        with self.lock:
            self._flush_internal()

    def _flush_internal(self):
        if not self.buffer:
            return

        logs_to_write = list(self.buffer)
        buffer_size = len(logs_to_write)
        self.buffer.clear()
        self.last_flush = time.time()

        def write_to_db():
            from django.db.utils import OperationalError, DatabaseError
            from wind.utils.db_utils import is_connection_error, reconnect_database

            max_retries = 3
            retry_count = 0

            while retry_count < max_retries:
                try:
                    with transaction.atomic():
                        from wind.models import AuthAuditLog
                        AuthAuditLog.objects.bulk_create([
                            AuthAuditLog(**log_data) for log_data, _serialized in logs_to_write
                        ], ignore_conflicts=True)
                    logger.debug(f"LogBuffer: Wrote {buffer_size} logs to DB")
                    _remove_from_durable_queue(logs_to_write)
                    return
                except (OperationalError, DatabaseError) as e:
                    if is_connection_error(e):
                        retry_count += 1
                        logger.warning(f"LogBuffer: Conexión perdida (intento {retry_count}/{max_retries}). Reconectando...")
                        reconnect_database()
                        if retry_count < max_retries:
                            time.sleep(2 * retry_count)
                            continue
                        else:
                            logger.error("LogBuffer: No se pudo reconectar después de los intentos")
                            return
                    else:
                        logger.error(f"LogBuffer: Error de BD: {e}", exc_info=True)
                        return
                except Exception as e:
                    logger.error(f"LogBuffer: Error escribiendo logs: {e}", exc_info=True)
                    return

        write_thread = threading.Thread(target=write_to_db, daemon=True)
        write_thread.start()

    def shutdown(self):
        self._shutdown = True
        self.flush()
        logger.info("LogBuffer shutdown completed")


def _remove_from_durable_queue(logs_written) -> None:
    """Retira de la cola Redis los eventos que ya se confirmaron en la BD.

    Usa LREM por valor exacto (no LPOP por cantidad) porque el bulk_create
    que dispara esto corre en un hilo aparte, y varios flushes pueden estar
    en vuelo al mismo tiempo -- LREM por valor es seguro sin importar el
    orden en que terminen esos hilos; un LPOP por cantidad no lo sería.
    """
    client = _get_redis_client()
    if client is None:
        return
    for _log_data, serialized in logs_written:
        if not serialized:
            continue
        try:
            client.lrem(REDIS_DURABLE_QUEUE_KEY, 1, serialized)
        except Exception as e:
            logger.warning(f"LogBuffer: no se pudo retirar evento de la cola durable: {e}")


def recover_pending_audit_logs(limit=1000):
    """
    Red de seguridad: escribe a AuthAuditLog cualquier evento que haya
    quedado en la cola durable de Redis sin confirmarse (proceso caído entre
    el RPUSH y el bulk_create). Pensada para correr como tarea periódica de
    Celery (wind.tasks.recover_pending_audit_logs_task), no en el hot path.
    """
    from wind.models import AuthAuditLog

    client = _get_redis_client()
    if client is None:
        return {"recovered": 0, "errors": 0, "skipped": True}

    try:
        raw_items = client.lrange(REDIS_DURABLE_QUEUE_KEY, 0, limit - 1)
    except Exception as e:
        logger.error(f"LogBuffer: no se pudo leer la cola durable de Redis: {e}")
        return {"recovered": 0, "errors": 0, "skipped": True}

    if not raw_items:
        return {"recovered": 0, "errors": 0, "skipped": False}

    recovered = 0
    errors = 0
    to_remove = []

    payloads = []
    for raw in raw_items:
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            payload = json.loads(text)
            payloads.append((raw, payload))
        except Exception as e:
            logger.error(f"LogBuffer: entrada inválida en cola durable, se descarta: {e}")
            errors += 1
            to_remove.append(raw)

    if payloads:
        try:
            with transaction.atomic():
                AuthAuditLog.objects.bulk_create(
                    [_build_model_from_durable_payload(payload) for _raw, payload in payloads],
                    ignore_conflicts=True,
                )
            recovered = len(payloads)
            to_remove.extend(raw for raw, _payload in payloads)
        except Exception as e:
            logger.error(f"LogBuffer: error recuperando {len(payloads)} eventos de la cola durable: {e}")
            errors += len(payloads)

    for raw in to_remove:
        try:
            client.lrem(REDIS_DURABLE_QUEUE_KEY, 1, raw)
        except Exception as e:
            logger.warning(f"LogBuffer: no se pudo limpiar entrada de la cola durable: {e}")

    if recovered or errors:
        logger.info(
            "LogBuffer: recuperación de cola durable -- recuperados=%s errores=%s",
            recovered,
            errors,
        )

    return {"recovered": recovered, "errors": errors, "skipped": False}


_log_buffer = LogBuffer(batch_size=100, flush_interval=5)


def log_audit_async(action_type, **kwargs):
    try:
        log_data = {
            'action_type': action_type,
            **kwargs
        }
        _log_buffer.add(log_data)
    except Exception as e:
        logger.error(f"Error adding log to buffer: {e}", exc_info=True)


def flush_logs():
    _log_buffer.flush()


def shutdown_log_buffer():
    _log_buffer.shutdown()
