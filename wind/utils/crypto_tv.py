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


def rsa_encrypt_for_app(plaintext: str, app_type: str) -> str:
    """
    Encripta datos usando la clave PRIVADA del backend
    La app usará la clave PÚBLICA para desencriptar
    """
    try:
        app_credentials = AppCredentials.objects.get(app_type=app_type, is_active=True)
        
        private_key = serialization.load_pem_private_key(
            app_credentials.private_key_pem.encode(),
            password=None,
            backend=default_backend()
        )
        
        encrypted = private_key.private_key().encrypt(
            plaintext.encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        
        return base64.b64encode(encrypted).decode('utf-8')
        
    except AppCredentials.DoesNotExist:
        raise Exception(f"No se encontraron claves activas para app_type={app_type}")
    except Exception as e:
        raise Exception(f"Error de encriptación: {str(e)}")


def hybrid_encrypt_for_app(plaintext: str, app_type: str) -> dict:
    """
    Encriptación híbrida segura:
    1. Genera clave AES aleatoria
    2. Encripta datos con AES
    3. Encripta clave AES con RSA pública de la app
    4. La app con clave privada puede desencriptar
    """
    try:
        app_credentials = AppCredentials.objects.get(app_type=app_type, is_active=True)
        
        public_key = serialization.load_pem_public_key(
            app_credentials.public_key_pem.encode(),
            backend=default_backend()
        )
        
        aes_key = os.urandom(32)
        iv = os.urandom(16)
        
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        
        plaintext_bytes = plaintext.encode('utf-8')
        padding_length = 16 - (len(plaintext_bytes) % 16)
        padded_plaintext = plaintext_bytes + bytes([padding_length] * padding_length)
        
        aes_encrypted_data = encryptor.update(padded_plaintext) + encryptor.finalize()
        
        rsa_encrypted_aes_key = public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        
        return {
            "encrypted_data": base64.b64encode(aes_encrypted_data).decode('utf-8'),
            "encrypted_key": base64.b64encode(rsa_encrypted_aes_key).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8'),
            "algorithm": "AES-256-CBC + RSA-OAEP",
            "app_type": app_type
        }
        
    except AppCredentials.DoesNotExist:
        raise Exception(f"No se encontraron claves activas para app_type={app_type}")
    except Exception as e:
        raise Exception(f"Error de encriptación híbrida: {str(e)}")


def verify_app_can_decrypt(app_type: str) -> bool:
    try:
        return AppCredentials.objects.filter(app_type=app_type, is_active=True).exists()
    except Exception:
        return False
