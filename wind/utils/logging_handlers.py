"""
Handlers y filtros personalizados de logging para manejar encoding en Windows.
"""
import logging
import sys
import re


class UnicodeSafeFilter(logging.Filter):
    """
    Filtro que reemplaza caracteres Unicode problemáticos antes de escribir.
    """
    
    EMOJI_REPLACEMENTS = {
        '🚀': '[INIT]',
        '✅': '[OK]',
        '❌': '[ERROR]',
        '🔄': '[RETRY]',
        '🔑': '[AUTH]',
        '🔍': '[CHECK]',
        '⚠️': '[WARN]',
        '🛑': '[STOP]',
        '🚨': '[ALERT]',
    }
    
    def filter(self, record):
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            # Reemplazar emojis conocidos
            for emoji, replacement in self.EMOJI_REPLACEMENTS.items():
                record.msg = record.msg.replace(emoji, replacement)
            
            # Reemplazar cualquier otro emoji o carácter no ASCII problemático
            if sys.platform == 'win32':
                record.msg = re.sub(
                    r'[\U0001F300-\U0001F9FF]|[\u2600-\u27FF]|[\u2700-\u27BF]',
                    '[?]',
                    record.msg
                )
        
        return True


class SafeConsoleHandler(logging.StreamHandler):
    """
    Handler de consola que maneja errores de encoding de forma segura en Windows.
    """
    
    def __init__(self, stream=None):
        if stream is None:
            stream = sys.stdout
        super().__init__(stream)
        
        self.addFilter(UnicodeSafeFilter())
        
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (AttributeError, ValueError):
                pass
    
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            
            try:
                if hasattr(stream, 'buffer'):
                    safe_msg = msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    stream.buffer.write(safe_msg.encode('utf-8', errors='replace'))
                    stream.buffer.write(self.terminator.encode('utf-8', errors='replace'))
                    stream.buffer.flush()
                else:
                    safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
                    stream.write(safe_msg + self.terminator)
                    stream.flush()
            except (UnicodeEncodeError, AttributeError, UnicodeDecodeError) as e:
                safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
                try:
                    stream.write(safe_msg + self.terminator)
                    stream.flush()
                except Exception:
                    self.handleError(record)
                
        except Exception:
            self.handleError(record)
