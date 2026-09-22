# Propuesta de integración RevenueCat — para feedback del equipo de Wind

Fecha: 2026-09-22
Estado: **borrador para discusión** -- ningún código de esta propuesta está implementado todavía. Este documento es para que el equipo de Wind lo revise y dé su feedback antes de tocar nada.

## 1. Resumen ejecutivo

El backend de Wind hoy **no tiene ningún concepto de billing/pagos**: PanAccess maneja un único "producto" (el trial gratuito, vía `addLicenseBlockToSubscriber`), y cualquier plan pago se gestiona por fuera de este sistema. Sumar RevenueCat implica decidir quién escribe en PanAccess cuando cambia el estado de una suscripción paga.

**Aclaración del cliente (2026-09-22) que define el alcance de este documento:** el backend de Wind **no va a modificar PanAccess directamente** como parte de esta integración -- eso lo hace el CRM del cliente, que se conecta directo a RevenueCat. Este documento propone esa arquitectura como la recomendada (Opción A), y además incluye una propuesta complementaria de qué podría hacer Wind desde su propio backend si en algún momento se aprueba que también actúe (Opción B) -- explícitamente marcada como sujeta a aprobación, no como plan por defecto.

## 2. Arquitectura recomendada -- Opción A: el CRM es el único integrador de RevenueCat

```
App (iOS/Android/Web)  →  RevenueCat  →  Webhook  →  CRM del cliente
                                                          │
                                                          ▼
                                                     PanAccess (API)
                                                          │
                                                          ▼
                                          Wind backend (sync periódico ya existente)
                                                          │
                                                          ▼
                                          ListOfSubscriber (caché local) → appVideo
```

**Por qué esto encaja bien sin escribir código nuevo en el camino crítico:** Wind ya tiene un pipeline de sincronización periódica DESDE PanAccess hacia `ListOfSubscriber` (`sync_subscribers_view`, `compare_and_update_subscribers_view`, `full_sync_view`, corriendo por Celery). Si el CRM otorga o revoca el producto pago directamente en PanAccess (vía `addLicenseBlockToSubscriber`/equivalente, el mismo mecanismo que hoy usa el trial), ese cambio de estado **llega solo** a Wind en el siguiente ciclo de sync, sin que Wind tenga que enterarse de RevenueCat en absoluto. Wind sigue siendo lo que ya es hoy: un espejo local de PanAccess para que `appVideo` no tenga que golpear PanAccess en cada pantalla.

**Punto de integración que ya existe y se podría reutilizar, si el CRM necesita algo de Wind:** ya hay un mecanismo M2M probado para integraciones externas -- `HasCrmApiKey` (`wind/permissions.py`), API key compartida vía header `X-CRM-Api-Key`, comparación en tiempo constante, fail-closed si no está configurada. Hoy lo usa el bot de CRM del cliente para `validate_subscriber_email_view` (pre-validar si un email ya existe antes de dar de alta un suscriptor). Si el CRM necesitara resolver `email → subscriber_code` (o algo similar) para saber a quién le corresponde un evento de RevenueCat, la extensión natural es un endpoint nuevo bajo ese mismo mecanismo de auth -- no hace falta inventar nada.

**Lo más importante para que esto funcione sin fricción:** el `app_user_id` que las apps le pasan a RevenueCat al inicializar el SDK debería ser directamente el `subscriber_code` de Wind (no el ID anónimo que RevenueCat genera por default). Con eso, el CRM nunca necesita preguntarle nada a Wind -- el propio evento de RevenueCat ya trae la clave correcta para actuar en PanAccess. Esto es una decisión de las apps (`appVideo`), no del backend.

**Ventajas de esta opción:**
- Un solo escritor sobre PanAccess (el CRM) -- cero riesgo de carrera o de dos sistemas pisándose el estado del mismo suscriptor.
- Cero código nuevo en el camino crítico de Wind -- el pipeline de sync ya existente hace todo el trabajo.
- Wind sigue enfocado en lo que ya hace bien: autenticación, perfil, pareo de TV, notificaciones -- no en lógica de billing.

**Lo que hay que confirmar con el equipo de Wind (ver sección 6) para que esta opción quede realmente cerrada:** si el pipeline de sync actual efectivamente refleja a tiempo los campos que le importan al negocio (¿cada cuánto corre? ¿alcanza con eso o el usuario espera ver el cambio "al instante" después de pagar?), y si algunas de las tareas que hoy son responsabilidad de Wind (correos transaccionales: bienvenida, cambio de contraseña, cierre de cuenta) deberían extenderse a eventos de suscripción (confirmación de pago, aviso de problema de cobro, etc.) o si eso también lo asume el CRM.

## 3. Propuesta complementaria -- Opción B: Wind también puede actuar (sujeta a aprobación explícita)

Esta sección es una propuesta, no un plan por defecto. Solo tendría sentido si el equipo de Wind y RevenueCat/el CRM deciden que conviene que Wind también pueda reaccionar directamente a eventos de RevenueCat -- por ejemplo, como respaldo si el CRM tiene una caída, o para algún subconjunto de eventos que el CRM no cubra.

### 3.1 Riesgo que hay que resolver ANTES de aprobar esto

