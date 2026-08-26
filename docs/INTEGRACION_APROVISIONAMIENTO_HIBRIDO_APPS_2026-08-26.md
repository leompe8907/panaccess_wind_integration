# Integración "registro con aprovisionamiento parcial" para Web, Android e iOS (Alto #3)

Fecha: 2026-08-26
Para: equipos de Web, Android e iOS
Backend: `POST /wind/create-subscriber/` (registro público de suscriptores)
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Alto #3), `docs/APROVISIONAMIENTO_HIBRIDO_SUSCRIPTOR_2026-08-26.md` (detalle técnico del lado backend)
Estado: **Backend listo, apagado en producción.** Este documento es lo que cada equipo necesita implementar en su lado ANTES de que el backend active el modo `hybrid`/`async`. No requiere ningún cambio de release inmediato -- es el trabajo a hacer para cuando se coordine la activación.

## Por qué este cambio

Hoy, `POST /wind/create-subscriber/` encadena hasta 10 llamadas síncronas a PanAccess (crear suscriptor, agregar email/teléfono, validarlos, bloquear licencia, releer smartcards, asignar producto de prueba) **dentro del mismo request HTTP**. Si PanAccess está lento, un solo registro puede tardar varios minutos en responder, y bajo carga puede agotar los procesos del servidor disponibles para todo el sitio, no solo para el registro.

El backend ya tiene lista una mitigación (modo `hybrid`): intenta todo síncrono como hoy, pero con un presupuesto de tiempo (8 segundos por defecto). Si todo va bien -- el caso normal, casi siempre -- la respuesta es **idéntica a la de hoy**, sin ningún cambio para el cliente. Si PanAccess está lento y se agota el presupuesto a mitad de camino, el backend corta ahí, termina el resto en segundo plano, y responde de inmediato con una forma de respuesta **distinta y más simple**. Ese es el único caso nuevo que cada cliente tiene que saber manejar.

**No se activa nada de esto hasta que los 3 equipos confirmen que su lado ya maneja el caso parcial.**

## El cambio de contrato, en concreto

### Caso normal (hoy, y seguirá siendo el caso normal con `hybrid` activado)

`201 Created`, con **todos** los campos que ya reciben hoy:

```json
{
  "success": true,
  "message": "Suscriptor creado exitosamente...",
  "subscriber_code": "BM$40212345678",
  "alternative_login": "usuario@example.com",
  "data": { "code": "...", "firstName": "...", "lastName": "...", "email": "...", "..." : "..." },
  "contacts_added": [{ "type": "email", "value": "..." }],
  "email_validated": true,
  "license_block_added": true,
  "token": "...",
  "credentials_url": "/wind/credentials/?t=...",
  "assigned_smartcards": ["..."],
  "product_add_result": { "success": true, "trial_granted": true, "...": "..." }
}
```

Ningún campo de este bloque cambia de nombre ni de forma. Si esto es lo único que su cliente sabe leer hoy, **sigue funcionando exactamente igual** cuando PanAccess responde rápido.

### Caso nuevo: aprovisionamiento parcial (solo ocurre si se agota el presupuesto de tiempo)

También `201 Created`, pero con un cuerpo mucho más chico:

```json
{
  "success": true,
  "message": "Suscriptor creado. El resto del aprovisionamiento (contactos, license block, producto de prueba) continúa en segundo plano.",
  "subscriber_code": "BM$40212345678",
  "alternative_login": "usuario@example.com",
  "provisioning": "async",
  "provisioning_status": "partial"
}
```

**No vienen** en este caso: `data`, `contacts_added`, `contacts_errors`, `email_validated`, `license_block_added`, `token`, `credentials_url`, `assigned_smartcards`, `product_add_result`.

La forma de distinguir un caso del otro: **la presencia de `provisioning_status: "partial"`** (o, de forma equivalente, la ausencia de `token`).

### Qué NO cambia

