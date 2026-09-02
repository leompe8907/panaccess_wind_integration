import os
import base64
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from wind.models import AppCredentials

def generate_rsa_key_pair(key_size=2048):
    """
    Genera un par de claves RSA
    Returns: (private_key_pem, public_key_pem)
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend()
    )
    
    public_key = private_key.public_key()
    
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')
    
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    return private_pem, public_pem


def _hybrid_encrypt_with_public_key(
    plaintext: str, public_key_pem: str, *, app_type: str | None = None, use_aead: bool = False
) -> dict:
    """
    Núcleo común de cifrado híbrido para una llave pública ya resuelta --
    usado tanto por el esquema histórico de llave estática por `app_type`
    (`hybrid_encrypt_for_app`) como por el esquema de llave efímera por
    pareo (`hybrid_encrypt_for_device_public_key`).

    `use_aead` decide el modo de AES:

    - `False` (default) -- AES-256-CBC sin autenticación, el comportamiento
      histórico intacto. Sigue siendo lo que `hybrid_encrypt_for_app` manda
      por default: ese esquema de llave estática por `app_type` tiene HOY
      clientes reales en producción (bromteck/cableatlantico, ver
      docs/MIGRACION_AEAD_CREDENCIALES_LEGADAS_2026-09-02.md) desencriptando
      ese formato exacto en TVs ya desplegadas -- cambiarle el algoritmo o
      el formato del payload sin coordinar con ese equipo sería un
      incidente de producción para un cliente que no tiene nada que ver
      con este cambio (revisión adversarial). Desde el 2026-09-02,
      `hybrid_encrypt_for_app` puede pedir `True` acá también, pero solo si
      la credencial (`AppCredentials.supports_aead`) fue marcada a mano
      para un `app_type` puntual, después de confirmar el lado cliente.
    - `True` -- AES-256-GCM, modo autenticado: antes CBC daba
      confidencialidad pero nada garantizaba que el ciphertext no fue
      alterado en tránsito (vulnerable a bit-flipping/padding oracle, sin
      HMAC). GCM agrega un tag de integridad en la misma operación de
      cifrado, sin padding manual (es un modo de flujo, no de bloque). Es
      lo que siempre usa `hybrid_encrypt_for_device_public_key` (esquema de
      llave efímera por pareo, sin clientes legados que romper), y lo que
      `hybrid_encrypt_for_app` usa opt-in por credencial (ver arriba).
    """
    public_key = serialization.load_pem_public_key(
        public_key_pem.encode(),
        backend=default_backend()
    )

    aes_key = os.urandom(32)
    extra_fields = {}

    if use_aead:
        iv = os.urandom(12)  # 96 bits -- tamaño de nonce recomendado para GCM
        cipher = Cipher(algorithms.AES(aes_key), modes.GCM(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        aes_encrypted_data = encryptor.update(plaintext.encode('utf-8')) + encryptor.finalize()
        # El tag de autenticación (16 bytes) no es secreto, viaja junto al
        # resto -- es lo que permite al que desencripta detectar
        # alteraciones del ciphertext antes de confiar en el resultado.
        extra_fields["tag"] = base64.b64encode(encryptor.tag).decode('utf-8')
        algorithm_label = "AES-256-GCM + RSA-OAEP"
    else:
        iv = os.urandom(16)
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()

        plaintext_bytes = plaintext.encode('utf-8')
        padding_length = 16 - (len(plaintext_bytes) % 16)
        padded_plaintext = plaintext_bytes + bytes([padding_length] * padding_length)

        aes_encrypted_data = encryptor.update(padded_plaintext) + encryptor.finalize()
        algorithm_label = "AES-256-CBC + RSA-OAEP"

    rsa_encrypted_aes_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )

    result = {
        "encrypted_data": base64.b64encode(aes_encrypted_data).decode('utf-8'),
        "encrypted_key": base64.b64encode(rsa_encrypted_aes_key).decode('utf-8'),
        "iv": base64.b64encode(iv).decode('utf-8'),
        "algorithm": algorithm_label,
        **extra_fields,
    }
    if app_type is not None:
        result["app_type"] = app_type
    return result


def hybrid_encrypt_for_app(plaintext: str, app_type: str) -> dict:
    """
    Encriptación híbrida con la llave pública estática registrada para un
    `app_type` en `AppCredentials`. Se conserva sin cambios de contrato por
    default para el/los integrador(es) que ya dependen de este esquema.

    Antes: `.get(app_type=app_type, is_active=True)` -- no filtraba
    `is_compromised` y, si llegaran a existir dos filas activas para el
    mismo `app_type` con distinta `app_version`, reventaba con
    `MultipleObjectsReturned` (ver auditoría). Ahora se filtra
    explícitamente por `is_active`/`is_compromised=False` y se toma la más
    reciente entre las que además no estén expiradas.

    Medio #8 (2026-09-02, ver docs/MIGRACION_AEAD_CREDENCIALES_LEGADAS_2026-09-02.md):
    `use_aead` ya no es un `False` fijo -- se lee de
    `AppCredentials.supports_aead` de la credencial resuelta. Default de esa
    columna es `False`, así que sin acción explícita esto sigue devolviendo
    exactamente el mismo AES-256-CBC de siempre para cualquier app_type
    existente. Solo produce AES-256-GCM (payload con campo `tag` extra) para
    una credencial que alguien marcó `supports_aead=True` a mano, después de
    confirmar que el lado que desencripta ya lo soporta -- ver el runbook en
    ese doc antes de activarlo para un app_type real.
    """
    try:
        candidates = (
            AppCredentials.objects
            .filter(app_type=app_type, is_active=True, is_compromised=False)
            .order_by('-created_at')
        )
        app_credentials = next((c for c in candidates if not c.is_expired()), None)
        if app_credentials is None:
            raise AppCredentials.DoesNotExist

        return _hybrid_encrypt_with_public_key(
            plaintext,
            app_credentials.public_key_pem,
            app_type=app_type,
            use_aead=app_credentials.supports_aead,
        )
    except AppCredentials.DoesNotExist:
        raise Exception(f"No se encontraron claves activas para app_type={app_type}")
    except Exception as e:
        raise Exception(f"Error de encriptación híbrida: {str(e)}")


def hybrid_encrypt_for_device_public_key(plaintext: str, device_public_key_pem: str) -> dict:
    """
    Encriptación híbrida para el esquema de llave efímera por pareo (ver
    `UDIDAuthRequest.device_public_key`): el propio dispositivo genera su
    par de llaves RSA al pedir el UDID (vía WebCrypto del lado del
    cliente) y manda solo la pública; acá se cifra específicamente para
    esa llave de esa sesión de pareo, sin consultar `AppCredentials`. La
    privada nunca sale del dispositivo ni se persiste en el backend.

    Es el camino nuevo para el pareo TV/QR con login social;
    `hybrid_encrypt_for_app` se conserva intacto para el otro integrador
    que sigue usando llaves estáticas por `app_type`.
    """
    if not device_public_key_pem:
        raise Exception("El pareo no tiene una llave pública de dispositivo registrada.")
    try:
        return _hybrid_encrypt_with_public_key(plaintext, device_public_key_pem, use_aead=True)
    except Exception as e:
        raise Exception(f"Error de encriptación híbrida (llave efímera): {str(e)}")


def verify_app_can_decrypt(app_type: str) -> bool:
    try:
        return AppCredentials.objects.filter(
            app_type=app_type, is_active=True, is_compromised=False
        ).exists()
    except Exception:
        return False
