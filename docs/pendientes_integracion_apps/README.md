# Documentación por funcionalidad para apps (iOS / Android / appVideo)

Un documento por funcionalidad, pensado para que el equipo de apps (mobile nativo especialmente) pueda implementar cada una sin tener que leer toda `docs/GUIA_INTEGRACION_UNIFICADA.md`. Cada doc dice explícitamente qué ya está implementado en appVideo (web/TV) y qué le falta a iOS/Android nativo -- esto no es solo para appVideo.

Fuente completa y autoritativa (si algo difiere, vale lo que dice ahí): `docs/GUIA_INTEGRACION_UNIFICADA.md`.

| # | Doc | Funcionalidad | Estado |
|---|---|---|---|
| 1 | [`01_eliminar_cuenta.md`](01_eliminar_cuenta.md) | Eliminar cuenta (cierre inmediato + con demora) | Cierre inmediato listo en appVideo. Eliminación con demora: backend listo, **sin implementar en ningún cliente** todavía. |
| 2 | [`02_cambiar_contrasena.md`](02_cambiar_contrasena.md) | Cambiar contraseña | Listo en backend y appVideo. iOS/Android debe replicar el mismo contrato (incluido `oldPass` obligatorio y re-login tras el cambio). |
| 3 | [`03_olvidar_contrasena.md`](03_olvidar_contrasena.md) | Olvidé mi contraseña (incluye `origin=app`) | Listo en backend. appVideo implementa el camino "redirigir a la página del backend"; el camino de modal 100% nativo queda para iOS/Android si lo eligen. |
| 4 | [`04_fingerprint.md`](04_fingerprint.md) | Fingerprint de dispositivo evadible (pareo de TV) | Mitigado en backend. Informativo -- no requiere acción de iOS/Android. |
| 5 | [`05_login_social_tv.md`](05_login_social_tv.md) | Login social (Google/Facebook) y su uso para autorizar TVs | Listo en backend. Login social básico implementado en appVideo; el combinado con pareo de TV le toca a mobile. |
| 6 | [`06_vincular_dispositivo.md`](06_vincular_dispositivo.md) | Vincular dispositivo (pareo de TV por QR/código) | Backend y TV listos. Falta Universal Link (iOS) / App Link (Android) para abrir la app directo al escanear -- mejora de UX, no bloqueante. |
| 7 | [`07_dispositivos_vinculados.md`](07_dispositivos_vinculados.md) | Dispositivos vinculados (listar/revocar sesiones) | Listo en backend y appVideo (con 2 gaps menores documentados). iOS/Android debe implementar el protocolo completo desde cero. |
| 8 | [`08_recaptcha.md`](08_recaptcha.md) | reCAPTCHA en acciones sensibles desde mobile nativo | Decisión de producto pendiente -- reCAPTCHA v3 web no tiene equivalente nativo directo. |
| 9 | [`09_preferencias_sincronizadas.md`](09_preferencias_sincronizadas.md) | Preferencias sincronizadas (control parental + favoritos) | Listo en backend y appVideo. Solo necesita el JWT de sesión -- iOS/Android puede implementarlo sin depender de "dispositivos vinculados". |
| 10 | [`10_logs_diagnostico.md`](10_logs_diagnostico.md) | Logs de diagnóstico para desarrolladores | Listo en backend y appVideo. Pensado explícitamente para que iOS/Android lo implemente desde el día uno -- no requiere JWT, sí una API key propia a pedir a backend. |

## Notas cruzadas que no son una funcionalidad aparte

- **Contraseña en texto plano** (correo de bienvenida + respuesta de login social): riesgo de negocio ya aceptado, sin acción pendiente -- documentado como nota de seguridad dentro de `05_login_social_tv.md`.
- **Brand `bromteck` con UDID apuntando a `http://127.0.0.1`**: pendiente de decisión de producto (¿está en uso en producción?), documentado dentro de `06_vincular_dispositivo.md`.
