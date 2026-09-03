# Guía: crear las llaves de reCAPTCHA para Wind

Fecha: 2026-08-26
Para: quien vaya a activar reCAPTCHA (cliente o quien administre la cuenta de Google del proyecto)
Verificado contra la documentación oficial de Google al momento de escribir esto (developers.google.com/recaptcha y docs.cloud.google.com/recaptcha).

## Antes de empezar: qué cambió respecto a "como se hacía antes"

Hasta 2024, crear una llave de reCAPTCHA v3 era un trámite de un solo paso, sin ninguna relación con Google Cloud. Google migró todo el servicio a **Google Cloud Fraud Defense** (el nombre nuevo de lo que antes se llamaba "reCAPTCHA Enterprise"):

- Desde **Q3 2024** ya no se pueden crear llaves "Classic" sueltas.
- Desde **Q1 2026** (ya pasó), Google terminó de migrar automáticamente incluso las llaves Classic viejas que quedaban sin un proyecto de Google Cloud asociado.

**La buena noticia:** el código que ya existe en este proyecto (`wind/utils/recaptcha.py`, que llama al endpoint clásico `https://www.google.com/recaptcha/api/siteverify` con una `secret` + el token del formulario) **sigue funcionando exactamente igual, sin ningún cambio de código**. Google mantiene, para cada llave nueva que se crea, una "legacy secret key" pensada justo para aplicaciones de terceros (como la nuestra) que no usan la API nueva de Google Cloud. Solo cambia el lugar donde hay que ir a buscarla (un paso extra en la consola).

No hace falta tener experiencia previa con Google Cloud ni pagar nada -- el nivel gratuito ("Essentials") da **10,000 verificaciones por mes sin costo y sin tarjeta**, más que suficiente para los 4 flujos que necesita Wind (registro, olvidé contraseña, restablecer contraseña, eliminar cuenta).

## Paso 1 -- Entrar a la consola y crear la llave

1. Ir a **https://www.google.com/recaptcha/admin/create**.
2. Iniciar sesión con una cuenta de Google. **Recomendado:** usar una cuenta de la empresa/organización (ej. una cuenta de Google Workspace del cliente), no una cuenta personal -- así la llave queda asociada a algo que la empresa controla a largo plazo, no a la cuenta personal de quien la creó.
3. En **Label**, poner un nombre identificable, por ejemplo `Wind - produccion`.
4. En **reCAPTCHA type**, elegir **Score based (v3)** (NO "Challenge (v2)" -- el código del backend está escrito específicamente para v3, que no interrumpe al usuario con ningún checkbox ni imagen).
5. En **Domains**, agregar:
   - `backend.wind.do` (el dominio real de producción).
   - Opcionalmente `localhost` si se quiere probar en desarrollo local.
6. Marcar la casilla de aceptar los términos de servicio.
7. Click en **Submit**.

Si es la primera vez que esta cuenta de Google usa Google Cloud, en este mismo paso Google crea automáticamente -- sin pedir nada más -- un proyecto de Google Cloud detrás de escena para alojar la llave. No hace falta entrar a la consola de Google Cloud a configurar nada a mano en este punto.

## Paso 2 -- Guardar la Site key (pública)

Apenas se crea la llave, la pantalla muestra la **Site key**. Copiarla y guardarla -- esta es la que va en el HTML del frontend (el widget de cada formulario), es pública y no hay problema en que quede visible en el código fuente de la página.

## Paso 3 -- Conseguir la Secret key (privada) -- el paso que cambió

Este es el paso que antes no hacía falta y ahora sí:

1. Entrar a la **consola de Google Cloud** para el proyecto que se creó (el enlace llega en el correo de confirmación de Google, o se puede entrar directo a **https://console.cloud.google.com/security/recaptcha** y seleccionar el proyecto correspondiente en el selector de arriba).
2. Ir a la pestaña **Keys**.
3. Hacer click en la llave que se acaba de crear (la que tiene el `Label`/`Display name` puesto en el Paso 1).
4. En la página de detalle de la llave, ir a la pestaña **Integration**.
5. Click en **"Use Legacy Key"**.
6. Se abre un panel con la **legacy secret key** -- esta es la que hay que copiar.

**Importante:** esta legacy secret key es un secreto real (equivalente a una contraseña de servicio) -- nunca debe ir en el frontend ni en ningún repositorio público, solo en el `.env` del servidor.

## Paso 4 -- Configurar el servidor

Agregar en `/opt/panaccess-wind/.env`:

```bash
RECAPTCHA_SECRET_KEY=<la-legacy-secret-key-del-paso-3>
```

(`RECAPTCHA_MIN_SCORE` ya existe en `.env` con el valor por defecto `0.5` -- no hace falta tocarlo salvo que se quiera ajustar la sensibilidad más adelante, viendo datos reales en el panel de Google.)

Reiniciar los 8 procesos Daphne para que tomen la variable nueva.

**Ojo con el orden:** apenas se configura `RECAPTCHA_SECRET_KEY`, el backend empieza a **exigir** el token de reCAPTCHA en los 4 endpoints (`wind/utils/recaptcha.py::recaptcha_required()` pasa a `True`). Si en ese momento el frontend todavía no manda el widget/token (ver `docs/PLAN_ACTIVACION_RECAPTCHA_2026-08-26.md`), esos 4 flujos empezarían a **rechazar todos los intentos**. Configurar esta variable debe ser el **último** paso, después de que el widget ya esté funcionando en los 4 formularios.

## Paso 5 -- (Opcional) Guardar acceso al panel para monitoreo

Desde la consola de Google Cloud (**Fraud Defense** → pestaña **Keys** → click en la llave) se puede ver, sin costo, el tráfico real y la distribución de scores -- útil para confirmar que `RECAPTCHA_MIN_SCORE=0.5` es un umbral razonable para el tráfico real de Wind, o si conviene ajustarlo.

## Sobre el límite gratuito

10,000 verificaciones por mes, por organización (sumando todos los sitios). Si algún mes se supera ese número sin tener facturación configurada en el proyecto de Google Cloud, las verificaciones (`SiteVerify`, que es lo que usa nuestro backend) **no bloquean nada** -- Google las deja pasar automáticamente como válidas ("fail open"), el mismo comportamiento de seguridad que ya tenía reCAPTCHA Classic antes de la migración. O sea: en el peor caso de superar la cuota gratuita, el sistema no se rompe ni bloquea usuarios reales -- simplemente ese mes deja de filtrar bots hasta que se resetea el contador o se activa facturación.

## Sobre las apps móviles (iOS/Android)

Este documento cubre la llave para **web** (score-based, tipo "Web"). Las apps móviles necesitan su propio tipo de llave (**iOS**/**Android**), un proceso distinto (no es el mismo formulario de `google.com/recaptcha/admin`, sino la consola de Google Cloud Fraud Defense) y el SDK nativo de reCAPTCHA en cada app en vez del script JS -- además requiere un cambio de backend que no está hecho (`wind/utils/recaptcha.py` solo verifica contra el endpoint clásico). Esto es lo que ya estaba señalado como "Posibles mejoras #31" en la auditoría. Ver `docs/GUIA_RECAPTCHA_MOBILE_IOS_ANDROID_2026-09-03.md` para la guía completa (creación de llaves, integración del SDK y el cambio de backend pendiente) -- pendiente de coordinar con esos equipos, fuera del alcance de este documento.