Si el CRM y Wind pueden escribir en PanAccess de forma independiente para el mismo suscriptor, hay riesgo real de carrera (dos sistemas otorgando/revocando el mismo producto en paralelo, o revirtiéndose uno a otro). Si se aprueba esta opción, hace falta una partición clara de responsabilidad -- por ejemplo, por tipo de evento (Wind solo actúa en `EXPIRATION`, el CRM en todo lo demás) o por bandera de "quién es dueño de este suscriptor" -- antes de escribir código. No es algo que se pueda decidir del lado de Wind unilateralmente.

### 3.2 Diseño técnico propuesto (si se aprueba)

**Endpoint de webhook:** `POST /api/v1/revenuecat/webhook/` (`AllowAny`, protegido por verificación HMAC del header `X-RevenueCat-Webhook-Signature` que RevenueCat ya provee nativamente -- firma sobre `"{timestamp}.{body_crudo}"`, comparación en tiempo constante, tolerancia de reloj de ~5 minutos). Es un mecanismo más robusto que una API key estática, y RevenueCat lo soporta out-of-the-box.

**Responder rápido, procesar después:** RevenueCat espera un 200 en menos de 60s y reintenta hasta 5 veces (5/10/20/40/80 min) si falla. La vista solo valida la firma, guarda el evento crudo, y encola una tarea Celery -- mismo patrón que ya usa todo Wind para no bloquear la respuesta HTTP en trabajo pesado.

**Idempotencia:** los reintentos de RevenueCat reusan el mismo `id` de evento (garantiza "at-least-once", no "exactly-once"). Hace falta una tabla nueva (`RevenueCatWebhookEvent`: `event_id` único, tipo, payload crudo, `processed_at`) para no repetir una acción ya aplicada -- mismo criterio que ya usamos en telemetría con `unique_fields=["record_id"]`.

**Mapeo de eventos a acciones (si Wind los procesara):**
- `INITIAL_PURCHASE` / `RENEWAL` / `UNCANCELLATION` → asegurar el producto pago activo en PanAccess.
- `EXPIRATION` → revocar el acceso -- este es el evento real de "se acabó", no `CANCELLATION` (que solo indica que no se va a renovar, pero el acceso puede seguir vigente hasta la fecha de expiración).
- `BILLING_ISSUE` → notificar al usuario sin tocar el acceso todavía (RevenueCat ya maneja el grace period del lado de la tienda).

En vez de derivar todo del payload del webhook, RevenueCat recomienda -- ante cualquier evento -- consultar `GET /v2/customers/{app_user_id}` (REST API v2) para traer el estado real y completo (`gives_access`, `auto_renewal_status`, fecha de expiración) y aplicar ESE estado. Es más simple y más robusto que tratar de reconstruir el estado evento por evento.

**Reconciliación:** tarea periódica (Celery beat) que confirme contra la API v2 que los suscriptores con producto pago siguen vigentes -- cubre el caso de un webhook perdido tras agotar los 5 reintentos.

## 4. Limitación de plataformas (aplica a ambas opciones)

RevenueCat no tiene integración de tienda nativa para LG webOS ni Samsung Tizen -- las plataformas de TV que usa `appVideo` hoy. Sí cubre App Store, Google Play, Amazon Appstore y Stripe/Web. En la práctica, el flujo realista es "comprá desde el celular, usá en la TV": el usuario paga desde la app móvil, y la TV recibe el acceso ya resuelto en PanAccess vía el pareo por QR/UDID que ya existe (Fases 1-4, ya implementadas) -- no hace falta que la TV compre nada directamente. Amazon Fire TV sí tiene tienda soportada por RevenueCat, así que podría ser un caso aparte si el cliente quiere venta directa ahí.

## 5. Qué necesitamos que confirme/decida el equipo de Wind

- ¿La Opción A (CRM como único integrador, Wind sin cambios de código en el camino crítico) es la que quieren seguir, o quieren evaluar en paralelo la Opción B?
- ¿El pipeline de sync actual (cada cuánto corre hoy) alcanza para que el usuario vea reflejado su pago "a tiempo", o el negocio espera que sea casi instantáneo? Esto puede requerir ajustar la frecuencia del sync, independientemente de qué opción se elija.
- ¿El `app_user_id` de RevenueCat va a ser el `subscriber_code` de Wind desde el arranque? Esto es una decisión de implementación en `appVideo`, pero conviene confirmarla temprano porque cambia todo lo demás.
- ¿Los correos transaccionales de suscripción (confirmación de pago, aviso de problema de cobro, cancelación) los envía el CRM, o siguen siendo responsabilidad de Wind (que ya tiene la infraestructura de emails vía Celery)?
- ¿El catálogo de productos/planes pagos ya está definido, y cómo se van a mapear a los "productos"/license blocks que entiende PanAccess hoy (que solo conoce el trial como concepto único)?
- Si se evalúa la Opción B a futuro: ¿quién decide la partición de responsabilidad entre CRM y Wind para evitar doble escritura sobre PanAccess?

## 6. Próximos pasos

1. Compartir este documento con el equipo de Wind y esperar su feedback sobre las preguntas de la sección 5.
2. Con las respuestas, decidir si se sigue solo con la Opción A o si se detalla más la Opción B.
3. Recién ahí, si corresponde, planificar la implementación concreta (no antes).
