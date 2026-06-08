"""
Excepciones personalizadas para el manejo de errores de PanAccess.
"""

class PanAccessException(Exception):
    """Excepción base para todos los errores relacionados con PanAccess."""
    pass


class PanAccessAuthenticationError(PanAccessException):
    """Error de autenticación con PanAccess (credenciales inválidas, API key deshabilitada, etc.)."""
    pass


class PanAccessSessionError(PanAccessException):
    """Error relacionado con la sesión de PanAccess (sesión expirada, inválida, etc.)."""
    pass


class PanAccessRateLimitError(PanAccessException):
    """Error cuando se excede el límite de rate limiting (más de 20 logins en 5 minutos)."""
    pass


class PanAccessConnectionError(PanAccessException):
    """Error de conexión con el servidor de PanAccess."""
    pass


class PanAccessTimeoutError(PanAccessException):
    """Error de timeout al comunicarse con PanAccess."""
    pass


class PanAccessAPIError(PanAccessException):
    """Error genérico de la API de PanAccess."""
    
    def __init__(self, message, status_code=None, error_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
