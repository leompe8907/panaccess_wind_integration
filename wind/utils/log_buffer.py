"""
Buffer en memoria para logs de auditoría que se escriben en batch.
Reduce la latencia de requests al evitar escrituras síncronas a la BD.
"""
import threading
import time
import logging
from collections import deque
from django.db import transaction

logger = logging.getLogger(__name__)


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
        with self.lock:
            self.buffer.append(log_data)
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
                            AuthAuditLog(**log_data) for log_data in logs_to_write
                        ], ignore_conflicts=True)
                    logger.debug(f"LogBuffer: Wrote {buffer_size} logs to DB")
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