- El código de estado HTTP sigue siendo `201` en ambos casos -- nunca hay que tratar el caso parcial como un error.
- `subscriber_code` y `alternative_login` están siempre presentes en los dos casos -- son suficientes para identificar la cuenta recién creada y, si hace falta, intentar un login más adelante.
- Los errores de validación (`400`), duplicado (`400`/`409`) y fallas de PanAccess (`500`) no cambian -- solo cambia la forma de la respuesta de **éxito**.

## Qué falta implementar en cada lado

### Web (portal, `wind/templates/wind/register.html` + su JS)

- Hoy, tras un `201`, la web redirige a `credentials_url` (`/wind/credentials/?t=<token>`) para mostrarle al usuario su usuario/contraseña generados. Con `provisioning_status: "partial"`, **no hay `credentials_url`** -- no hay nada que mostrar todavía.
- Implementar: si la respuesta trae `provisioning_status === "partial"`, en vez de redirigir a `/wind/credentials/`, mostrar una pantalla/mensaje tipo *"Tu cuenta se está terminando de configurar. Te enviaremos tus datos de acceso por correo en los próximos minutos."* y redirigir a `login` (no a `credentials`).
- No hay que agregar polling: el correo de bienvenida con las credenciales ya se envía igual en ambos casos (síncrono o en background) -- es el mecanismo existente para que el usuario reciba sus datos sin importar el modo.

### Android / iOS

- Mismo criterio: si `provisioning_status === "partial"`, no asumir que la cuenta está 100% lista para loguear de inmediato ni intentar leer `token`/`data`/`assigned_smartcards` (van a venir `null`/ausentes, no hay que tratarlo como un payload malformado).
- Mostrar un estado de "cuenta en proceso" en vez de la pantalla de bienvenida/credenciales que se muestra hoy tras un registro exitoso normal. Sugerido: mismo mensaje que web ("revisa tu correo en unos minutos"), con un botón para ir a la pantalla de login.
- Si la app intenta loguear inmediatamente después del registro (algunas apps lo hacen para evitar que el usuario tenga que re-escribir credenciales): **no hacerlo cuando `provisioning_status === "partial"`** -- el login puede fallar simplemente porque el contacto/license block todavía no terminó de aplicarse en PanAccess. Esperar a que el usuario vuelva a intentar por su cuenta (desde el correo o manualmente).

## Limitación conocida: no hay forma de consultar el estado más tarde

Hoy **no existe un endpoint** para que un cliente pregunte "¿ya terminó de aprovisionarse el suscriptor X?" después de recibir `provisioning_status: "partial"`. El único mecanismo con el que el usuario se entera de que su cuenta ya está lista es el correo de bienvenida (que se envía en cuanto el aprovisionamiento en background termina, con las mismas credenciales que hoy se muestran en `/wind/credentials/`).

Si algún equipo necesita mostrar un estado más preciso en la propia app (en vez de derivar todo al correo), es un endpoint nuevo a construir del lado del backend -- no está en el alcance de esta activación. Avisar si hace falta antes de la fecha de activación para poder planificarlo.

## Cómo probar antes de que se active en producción

El backend puede activar `hybrid` con un presupuesto bajo (1-2 segundos) en un entorno de prueba para forzar el caso parcial en casi todos los registros, sin depender de que PanAccess esté realmente lento. Avisar cuando cada equipo tenga su lado listo para coordinar esa prueba conjunta antes de activar en producción.

## Checklist de activación

- [ ] Web: maneja `provisioning_status: "partial"` sin redirigir a `/wind/credentials/`.
- [ ] Android: maneja `provisioning_status: "partial"` sin asumir campos que no vienen, no intenta login automático en ese caso.
- [ ] iOS: mismo checklist que Android.
- [ ] Prueba conjunta con presupuesto bajo en entorno de prueba, confirmando los 3 clientes.
- [ ] Backend cambia `CREATE_SUBSCRIBER_PROVISIONING_MODE` de `sync` a `hybrid` en producción (un solo valor de `.env`, reversible sin redeploy).
