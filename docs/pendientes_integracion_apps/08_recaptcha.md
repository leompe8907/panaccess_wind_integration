# reCAPTCHA en "olvidé contraseña" / "eliminar cuenta" desde iOS/Android nativo

Estado: decisión de negocio/arquitectura pendiente, sin definir todavía.
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` secciones 5.2/5.3/5.4 y 10; `01_eliminar_cuenta.md`, `03_olvidar_contrasena.md`.

## Qué es

`requestPasswordReset()`, `closeAccount()` y `requestAccountDeletion()` (ver `01_eliminar_cuenta.md`) mandan un `recaptcha_token` opcional. El backend (`wind/utils/recaptcha.py`) solo lo exige si tiene `RECAPTCHA_SECRET_KEY` configurado -- es opt-in de los dos lados, así que hoy nada se rompe si no se manda.

El problema es que `recaptchaService.js` (appVideo) está armado **exclusivamente para web**: carga el script de Google (`https://www.google.com/recaptcha/api.js`) con `document.createElement('script')` y usa `window.grecaptcha.execute(...)` -- eso solo existe en un navegador o WebView. Una app **nativa** iOS/Android (sin WebView de por medio) no tiene `document`/`window`, así que un equivalente de `getRecaptchaToken()` en ese contexto siempre devolvería `null` -- no falla, pero tampoco protege nada.

## Por qué no se resolvió ya

No es un bug a corregir, es una decisión que le corresponde a producto/mobile antes de escribir código: reCAPTCHA v3 clásico (el que ya usa la web) no tiene una versión nativa equivalente. Las opciones reales son:

1. **reCAPTCHA Enterprise con SDK nativo** (Android/iOS por separado) -- requiere un proyecto de Google Cloud con facturación habilitada (a diferencia de reCAPTCHA v3 clásico, que es gratis), site keys distintas a las de web, e integrar un SDK nativo por plataforma.
2. **No usar reCAPTCHA en mobile nativo** y confiar solo en las protecciones que ya existen del lado del servidor -- `PasswordResetThrottle` (5/hora por IP) y el resto de rate limits ya documentados en la guía. Esto es simplemente no cerrar esa capa extra para mobile, aceptando el rate limit por IP como única defensa.
3. **Un paso intermedio en WebView** solo para esa acción puntual (cargar una página mínima que corra el reCAPTCHA v3 clásico y devuelva el token a la app nativa) -- funciona, pero es una fricción de UX rara para algo tan puntual, y complejidad extra para mantener.

## Qué falta para decidir esto

1. Confirmar con el cliente si le importa tener esta capa extra específicamente en mobile nativo, dado que ya existe el rate limit por IP del lado del servidor.
2. Si la respuesta es sí: decidir entre reCAPTCHA Enterprise (opción 1) o el WebView puente (opción 3), coordinando con el equipo mobile el costo real de cada una.
3. Si la respuesta es no: no hace falta ningún cambio de código -- documentar la decisión acá y en la guía unificada para que quede constancia de que fue consciente, no un olvido.

## A qué afecta esto en la práctica (mientras no se decida)

- `03_olvidar_contrasena.md`: iOS/Android puede mandar `recaptcha_token: null`/omitido sin que el endpoint falle -- solo pierde la capa extra de protección.
- `01_eliminar_cuenta.md`: mismo caso para ambos endpoints de eliminar cuenta.
- Ninguno de estos tres puntos depende del backend Wind -- es una decisión de producto/mobile, sin trabajo técnico previo del lado del servidor.
