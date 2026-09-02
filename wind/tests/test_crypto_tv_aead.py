"""
Medio #8 (ver docs/AUDITORIA_CONSOLIDADA_2026-08-24.md y
docs/MIGRACION_AEAD_CREDENCIALES_LEGADAS_2026-09-02.md): confirma que
`AppCredentials.supports_aead` controla de verdad el algoritmo que usa
`hybrid_encrypt_for_app`, y que el default (`False`) reproduce EXACTAMENTE
el payload de siempre (AES-256-CBC, sin campo `tag`) -- la garantía real de
que este cambio no afecta a ningún cliente existente (bromteck/cableatlantico)
hasta que alguien prenda el flag a mano para una credencial puntual.
"""
import base64

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from django.test import TestCase

from wind.models import AppCredentials
from wind.utils.crypto_tv import generate_rsa_key_pair, hybrid_encrypt_for_app


def _decrypt(result: dict, private_key_pem: str) -> bytes:
    """Desencripta un resultado de hybrid_encrypt_for_app/_with_public_key
    con la privada correspondiente -- simula lo que haría el dispositivo,
    para no depender de asserts sobre el formato interno únicamente."""
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None, backend=default_backend()
    )
    aes_key = private_key.decrypt(
        base64.b64decode(result["encrypted_key"]),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    iv = base64.b64decode(result["iv"])
    ciphertext = base64.b64decode(result["encrypted_data"])

    if "tag" in result:
        tag = base64.b64decode(result["tag"])
        cipher = Cipher(algorithms.AES(aes_key), modes.GCM(iv, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return padded[: -padded[-1]]  # quita el padding PKCS-style que usa el CBC legado


class HybridEncryptForAppAeadTestCase(TestCase):
    def setUp(self):
        self.private_pem, self.public_pem = generate_rsa_key_pair()

    def _make_credential(self, app_type="lg", supports_aead=False, app_version="1.0"):
        return AppCredentials.objects.create(
            app_type=app_type,
            app_version=app_version,
            private_key_pem=self.private_pem,
            public_key_pem=self.public_pem,
            is_active=True,
            is_compromised=False,
            supports_aead=supports_aead,
        )

    def test_default_supports_aead_false_keeps_legacy_cbc_payload(self):
        """Sin tocar nada, una credencial nueva es igual a las que ya existen
        en producción -- el payload no debe traer 'tag' ni cambiar el label
        de algoritmo."""
        self._make_credential(supports_aead=False)

        result = hybrid_encrypt_for_app("hola mundo", "lg")

        self.assertEqual(result["algorithm"], "AES-256-CBC + RSA-OAEP")
        self.assertNotIn("tag", result)
        self.assertEqual(len(base64.b64decode(result["iv"])), 16)  # IV de bloque CBC
        self.assertEqual(_decrypt(result, self.private_pem), b"hola mundo")

    def test_supports_aead_true_produces_authenticated_gcm_payload(self):
        """Con el flag prendido a mano para esta credencial puntual, pasa a
        AES-256-GCM -- confirma el formato nuevo y que sigue siendo
        desencriptable con la misma privada."""
        self._make_credential(app_type="samsung", supports_aead=True)

        result = hybrid_encrypt_for_app("hola mundo AEAD", "samsung")

        self.assertEqual(result["algorithm"], "AES-256-GCM + RSA-OAEP")
        self.assertIn("tag", result)
        self.assertEqual(len(base64.b64decode(result["iv"])), 12)  # nonce GCM
        self.assertEqual(_decrypt(result, self.private_pem), b"hola mundo AEAD")

    def test_mixed_app_types_are_independent(self):
        """Dos app_types distintos, uno con el flag y otro sin -- confirma
        que activar AEAD para uno no afecta al otro (el caso real: activar
        para un cliente sin tocar a los demás)."""
        self._make_credential(app_type="lg", supports_aead=False)
        self._make_credential(app_type="samsung", supports_aead=True)

        legacy_result = hybrid_encrypt_for_app("legacy", "lg")
        aead_result = hybrid_encrypt_for_app("aead", "samsung")

        self.assertNotIn("tag", legacy_result)
        self.assertIn("tag", aead_result)
