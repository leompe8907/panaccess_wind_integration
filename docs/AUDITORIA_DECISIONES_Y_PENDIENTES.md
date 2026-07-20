# Auditoría técnica — decisiones y pendientes sobre los hallazgos críticos

Fecha: 2026-07-14
Referencia: `docs/Auditoria_Tecnica_Backend_PanAccess_Wind.docx` (auditoría completa)

Este documento registra qué se hizo con cada uno de los 5 hallazgos **Críticos** de la auditoría: cuáles son una decisión de diseño aceptada, cuál se corrigió, y cuáles quedan pendientes de desarrollo con su plan de implementación.

---

## 1. Password de PanAccess en texto plano en la respuesta del login social

**Archivos:** `wind/auth_views.py:26`, `wind/services/social_login_provisioning.py:129-154`

**Estado: decisión de diseño aceptada — no se modifica.**

Motivo (según el equipo): el backend no tiene hoy un mecanismo para entregar a las apps un sessionId propio y válido con el que autenticarse contra PanAccess de forma segura. Ante esa limitante, se decidió delegar en las apps cliente la responsabilidad de recibir y manejar las credenciales reales (`login1`, `password`, `login2`, `subscriberCode`) para que puedan autenticarse directamente.

**Riesgo que queda vigente (documentado, no mitigado):** cualquier proxy, log de HTTP, herramienta de APM o error de manejo en el cliente que capture la respuesta de login social expone la contraseña real del abonado en PanAccess.

**Nota para revisión futura:** si en algún momento se define un mecanismo propio de sesión/token para las apps (ver punto 4 de la auditoría completa, sección 8.2 — "invalidación de JWT"), este es el primer punto a revisar para poder dejar de exponer la contraseña en claro.

---

## 2. Contraseña reenviada en texto plano por el correo de bienvenida

**Archivo:** `wind/services/welcome_email.py` (evidencia observada en `wind/management/commands/send_welcome_email_test.py:107`)

**Estado: decisión de diseño aceptada — solicitud explícita del cliente (Wind).**

No se modifica. Queda documentado como una decisión de negocio, no un descuido técnico.

---

## 3. Login-storm contra PanAccess por lock de sesión mal implementado — RESUELTO

**Archivos corregidos:**
- `appConfig.py` (`RedisConfig.task_lock`)
- `wind/services/panaccess_session_store.py` (`refresh_lock`)
- `wind/services/panaccess_singleton.py` (`_load_or_authenticate_session`)

### Qué estaba mal

El lock distribuido en Redis usaba `acquire(blocking=False)` (una sola comprobación, sin esperar). Cuando un proceso no conseguía el lock y todavía no había sesión publicada por el que sí lo tenía, el código **igual continuaba y se autenticaba contra PanAccess sin el lock**. Bajo carga (varios workers Celery/Django pidiendo sesión al mismo tiempo), esto podía disparar el límite de PanAccess de 20 logins en 5 minutos y tumbar toda la integración.

### Qué se cambió

- `RedisConfig.task_lock()` ahora acepta `blocking` y `blocking_timeout` (por defecto sigue siendo no bloqueante, para no afectar los locks de las tareas de sync en `wind/tasks.py`, que deben seguir saltándose si ya hay una instancia corriendo).
- `panaccess_session_store.refresh_lock()` ahora es bloqueante por defecto (espera hasta 15 segundos) en vez de fallar al instante.
- `panaccess_singleton._load_or_authenticate_session()` ahora, tanto si consigue el lock como si no, **vuelve a comprobar si ya hay una sesión publicada en Redis antes de autenticarse**. Sólo si after esperar los 15 segundos completos no hay lock ni sesión disponible, se autentica como último recurso (caso degradado, con warning en el log — ya no es la carrera rutinaria de antes).

### Impacto esperado

- Bajo carga normal, sólo un proceso hace login real; el resto reutiliza la sesión que ese proceso publica en Redis en cuanto termina (típicamente en menos de 1-2 segundos).
- No cambia el comportamiento para el caso simple (sin contención): sigue autenticando de inmediato si no hay sesión y nadie más la está pidiendo.
- El único cambio de comportamiento perceptible es que, bajo alta contención, algunos procesos esperarán hasta 15s por la sesión en vez de fallar/reintentar de inmediato — es una espera aceptable comparada con el riesgo de bloquear la cuenta de servicio completa por rate limit.

**Pendiente recomendado (no incluido en este cambio):** los hallazgos de severidad Alta en el mismo archivo (`__init__` sin lock en la primera construcción concurrente, y el hilo de validación periódica reteniendo el lock global durante la llamada de red) no se tocaron — quedan en el backlog de la auditoría completa, sección 5.2.

---

## 4. Restricción por IP de rutas administrativas se puede saltar falseando `X-Forwarded-For`

**Archivo:** `wind/middleware/sync_admin_ip_middleware.py:25-30`

**Estado: pendiente de implementar.** El equipo confirma que este control todavía no está terminado. A continuación las opciones evaluadas y su impacto, para decidir cuál implementar.

**Actualización (2026-07-20):** se agregó `/admin/` a `_PROTECTED_PREFIXES` (antes el panel de Django admin no tenía ninguna restricción de IP). Esto significa que `/admin/` ahora depende del mismo `_client_ip()` descrito acá -- así que también queda expuesto a este mismo bypass de `X-Forwarded-For` mientras no se implemente alguna de las opciones de abajo. No es un problema nuevo, es el mismo de siempre cubriendo una ruta más.

### Por qué existe el problema

Según `deploy/systemd/*.service`, Daphne sólo escucha en `127.0.0.1` — nginx es quien recibe el tráfico público y lo reenvía internamente. Eso significa que, visto desde Django, `REMOTE_ADDR` **siempre** es la IP de nginx (127.0.0.1), nunca la del cliente real. Por eso el middleware necesita leer `X-Forwarded-For` para saber la IP real — pero hoy confía en ese header sin verificar que realmente venga de nginx y sin que nginx lo sanitice, así que un cliente puede mandar su propio `X-Forwarded-For` con una IP de la lista permitida y pasar el filtro.

### Opciones de implementación

**Opción A — Confiar en X-Forwarded-For sólo si viene de un proxy conocido (recomendada)**
1. En el middleware: sólo leer `X-Forwarded-For` si `REMOTE_ADDR` está en una lista de proxies de confianza (por defecto `127.0.0.1`, `::1`; configurable vía nueva variable `SYNC_ADMIN_TRUSTED_PROXIES`). Si la petición no viene de un proxy de confianza, usar `REMOTE_ADDR` directamente e ignorar el header.
2. En nginx: cambiar `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` (que **añade** el valor que ya traiga el cliente) por `proxy_set_header X-Forwarded-For $remote_addr;` (que lo **sobrescribe** con la IP que nginx vio realmente), para que el cliente no pueda inyectar un primer salto falso.
3. Añadir a `_PROTECTED_PREFIXES` las rutas que hoy quedan fuera (`/wind/logged-in/`, `/wind/test-call-list-*/`, `/wind/*-stats/`), cerrando el hueco ya detectado en la auditoría.

*Impacto:* ninguno sobre tráfico legítimo, siempre que el cambio de nginx y el de Django se desplieguen juntos (si sólo se actualiza uno de los dos, los admins podrían quedar bloqueados temporalmente). Requiere agregar la variable `SYNC_ADMIN_TRUSTED_PROXIES` al `.env` de cada entorno y recargar nginx.

**Opción B — Apoyarse sólo en nginx, sin usar X-Forwarded-For en Django**
Los `deploy/nginx/*.conf` ya tienen `allow 127.0.0.1; deny all;` para varias de estas rutas a nivel de nginx. Se podría simplificar el middleware para que sólo sea una segunda capa de verificación basada en `REMOTE_ADDR` (que dado el binding a loopback, siempre sería nginx), asumiendo que nginx ya filtró antes de reenviar.

*Impacto:* más simple de mantener, pero deja de proteger si algún día nginx se reconfigura sin ese bloque `allow/deny`, o si se agrega una ruta nueva y se olvida replicarla en ambos lugares (justo el problema que ya existe hoy con las rutas no cubiertas).

**Opción C — Usar una librería probada (`django-ipware`)**
Delegar la resolución de "IP real detrás de proxies de confianza" a una librería mantenida y con más casos de borde cubiertos (cadenas de varios proxies, IPv6 mapeado en IPv4, múltiples headers). Mismo resultado que la Opción A pero con menos código propio que mantener.

*Impacto:* una dependencia nueva; sin impacto funcional distinto a la Opción A.

### Recomendación

Combinar A + C: usar `django-ipware` (o la lógica manual de la Opción A) para la verificación de proxy de confianza, actualizar nginx para sobrescribir `X-Forwarded-For`, y cerrar el hueco de rutas no cubiertas. Es el único enfoque que protege incluso si nginx llegara a tener un error de configuración en el futuro (defensa en profundidad real, en vez de depender de una sola capa).

---

## 5. `json.serialize_credentials` no existe — rompe `AuthenticateWithUDIDView`

**Archivo:** `wind/views.py:454`

**Estado: pendiente — funcionalidad de emparejamiento Smart TV vía HTTP directo todavía en construcción.**

El equipo confirma que esta lógica no está terminada. Se documenta como pendiente de desarrollo, no como bug a corregir de inmediato. Recordatorio para cuando se reanude ese trabajo: ya existe una implementación correcta y equivalente (`json_serialize_credentials` / `json.dumps`) en `wind/services/udid_auth_service.py`, usada por el flujo de WebSocket — al terminar el flujo HTTP directo, reutilizar esa misma función en vez de duplicar la lógica de cifrado evitaría que ambos caminos vuelvan a desincronizarse.

---

## Resumen de estado (Críticos)

| # | Hallazgo | Estado |
|---|----------|--------|
| 1 | Password en claro — login social | Decisión de diseño (Wind/apps) — documentado |
| 2 | Password en claro — correo de bienvenida | Decisión de diseño (solicitud de Wind) — documentado |
| 3 | Login-storm en panaccess_singleton.py | **Resuelto** |
| 4 | Bypass de IP allowlist vía X-Forwarded-For | **Parcial** — se cerró el hueco de rutas (ver Altos #2 abajo); la confianza ciega en `X-Forwarded-For` en sí (`_client_ip()`) sigue pendiente de definir (nginx + proxy de confianza) |
| 5 | Bug UDID (`json.serialize_credentials`) | Pendiente — feature de emparejamiento HTTP directo aún en construcción |

---

## Hallazgos Altos — resueltos

### 1. `panaccess_client.py:129-224` — reintentos síncronos bloqueaban un worker hasta ~192s

**Resuelto.** Timeout por intento y reintentos ahora configurables desde `appConfig.py` (`PanaccessConfig.HTTP_TIMEOUT_SECONDS=25`, `HTTP_MAX_RETRIES=2`, `HTTP_RETRY_MAX_DELAY_SECONDS=10`), sin tocar código para ajustarlos por entorno. Peor caso baja de ~192s a ~54s. De paso se completó la redacción del `sessionId` en logs (antes se veían los primeros 20 caracteres).

### 2. `panaccess_client.py:106` — mutaba el dict de parámetros del llamador

**Resuelto.** `call()` ahora trabaja siempre sobre una copia propia de `parameters`; nunca vuelve a escribir en el diccionario que le pasó quien lo invocó.

### 3. `panaccess_singleton.py:199-233` — validación periódica retenía el lock global durante la llamada de red (~150s cada 15 min)

**Resuelto.** La llamada a `logged_in()` y una eventual reautenticación ya no ocurren dentro de `_session_lock`. Se lee el `session_id` bajo lock (instantáneo), se valida/reautentica sin lock, y sólo se retoma el lock un instante para escribir el resultado, verificando que nadie más lo haya refrescado mientras tanto.

### 4. `settings.py:52-60` — HSTS/cookies seguras dependían de un flag manual

**Resuelto.** `PRODUCTION_HTTPS` ahora se activa automáticamente cuando `DEBUG=False`, salvo que se desactive explícitamente con `PRODUCTION_HTTPS=false` — en ese caso se registra un **warning** en el arranque para que sea una decisión consciente, no un olvido. Ver `DjangoConfig.production_https()` / `production_https_explicitly_disabled()` en `appConfig.py`.

### 5. `wind/urls.py` + middleware — rutas fuera del allowlist de IP

**Resuelto (la parte de cobertura).** Se agregaron `/wind/logged-in`, `/wind/test-call-list-`, `/wind/products-stats` y `/wind/smartcards-stats` a `_PROTECTED_PREFIXES` en `sync_admin_ip_middleware.py`. **Importante:** esto sólo cierra el hueco de qué rutas están cubiertas por el allowlist; no corrige el problema de fondo del Crítico #4 (que el middleware confíe en `X-Forwarded-For` sin validar el proxy) — eso sigue pendiente de tu decisión sobre la Opción A/B/C descrita arriba.

### 6. `settings.py:113-117` — middleware de IP allowlist opt-in sin aviso

**Resuelto.** `wind/apps.py` ahora loguea un **warning** en el arranque (solo en procesos de servidor reales — se salta en `migrate`/`test`/`shell`/etc., igual que la inicialización de PanAccess) si `SYNC_ADMIN_IP_ALLOWLIST` no está configurado.

### 7. `panaccess_singleton.py:51-60` — `__init__` sin lock

**Resuelto.** El cuerpo de `__init__` ahora está protegido por el mismo lock de clase que usa `__new__`, eliminando la condición de carrera en la primera construcción concurrente del singleton.

### 8. `exceptions.py:20-22` — `PanAccessRateLimitError` nunca se usaba

**Resuelto.** `wind/utils/panaccess_auth.py` ahora detecta (por palabras clave en el mensaje de error — a confirmar/ajustar contra la documentación oficial de PanAccess cuando la tengamos) el rechazo por límite de intentos y lanza `PanAccessRateLimitError` en vez del error genérico de autenticación. `panaccess_singleton.py` la maneja de forma distinta: no la reintenta con el backoff corto (para no empeorar el bloqueo), la deja constancia en el log y la propaga de inmediato.

### 9. `wind/functions/logged_in.py:46-49` — endpoint de diagnóstico creaba sesiones huérfanas

**Resuelto.** `logged_in_view` ahora reutiliza el singleton compartido (`get_panaccess()` + `ensure_session()`), igual que sus endpoints hermanos `/wind/singleton/` y `/wind/ops/panaccess-session/`, en vez de crear un `PanAccessClient` nuevo y autenticarse desde cero sin cerrar la sesión.

### 10. `panaccess_deprovision.py:78,82-84` — reutiliza fila de suscriptor como si fuera de cada smartcard

**Validado contra el WSDL oficial de operador** (`https://cv01.panaccess.com/?requestMode=wsdl&v=4.3&r=operator`, v4.3).

**Sobre el orden productos → smartcards:** el WSDL documenta las excepciones (`@throws`) de `cvRemoveSmartcardFromSubscriber` como `not_a_smartcard`, `access_denied`, `no_access_to_function`, `function_not_available`, `unknown_error_serverside`. No hay ninguna excepción del tipo "la tarjeta tiene órdenes/productos activos". Lo que sí está documentado es que `cvDeleteSubscriber` (borrar el suscriptor completo) lanza `subscriber_has_smartcards` — es decir, la restricción documentada es "no se puede borrar el suscriptor mientras tenga smartcards", no "no se puede quitar la smartcard mientras tenga productos". El código actual nunca llama a `cvDeleteSubscriber` (solo desvincula productos y smartcards), así que esa restricción documentada ni siquiera aplica al flujo actual. Conclusión: el comportamiento descrito (PanAccess bloquea la remoción de la smartcard si aún tiene productos) puede ser real en el servidor de producción, pero no está en el contrato documentado — probablemente sea una regla de negocio no documentada del lado del proveedor. Mantener el orden productos → smartcards como salvaguarda es razonable y se conserva.

**Dos problemas nuevos encontrados (independientes del orden), estos sí ameritan corrección:**

1. **Nombre de operación incorrecto para productos.** `PanaccessConfig.REMOVE_PRODUCT_API` usa por defecto `"removeProductFromSmartcard"` (singular). Ese nombre no existe en el WSDL; la operación documentada es `cvRemoveProductFromSmartcards` (plural).
2. **Parámetros no coinciden con la firma documentada:**
   - `cvRemoveProductFromSmartcards` espera `smartcards` como arreglo (notación `smartcards[0]`, `smartcards[1]`... — confirmado porque el propio proyecto ya usa esa notación para `addProductToSmartcards` en `create_subscriber.py:777-780`, que sí funciona en producción) más un único `productId`. El código de deprovisioning en cambio manda `{"code", "smartcard": sn, "productId"}` uno a la vez — la clave `smartcard` no es válida y `code` no forma parte de la firma.
   - `cvRemoveSmartcardFromSubscriber` espera `sessionId` + `smartcardId`. El código manda `{"code", "smartcard": sn}` — de nuevo la clave no coincide (`smartcard` en vez de `smartcardId`).

Dado que los nombres de parámetro no coinciden con lo documentado, existe el riesgo de que estas llamadas no estén haciendo lo que se espera (falla silenciosa o ignoradas por el servidor).

**Además, existe una operación mejor para este caso:** `cvCleanSmartcards` recibe un arreglo de smartcards y en una sola llamada limpia todas las órdenes/productos de esas tarjetas, sin iterar producto por producto. Usarla también elimina de raíz el bug ya señalado de `_extract_product_ids` (usa datos a nivel suscriptor en vez de por-smartcard), porque ya no haría falta conocer qué productos tiene cada tarjeta.

**IMPLEMENTADO — cambio de alcance aprobado por el cliente.** En reunión con Wind se decidió que el cierre de cuenta pase de ser un "desasociar productos y smartcards" a un cierre real: también se borra el suscriptor en PanAccess (`cvDeleteSubscriber`). Para evitar que esto reabra la puerta al abuso de período de prueba (crear cuenta → esperar el trial → borrar → registrar de nuevo), el control de "ya usó su trial" se apoya en tablas propias de Wind (`SubscriberDocumentRegistry` / `SubscriberEmailRegistry`, ya existentes en `wind/models.py`, migración `0002_subscriberclosurelog_...`), que **nunca se borran** aunque el suscriptor sí desaparezca de PanAccess.

El equipo de servicios de PanAccess confirmó la secuencia de operaciones a usar: `cvRemoveLicenseBlockFromSubscriber`, `cvRemoveProductFromSmartcards`, `cvRemoveSmartcardFromOrder`, `cvCleanSmartcards` (con dudas de si hacen falta las dos últimas juntas — se implementan ambas y se deja evidencia en el log de cada llamada para confirmarlo empíricamente) y `cvDeleteSubscriber`.

`wind/services/panaccess_deprovision.py` fue reescrito con la secuencia completa, contra los parámetros y `@throws` reales del WSDL v4.3:

1. `cvGetOrdersOfSubscriber` — trae las órdenes reales (orderId, productId, sn) del suscriptor. Reemplaza la fuente de productIds que antes venía de la fila de `getSubscriber` (el bug de usar datos a nivel-suscriptor en vez de por-smartcard queda resuelto: ahora se usa el productId real de cada orden).
2. `cvRemoveLicenseBlockFromSubscriber(code)` — best-effort, no bloquea el resto si falla (la documentación no aclara cuándo "ya no quedan" bloques).
3. `cvRemoveProductFromSmartcards(smartcards[], productId)` — batch con notación `smartcards[0]`, `smartcards[1]`... (la misma que ya usa `create_subscriber.py` para el alta, confirmada como la forma correcta de mandar arreglos a este puente HTTP).
4. `cvRemoveSmartcardFromOrder(subscriberCode, orderId, smartcard)` — por orden activa, como red de seguridad (no-op documentado si la tarjeta ya no pertenece a esa orden).
5. `cvCleanSmartcards(smartcards[])` — barrido final de todas las órdenes restantes.
6. `cvRemoveSmartcardFromSubscriber(smartcardId)` — corregido: la clave real es `smartcardId`, no `smartcard`; ya no se manda `code` (no es parte de la firma documentada).
7. `cvDeleteSubscriber(code)` — borra el suscriptor. Lanza `subscriber_has_smartcards` si algún paso anterior no desvinculó todas las tarjetas, lo cual queda explícito en `errors`.

Los pasos 2-4 se tratan como no críticos (`warnings`); solo el paso 6 (por tarjeta) y el 7 (borrado final) determinan `success`. Cada llamada queda registrada en `steps` con su respuesta cruda, para poder revisar en la prueba real cuál de `cvRemoveSmartcardFromOrder` / `cvCleanSmartcards` hace falta.

`wind/services/subscriber_closure.py` (el orquestador que marca `SubscriberDocumentRegistry`/`SubscriberEmailRegistry` como cerrados y `eligible_for_trial=False`, y que registra `SubscriberClosureLog` como respaldo si el cierre queda parcial) **no requirió cambios** — ya trata el resultado de `deprovision_subscriber_in_panaccess` de forma genérica y ya nunca borra las tablas de control, solo las marca.

Se agregaron las constantes `GET_ORDERS_API`, `REMOVE_LICENSE_BLOCK_API`, `REMOVE_SMARTCARD_FROM_ORDER_API`, `CLEAN_SMARTCARDS_API` y `DELETE_SUBSCRIBER_API` en `appConfig.py`, y se corrigieron los nombres por defecto de `REMOVE_PRODUCT_API`/`REMOVE_SMARTCARD_API` a los del WSDL (`cv...`).

**Corregido tras dos pruebas reales (`--dry-run`) contra el suscriptor 1120743001:** la cuenta de servicio no tiene permiso para `cvGetOrdersOfSubscriber` ("Ud. no tiene los permisos para ejecutar esta funcionalidad"), igual que el resto del proyecto casi nunca usa el prefijo "cv". Se cambiaron los valores por defecto de las 7 constantes nuevas en `appConfig.py` para que sean SIN prefijo "cv" (`getOrdersOfSubscriber`, `removeLicenseBlockFromSubscriber`, `removeProductFromSmartcards`, `removeSmartcardFromOrder`, `cleanSmartcards`, `removeSmartcardFromSubscriber`, `deleteSubscriber`, `disableOrderOfSubscriber`), y además `panaccess_deprovision.py` ahora reintenta automáticamente con la variante alterna (con/sin "cv") en cada paso si el nombre configurado falla — mismo patrón que ya usa `create_subscriber.py` para `validateContactOfSubscriber`/`cvValidateContactOfSubscriber`. Así funciona sin importar cuál de las dos formas tenga habilitada la cuenta de servicio en cada entorno.

También se validó con la prueba real que este suscriptor tiene una orden activa sin smartcard asociada (`sn: null`) — caso que se cubrió agregando el paso `disableOrderOfSubscriber(orderId, subscriberCode)`.

Se agregó `wind/tests/test_panaccess_deprovision.py` con PanAccess mockeado (sin red): cubre el caso feliz, el caso de orden sin smartcard, el fallback de nombre de API, y la falla parcial (`subscriber_has_smartcards`). Los 4 casos pasan (`python manage.py test wind.tests.test_panaccess_deprovision`).

**PROBADO de punta a punta por el equipo** (`python manage.py close_subscriber --code 1120743001 --reason "prueba"`, sin `--dry-run`, contra PanAccess real): cierre exitoso — `removeLicenseBlockFromSubscriber`, `disableOrderOfSubscriber` (la orden 90 sin smartcard) y `deleteSubscriber` funcionaron sin errores ni warnings.

**Del grupo de operaciones para limpiar productos/órdenes de las smartcards, el equipo confirmó que solo hace falta `cleanSmartcards`.** Se sacaron del flujo `removeProductFromSmartcards` y `removeSmartcardFromOrder` (y sus constantes en `appConfig.py`) para no hacer llamadas de más — `cleanSmartcards` alcanza por sí sola.

**Hallazgo a tener en cuenta para la próxima prueba:** en la corrida real, `getSubscriber` devolvió `smartcards: []` para el subscriber 1120743001, pero `removeLicenseBlockFromSubscriber` reveló 4 smartcards reales asociadas (`4001823831/...830/...829/...828`) que nunca aparecieron en `getSubscriber`. Como el flujo arma la lista de tarjetas a partir de `getSubscriber`, el paso `cleanSmartcards`/`removeSmartcardFromSubscriber` no llegó a ejecutarse para esas 4 tarjetas — el cierre funcionó igual porque `removeLicenseBlockFromSubscriber` parece liberarlas como efecto colateral. Falta probar el flujo contra un suscriptor cuyo `getSubscriber` sí devuelva `smartcards` con contenido, para confirmar que `cleanSmartcards`/`removeSmartcardFromSubscriber` funcionan cuando de verdad hace falta usarlos (ese camino no se ha ejercitado todavía en un caso real).

Test mockeado actualizado (`wind/tests/test_panaccess_deprovision.py`) para reflejar el flujo recortado (sin los pasos que se sacaron). Los 4 casos siguen pasando.

Todos los cambios de esta sección fueron verificados con `python -m compileall` sobre el proyecto completo (sin errores de sintaxis). No se ejecutó el suite de tests (`python manage.py test wind.tests`) porque Django no está instalado en el entorno donde se hizo esta auditoría — se recomienda correrlo antes de desplegar, y sobre todo antes de correr `close_subscriber` sin `--dry-run` en producción.

### 11. `subscriber_closure.py` — tombstone local no se creaba si el suscriptor nunca se había sincronizado

**Resuelto.** El branch de "no hay fila local" usaba `ListOfSubscriber.objects.filter(code=...).update(...)`, que sobre cero filas no crea nada ni avisa (no-op silencioso de Django). Eso dejaba el cierre sin tombstone local, y el siguiente `periodic_sync_pipeline_task`/`full_sync_task` volvía a insertar al suscriptor como "active" con los datos que PanAccess todavía tuviera, deshaciendo el cierre en la caché local. Cambiado a `ListOfSubscriber.objects.update_or_create(code=..., defaults={...status=CLOSED...})`, que siempre deja una fila cerrada exista o no de antes.

De paso se corrigió un `NameError` real en la misma función (`_deactivate_portal_user` en vez de `_deactivate_portal_users(subscriber_code)`) que habría roto todo cierre real que llegara a esa línea.

**Pendiente relacionado, no resuelto con este cambio:** los cierres que quedan en estado `partial` (`SubscriberClosureLog.status=PARTIAL`) no tienen reintento automático ni alerta — solo quedan en la tabla a la espera de que alguien la revise manualmente. Se recomienda un task periódico que los detecte y reintente, o al menos una alerta.

**Condición de carrera cierre vs. sync periódico:** cubierta en el caso común — cuando ya existe fila local, `close_subscriber_account` la marca `PENDING_CLOSURE` y la guarda *antes* de llamar a PanAccess, así que `_is_closure_tombstone` la protege durante toda la duración del cierre. Cuando no existe fila local todavía, no hay tombstone hasta el `update_or_create` final; si un sync corre justo en esa ventana, el `update_or_create` al terminar sobreescribe cualquier dato que el sync haya insertado, así que no queda inconsistencia permanente — como mucho un parpadeo de pocos segundos en el caso raro de cerrar un suscriptor nunca sincronizado antes. Si se quiere cerrar también ese hueco, se puede crear un tombstone `PENDING_CLOSURE` de entrada incluso sin fila previa, igual que ya se hace cuando la fila existe.

### 12. `panaccess_session_store.py:46` — sessionId de PanAccess en Redis sin cifrar

**Resuelto.** `set_session_id`/`get_session_id` ahora usan `encrypt_value`/`decrypt_value` (`wind/utils/encryption.py`, Fernet, ya usado para otros secretos del proyecto) antes de escribir/leer en Redis. Un valor viejo sin cifrar (o corrupto) simplemente falla el `decrypt_value` y se trata como cache-miss, forzando un login nuevo, en vez de propagar el sessionId en texto plano.

### 13. Temas nuevos identificados, aún sin resolver

- **`password_reset.py:64-84`** — `is_reset_token_used`/`mark_reset_token_used` dependen solo de Redis y, si Redis falla, hacen fail-open (tratan el token como no usado). Un enlace de reset filtrado se puede reutilizar durante toda la ventana de una caída de Redis. Propuesta: mover el flag de "token usado" a base de datos (no solo Redis), para que una caída de Redis no abra la puerta a la reutilización.
- **Password reset/cambio no invalida JWT existentes** (`password_reset.py`, `profile/views.py`, `change_password.py`) — `SIMPLE_JWT` solo blacklistea un refresh token *después* de rotarlo, no invalida proactivamente los tokens ya emitidos cuando el usuario cambia/resetea su contraseña. Un access/refresh token robado antes del cambio sigue funcionando hasta que expira por su cuenta.
- **`subscriber_auth.py:89-155`** (`_discover_login_by_login1`) — un login con `login1` numérico no encontrado localmente dispara hasta `PANACCESS_LOGIN_DISCOVERY_MAX_CALLS` (40 por defecto) llamadas síncronas a PanAccess paginando el catálogo completo, dentro del mismo request. Está acotado (no es infinito) pero es una amplificación real: pocas requests de un atacante pueden generar cientos de llamadas a PanAccess y empujarlo a su propio rate-limit, degradando el login de usuarios legítimos. Solo protegido por el throttle anónimo genérico (`AnonBurstThrottle`), no por uno específico de login.
- **`create_subscriber.py`** — el registro público encadena 6-9 llamadas síncronas a PanAccess dentro del mismo request. Protegido por `RegisterThrottle` contra volumen de abuso, pero no resuelve que cada registro retiene un worker todo el tiempo que tarden esas llamadas — si PanAccess está lento, los workers se agotan más rápido que con un endpoint típico.
- **`adapters.py:56-65`** — `pre_social_login` confía en el email que devuelve el proveedor social y lo trata como verificado sin revisar el flag de verificación del proveedor (p. ej. `email_verified` de Google), antes de fusionarlo con una cuenta local existente por coincidencia de email.
- **UDID (`SubscriberInfo` / `udid_auth_service.py:88-97`)** — la lógica de emparejamiento UDID↔suscriptor todavía no está implementada (depende de un proceso externo que no existe hoy). Queda anotado como tema a revisar más adelante, no se toca por ahora.

### 14. Implementado — resto de los temas de la sección 13

- **Condición de carrera cierre vs. sync (hueco cerrado):** `close_subscriber_account` ahora marca `PENDING_CLOSURE` (o crea la fila con `update_or_create` si nunca existió) *antes* de llamar a `deprovision_subscriber_in_panaccess`, no después. Así el tombstone protege la fila durante toda la desaprovisión, incluso para un suscriptor nunca sincronizado localmente.
- **Retry + alerta para cierres "partial":** nuevo campo `ListOfSubscriber.closure_retry_count` + task `wind.tasks.retry_partial_closures_task` (cada `CELERY_CLOSURE_RETRY_MINUTES`, default 30 min): busca suscriptores en `PENDING_CLOSURE` con `closure_retry_count < CELERY_CLOSURE_RETRY_MAX_ATTEMPTS` (default 5) y reintenta `close_subscriber_account`. Al agotar los intentos, manda un correo a `EmailConfig.OPS_ALERT_ADDRESS` (default: mismo correo de soporte) y deja de reintentar automáticamente.
- **`panaccess_session_store.py` — sessionId cifrado:** `set_session_id`/`get_session_id` usan `encrypt_value`/`decrypt_value` (Fernet). Un valor viejo sin cifrar se trata como cache-miss.
- **`adapters.py` — email social sin verificar:** `pre_social_login` ahora rechaza el login si el proveedor no marca el email como verificado (`sociallogin.email_addresses[].verified`, con respaldo en `extra_data.email_verified`), antes de fusionar con una cuenta local existente.
- **`password_reset.py` — token de reset movido a BD:** nuevo modelo `PasswordResetTokenUse` (hash del token + `used_at`). `is_reset_token_used`/`mark_reset_token_used` ahora consultan/escriben la BD primero (fuente de verdad); Redis queda como caché best-effort. Una caída de Redis ya no permite reutilizar un token filtrado.
- **JWT no invalidado tras cambio de contraseña:** nuevo modelo `UserSecurityProfile` (`password_changed_at` por usuario) + `wind/services/jwt_invalidation.py`. `mark_password_changed(user)` (llamado desde `sync_password_locally`, compartida por "olvidé mi contraseña" y "cambiar contraseña") blacklistea los refresh tokens vigentes y actualiza `password_changed_at`. `PasswordAwareJWTAuthentication` (ahora el `DEFAULT_AUTHENTICATION_CLASSES`) rechaza cualquier access token cuyo `iat` sea anterior a ese timestamp.
- **`create_subscriber.py` — 6-9 llamadas encadenadas:** nuevo flag opt-in `FeatureConfig.CREATE_SUBSCRIBER_ASYNC_ENRICHMENT` (default `False`, sin cambios de comportamiento hasta activarlo). Con el flag en `true`, `create_subscriber_view` solo hace `addSubscriber` sync y responde de una vez; el resto (registros de unicidad, contactos, license block, producto/trial, búsqueda de smartcards) corre en `wind.tasks.finish_subscriber_provisioning_task`. **Importante:** en modo async la respuesta ya NO incluye `token`/`credentials_url`/`license_block_added`/`contacts_added`/`assigned_smartcards` de forma síncrona (dependen de esas mismas llamadas) — coordinar con el equipo de frontend antes de activar el flag en producción.
- **`subscriber_auth.py` (paginación de catálogo en login):** el equipo confirmó que `login1` siempre existe porque PanAccess lo crea automáticamente al crear el suscriptor, así que `_discover_login_by_login1` no debería activarse en la práctica. No se tocó código; queda como salvaguarda para casos borde, sin acción adicional.

Migración: `wind/migrations/0003_closure_retry_password_reset_security_profile.py` (campo `closure_retry_count` en `ListOfSubscriber`, modelos `PasswordResetTokenUse` y `UserSecurityProfile`). Escrita a mano siguiendo el estilo de `0002_*` porque el entorno de esta auditoría no tiene Django instalado con acceso a Postgres para correr `makemigrations` — **correr `python manage.py migrate` y revisar el diff de la migración antes de desplegar**.

### 15. `compare_and_update_subscribers_task` pasa a ser periódica (cada 5 min) + lotes de 1000 + cola/worker dedicados

**Decisión del cliente:** a pesar de que esta tarea escala con el tamaño TOTAL del catálogo (no con lo que cambió — pagina todo PanAccess y carga toda la tabla local para comparar), se pidió correrla cada 5 minutos en vez de solo nocturna. Implementado con las siguientes salvaguardas para que no se acumulen corridas:

- Cola propia `compare_reconcile` (`CeleryConfig.COMPARE_SUBSCRIBERS_QUEUE`), separada de `sync_pipeline` y `full_sync`, para poder levantarle un worker dedicado sin que compita con la sync incremental: `celery -A panaccess_wind_integration worker -Q compare_reconcile -c 1`.
- `expires` en el schedule (`CELERY_COMPARE_SUBSCRIBERS_MINUTES * 60`): si una corrida se atrasa más que el intervalo, la siguiente invocación se descarta en vez de encolarse en cadena.
- El lock de Redis de la tarea (que antes tenía un TTL fijo de 600s, más corto que su `time_limit`) ahora usa `CeleryConfig.COMPARE_SUBSCRIBERS_LOCK_TIMEOUT` (default 1800s) para evitar que el lock expire mientras la tarea sigue corriendo y se disparen dos reconciliaciones en paralelo.
- Sigue respetando `is_full_sync_in_progress()` (se omite si el `full_sync_task` nocturno está en curso).

**Riesgo que queda documentado (no resuelto, aceptado por el cliente):** si el catálogo crece lo suficiente como para que una corrida tarde más de 5 minutos, `expires` empieza a descartar corridas en vez de acumularlas — en la práctica dejaría de reconciliar cada 5 min exactos y pasaría a reconciliar "cada vez que una corrida logra completar". Si esto se vuelve frecuente en los logs, la alternativa es reescribir la tarea para reconciliar en bloques con cursor (procesar una porción del catálogo por corrida en vez de todo), pendiente si se necesita.

**Lotes subidos a 1000** (antes 100-200) en descarga y escritura a BD, para subscribers, smartcards, products y login info: `CELERY_SYNC_LIMIT`, `PANACCESS_LOGIN_INFO_PAGE_LIMIT`, `PANACCESS_SMARTCARD_PAGE_LIMIT`, `PANACCESS_LOGIN_INFO_DB_CHUNK` y el nuevo `PANACCESS_DB_WRITE_CHUNK_SIZE` (usado por defecto en `store_all_subscribers_in_chunks`/`store_all_smartcards_in_chunks`/`store_all_products_in_chunks`). Todos configurables por variable de entorno.

### 16. Nuevos hallazgos consultados (2026-07-16) — sin resolver salvo lo indicado

- **`subscriber_catalog.py` / `profile/serializers.py`** — el `GET` de perfil llama a PanAccess de forma síncrona dentro del request. Mismo riesgo de disponibilidad que login/registro: retiene un worker por cada perfil consultado mientras PanAccess responde.
- **Doble alta de suscriptor con mismo email/documento (`create_subscriber.py:317-366,547-576`, `social_login_provisioning.py:94-126`) — confirmado SIN resolver.** Patrón "leer si existe → crear" sin lock (`select_for_update` ni lock distribuido). El `unique=True` de `SubscriberEmailRegistry.email` protege solo la tabla de Wind; dos requests concurrentes pueden crear dos suscriptores distintos en PanAccess para el mismo email antes de que cualquiera escriba el registro local.
- **Doble concesión de trial (`subscriber_trial.py` + `create_subscriber.py:787`) — confirmado SIN resolver.** Mismo patrón check-then-act sin lock entre `is_eligible_for_trial()` y `mark_trial_granted()`.
- **`getSubscriber.py:184-271` — borrado de credenciales por paginación incompleta — confirmado SIN resolver.** En `compare_and_update_all_subscribers`, si una página intermedia llega vacía por un glitch transitorio, el loop lo interpreta como "fin del catálogo" y corta ahí. Los suscriptores de páginas no alcanzadas nunca entran a `remote_codes`, y `_delete_local_subscribers_not_in_remote` los borra (a ellos y sus credenciales) pensando que ya no existen en PanAccess.
- **`getSmartcard.py:571-577` — limpieza de huérfanos por tope de páginas.** `_fetch_smartcards_for_subscriber` corta a los `PANACCESS_SMARTCARD_SUBSCRIBER_MAX_PAGES` (default 5). Si un abonado tiene más smartcards de las que entran en ese tope, `_reconcile_subscriber_smartcards` borra las que quedaron fuera del muestreo tratándolas como huérfanas, aunque sigan siendo válidas en PanAccess.
- **`panaccess_circuit_breaker.py:47-58,66-83`** — solo `PanAccessConnectionError`/`PanAccessTimeoutError` cuentan como fallo (un error de negocio con `success:false` no abre el circuito), y el estado (`_failures`/`_opened_at`) vive en memoria del proceso — cada worker tiene su propio circuito, no comparten uno solo. Propuesta: contar también `PanAccessException` como fallo, y mover el estado a Redis (mismo patrón que `panaccess_session_store.py`).
- **`tasks.py` (`full_sync_task`):379-411** — el flag `full_sync_in_progress` en Redis tiene el mismo TTL que el `time_limit` duro de Celery; si la tarea tarda casi ese tiempo, el flag puede expirar segundos antes de que Celery termine de forzar su fin, dejando una ventana donde `periodic_sync_pipeline_task` podría arrancar en paralelo. Propuesta: TTL del flag = `FULL_SYNC_TIME_LIMIT` + margen (ej. 300s).
- **UDID/websocket (`ValidateAndAssociateUDIDView`, `DisassociateUDIDView`, `websocket_utils.py` fingerprint/rate-limit por `client_token`)** — lógica todavía no completada; quedan anotados como temas a revisar más adelante junto con el resto de UDID (sección 13), sin acción por ahora.

### 17. Login no revisaba `status=closed` localmente — RESUELTO (confirmado en la práctica por el cliente)

Al revisar el flujo de login a raíz de una pregunta sobre cuentas cerradas: `authenticate_portal_user`/`verify_panaccess_credentials` nunca consultaban `ListOfSubscriber.status`, y `get_or_create_portal_user` (`subscriber_auth.py`) ponía `user.is_active = True` sin condición en cada login exitoso por credenciales de PanAccess. Si PanAccess todavía acepta esas credenciales (recordar la duda abierta sobre si `deleteSubscriber` borra de verdad — sección 10), un suscriptor "cerrado" localmente podía seguir iniciando sesión con normalidad, y si su `User` de Django había quedado desactivado por el cierre, este mismo login lo reactivaba sin darse cuenta.

**Confirmado en producción:** el cliente cerró una cuenta y pudo seguir logueándose en el perfil. Investigado el porqué exacto: `close_subscriber_account` sí borra `SubscriberLoginInfo` (la credencial cacheada localmente) y sí desactiva el `User` -- pero si el suscriptor vuelve a intentar loguearse, `find_login_record` no encuentra nada localmente (correcto), cae a `fetch_and_find_login_record`, que **vuelve a pedirle la credencial a PanAccess en vivo** (`fetch_login_info_for_subscriber`). Si PanAccess todavía la entrega (la cuenta no quedó realmente eliminada del lado de PanAccess, o hay un lag), esa credencial se re-cachea localmente, el login se valida, y `get_or_create_portal_user` reactiva el `User` sin preguntar -- deshaciendo la desactivación que `close_subscriber_account` acababa de hacer. Es decir: aunque la limpieza local estaba bien hecha, **nada en toda la cadena volvía a chequear el estado local antes de conceder o reactivar acceso**, así que un simple reintento de PanAccess deshacía el cierre.

**Implementado:** nueva función `is_subscriber_closed_locally(subscriber_code)` en `subscriber_auth.py` (`True` si `ListOfSubscriber.status` es `CLOSED` o `PENDING_CLOSURE`). Se usa en dos puntos:
- `authenticate_portal_user`: rechaza el login (devuelve `None`) en los tres caminos -- Django nativo, Django por email, y credenciales PanAccess -- si el abonado vinculado está cerrado localmente, sin importar si la credencial venía de caché o de una re-consulta en vivo a PanAccess.
- `get_or_create_portal_user`: ya no fuerza `is_active = True` sin condición; solo reactiva si el abonado NO está cerrado localmente -- segunda capa de defensa por si algún otro caller llega a usarla sin pasar por el chequeo anterior.

Los productos de prueba no se ven afectados por este hallazgo (solo se otorgan en el registro, nunca en el login).

### 22. Confirmado en producción tras el fix anterior — una sesión ya logueada seguía entrando al dashboard después de cerrar la cuenta

El cliente cerró una cuenta y, aunque el fix de la sección 17 ya bloquea logins NUEVOS, seguía pudiendo entrar al dashboard con la sesión que ya tenía abierta -- el perfil devolvía 404 ("No hay suscriptor vinculado a este usuario") pero el resto de la app seguía cargando, señal de que la autenticación en sí seguía pasando.

**Causa raíz:** `_deactivate_portal_users` (que pone `is_active=False` en el `User` de Django) solo se ejecutaba en `close_subscriber_account` **después** de que la desaprovisión en PanAccess terminara con éxito completo. Si PanAccess fallaba o el cierre quedaba `PARTIAL` (ver sección 11), la función retornaba antes de llegar a esa línea -- el `User` nunca se desactivaba, así que cualquier sesión JWT ya abierta (access token emitido antes del cierre) seguía autenticando con total normalidad hasta que expirara por su cuenta. Además, aunque el `User` sí llegara a desactivarse, nada invalidaba de forma proactiva los tokens ya emitidos -- solo `is_active=False`, que corta accesos *nuevos* pero no imita el mecanismo que ya existía para cambio de contraseña (`jwt_invalidation.py`).

**Resuelto:**
- `_deactivate_portal_users` ahora corre en el mismo punto donde se marca el tombstone `PENDING_CLOSURE`, **antes** de llamar a PanAccess -- no solo tras un éxito completo. Así, apenas se pide el cierre (sin importar si PanAccess termina bien, mal, o parcial), el acceso al portal queda cortado.
- Se generalizó el mecanismo de invalidación de JWT que ya existía para cambio de contraseña (`wind/services/jwt_invalidation.py`, antes `mark_password_changed`): nueva función `invalidate_active_sessions(user)` -- blacklistea los refresh tokens vigentes y adelanta el corte de `iat` que `PasswordAwareJWTAuthentication` usa para rechazar access tokens ya emitidos. `mark_password_changed` ahora es un alias de esta función; `_deactivate_portal_users` la llama por cada usuario que desactiva, así un access token vigente emitido antes del cierre deja de servir de inmediato, no solo al expirar.

Tests nuevos en `wind/tests/test_subscriber_closure.py` confirman que el usuario queda desactivado tanto si la desaprovisión en PanAccess falla como si termina en éxito completo.

### 18. Implementado — resto de los temas de la sección 16

- **`getSubscriber.py` — borrado de credenciales por paginación incompleta (resuelto).** `compare_and_update_all_subscribers` ahora distingue "página vacía = fin real del catálogo" de "página vacía por glitch transitorio": si una página intermedia llega vacía, reintenta hasta `max_empty_page_retries` (2) antes de darla por buena. Se agregó una bandera `pagination_complete` que solo queda en `True` si el recorrido llegó al final sin cortes sospechosos; `_delete_local_subscribers_not_in_remote` y `cleanup_login_info_not_in_remote` (los pasos destructivos) ahora se saltan por completo si `pagination_complete=False`, en vez de borrar con datos incompletos.
- **`getSmartcard.py` — limpieza de huérfanos por tope de páginas (resuelto con el mismo patrón).** `_fetch_smartcards_for_subscriber` ahora devuelve `(entries, truncated)` en vez de solo la lista; si se llegó al tope de `PANACCESS_SMARTCARD_SUBSCRIBER_MAX_PAGES` sin agotar el catálogo del abonado, `truncated=True`. `_reconcile_subscriber_smartcards(..., truncated=True)` se salta el borrado de huérfanas para ese abonado en esa corrida (se reintenta en la próxima reconciliación), en vez de borrar smartcards válidas que quedaron fuera del muestreo. Tests actualizados/agregados en `wind/tests/test_get_smartcard.py`.
- **`panaccess_circuit_breaker.py` — estado por proceso + errores de negocio contando como fallo (resuelto).** Reescrito para guardar el estado (`_failures`/circuito abierto) en Redis (`panaccess:cb:open`, `panaccess:cb:failures`) en vez de memoria del proceso, así todos los workers comparten el mismo circuito. Sobre qué cuenta como fallo: se sumaron `PanAccessSessionError`/`PanAccessRateLimitError`/`PanAccessAuthenticationError` a los ya contados `PanAccessConnectionError`/`PanAccessTimeoutError` — **deliberadamente no** se suma `PanAccessException`/`PanAccessAPIError` genérico, porque esa misma clase la usa `panaccess_client.py` para errores de negocio comunes con HTTP 200 (ej. "el email ya existe" al registrar); contarla abriría el circuito para todos los usuarios solo porque varios registros seguidos fallaron por validación de otro usuario.
- **`tasks.py` (`full_sync_task`) — ventana entre el flag `full_sync_in_progress` y el fin real de la tarea (resuelto, con alcance ampliado por pedido del cliente).** El cliente pidió que `full_sync_task` corra hasta terminar sin importar cuánto tarde, así que en vez de solo ajustar el TTL del flag se quitó el límite de tiempo por completo: nuevo flag `CeleryConfig.FULL_SYNC_NO_TIME_LIMIT` (default `true`) hace que la tarea se declare sin `time_limit`/`soft_time_limit` de Celery (con `CELERY_FULL_SYNC_NO_TIME_LIMIT=false` se puede volver al comportamiento anterior con límites duros). Para que el lock de Redis y el flag `full_sync_in_progress` no expiren antes de que la tarea termine, `RedisConfig.task_lock()` ganó un parámetro `auto_extend` que levanta un hilo de heartbeat (`lock.extend()` cada mitad del TTL) mientras la tarea sigue viva, y `full_sync_task` levanta un segundo heartbeat análogo para renovar el flag `full_sync_in_progress`. Ambos hilos se detienen y el lock se libera siempre en el `finally`, corra bien o mal la tarea.
  - **Decisión sobre cómo conviven las demás tareas mientras full_sync corre (dejada a mi criterio por el cliente):** no se usan workers/cores separados por tarea. Se mantiene el mecanismo ya existente (`RedisConfig.is_full_sync_in_progress()`), que hace que `periodic_sync_pipeline_task` y `compare_and_update_subscribers_task` se auto-pausen (se registran como "skipped: full_sync en progreso") mientras el flag esté activo, y se retomen solas en su siguiente disparo de Beat una vez que `full_sync_task` libera el flag en su `finally`. Es más simple y más seguro que separar workers por core: no depende de que el operador levante procesos Celery adicionales por entorno, y evita que dos tareas escriban la misma tabla (`ListOfSubscriber`) al mismo tiempo. Si en producción se nota que la pausa de horas de las tareas incrementales es un problema, la alternativa sería sí levantar un worker dedicado solo para `full_sync` (`celery -A panaccess_wind_integration worker -Q full_sync -c 1`) en su propio proceso/core -- no compite por CPU con el worker del pipeline incremental, pero de todos modos seguiría pausando el pipeline mientras full_sync tenga el flag activo, porque el punto no es CPU sino evitar que dos procesos escriban la misma tabla a la vez.
- **Doble alta de suscriptor + doble concesión de trial (resueltos con un solo mecanismo).** Nuevo módulo `wind/services/registration_lock.py`: lock distribuido en Redis por email normalizado y, si aplica, por documento (`register:email:<email>`, `register:doc:<doc>`), adquirido al principio de `create_subscriber_view` y liberado en **todos** los puntos de salida (éxito síncrono, éxito en modo async, y las dos ramas de error). Mientras el lock está tomado: los registros de unicidad (`SubscriberEmailRegistry`/`SubscriberDocumentRegistry`) se escriben de forma síncrona justo después de crear el suscriptor en PanAccess -- **antes** de la rama sync/async (antes esa escritura quedaba diferida a `finish_subscriber_provisioning_task` en modo async, dejando ese modo sin protección real) -- y si corresponde trial, se "reserva" de una vez (`eligible_for_trial=False`) para que ninguna otra request concurrente pueda tomarlo mientras se termina de otorgar el producto en PanAccess. Si el otorgamiento del trial llega a fallar después de reservado (tanto en el flujo síncrono como en `finish_subscriber_provisioning_task`), `revert_trial_reservation()` deshace la reserva para no bloquear permanentemente a alguien que nunca recibió el beneficio. El login social (`social_login_provisioning.py`) llama a `create_subscriber_view` directamente vía `RequestFactory`, así que queda cubierto por el mismo lock sin cambios adicionales.
- **`subscriber_catalog.py` / `profile/serializers.py` — llamadas síncronas a PanAccess dentro de un GET de perfil (resuelto).** `get_subscriber_record`/`build_subscriber_detail_payload`/`build_subscriber_products_payload` ya no llaman a PanAccess dentro del request: leen siempre de caché local (`ListOfSubscriber`/`ListOfSmartcards`). Si la fila no existe o luce incompleta (sin nombre/email, o sin smartcards en caché), se encola la nueva tarea `wind.tasks.refresh_subscriber_profile_task` (cola `sync_pipeline`, lock corto de 60s por `subscriber_code` para no duplicar refrescos si el usuario recarga rápido) y se responde de inmediato con lo que haya en caché, marcado con `pending_sync: true` (a nivel del suscriptor y/o del payload de productos) para que el frontend sepa que puede reconsultar en unos segundos. La próxima consulta ya encuentra el dato actualizado por el refresh en background.

**Quedan pendientes de la sección 16, sin acción (fuera de alcance de esta ronda, ya anotados antes):** UDID (`ValidateAndAssociateUDIDView`, `DisassociateUDIDView`) y `websocket_utils.py` (fingerprint/rate-limit por `client_token`) -- lógica todavía no completada, confirmado por el cliente como tema a revisar más adelante.

### 19. Nuevos hallazgos consultados (2026-07-16, segunda ronda) — resueltos salvo lo indicado

- **`crypto_tv.py` (`get_cached_app_credentials`):52-69 — no descarta claves RSA comprometidas o expiradas.** Confirmado por el cliente como tema a terminar más adelante, sin acción en esta ronda.

- **`getSubscriber.py`:426-495,663-686 — sync incremental podía degradar a recorrido completo o perder altas nuevas por desalineación de criterios de orden (resuelto).** `download_subscribers_since_last` decidía el corte comparando cada fila contra el CÓDIGO local más alto (`LastSubscriber()`, `.latest('code')`), pero `CallListExtendedSubscribers` pagina con `orderBy=created, DESC` -- dos claves de orden independientes, porque los códigos pueden venir de documentos de usuario y no son secuenciales por fecha de alta. Si el suscriptor con el código más alto no era el más reciente, el loop paginaba casi todo el catálogo para encontrarlo; y si ese suscriptor ya no existía en PanAccess (cuenta cerrada y borrada), el corte nunca se encontraba y el loop recorría el catálogo completo en cada corrida de 10 minutos, sin avisar.

  Reescrito para usar la misma clave que la query: el corte ahora es el `created` más reciente que ya está en caché local (`Max("created")` sobre `ListOfSubscriber`), con un margen de solapamiento de `PanaccessConfig.INCREMENTAL_SYNC_OVERLAP_SECONDS` (default 5s) para no perder registros con timestamp igual o reloj levemente desalineado -- reprocesarlos de más es inofensivo porque el guardado es upsert. Se detiene en cuanto una fila tiene `created` <= cursor (coherente con el orden DESC de la query). Se agregó un tope de páginas de seguridad, `PanaccessConfig.INCREMENTAL_SYNC_MAX_PAGES` (default 50): si se supera sin cruzar el corte, se detiene la corrida y loguea error, en vez de seguir paginando el catálogo entero -- lo que falte lo recoge `compare_and_update_subscribers_task` (reconciliación completa cada pocos minutos). Si no hay ningún `created` local confiable (tabla sin ese dato), cae a `fetch_all_subscribers` completo en vez de silenciosamente no traer nada (comportamiento del código anterior si `LastSubscriber()` devolvía `None`).

- **`tasks.py` (locks Redis) — sin renovación, podían expirar durante ejecución larga y permitir tareas duplicadas en paralelo (resuelto, generalizado).** El mecanismo `auto_extend` de `RedisConfig.task_lock()` (construido para `full_sync_task` en la ronda anterior) se extendió a todas las demás tareas periódicas/on-demand que toman un lock: `periodic_sync_pipeline_task`, `sync_subscribers_task`, `sync_products_task`, `compare_and_update_subscribers_task`, `compare_and_update_smartcards_task` y `sync_smartcards_task`. Cada una ahora renueva su lock solo mientras sigue corriendo, así una corrida lenta (PanAccess lento, catálogo grande) no deja que el TTL expire a mitad de camino. De paso, los TTL que estaban hardcodeados en `600` dentro de `tasks.py` (`sync_subscribers_task`, `sync_products_task`, `sync_smartcards_task`, `compare_and_update_smartcards_task`) se movieron a constantes configurables en `appConfig.py` (`CeleryConfig.SYNC_SUBSCRIBERS_LOCK_TIMEOUT`, `SYNC_PRODUCTS_LOCK_TIMEOUT`, `SYNC_SMARTCARDS_LOCK_TIMEOUT`, `COMPARE_SMARTCARDS_LOCK_TIMEOUT`), mismo patrón que ya existía para `COMPARE_SUBSCRIBERS_LOCK_TIMEOUT`/`PIPELINE_LOCK_TIMEOUT`. `retry_partial_closures_task` no usa lock y no lo necesita: es naturalmente idempotente por suscriptor (se apoya en el tombstone `PENDING_CLOSURE` de `close_subscriber_account`), así que una corrida solapada como mucho reintenta dos veces el mismo cierre sin efecto adverso.

### 21. Nuevos hallazgos consultados (2026-07-19) — confirmados por el cliente para revisar más adelante, sin acción en esta ronda

- **`crypto_tv.py`:68-116 — cifrado AES-CBC sin autenticación (no AEAD/HMAC).** Sin un HMAC (encrypt-then-MAC) o un modo AEAD nativo (AES-GCM), un atacante que pueda manipular el ciphertext puede alterarlo sin que el sistema lo detecte antes de descifrar (maleabilidad de CBC, potencial padding oracle). Mitigación estándar cuando se retome: migrar a AES-GCM, o agregar HMAC-SHA256 sobre el ciphertext y verificarlo antes de descifrar.

- **`websocket_utils.py`:280-294 — el "token bucket" documentado en comentarios no existe como tal; es un contador simple no atómico.** El límite de tasa se implementa con un incremento que no es atómico de punta a punta (no usa un script Lua real ni `INCR` atómico con expiración en un solo paso), lo que abre una ventana de condición de carrera bajo concurrencia: dos requests casi simultáneas pueden leer el mismo valor antes de que cualquiera escriba el incremento, dejando pasar más tráfico del límite real pretendido.

- **`models.py` (`UDIDAuthRequest`):543-550 — el contador de intentos fallidos no se aplica en el endpoint real de asociación.** El modelo tiene los campos para llevar la cuenta, pero la vista real de asociación de UDID no los incrementa ni los consulta para cortar el acceso — no hay límite de intentos efectivo ahí, dejando el flujo de asociación abierto a fuerza bruta.

- **`subscriber_code_generator.py`:17-19 — infiere el siguiente código ordenando texto en vez de números.** Ya identificado al validar las auditorías originales: usa `order_by('-code')` sobre un `CharField`, lo que ordena lexicográficamente en vez de numéricamente (ej. `"AUTO10"` ordena antes que `"AUTO9"`), pudiendo generar códigos duplicados o fuera de la secuencia esperada.

**Nota:** UDID (`ValidateAndAssociateUDIDView`, `DisassociateUDIDView`) y `websocket_utils.py` ya estaban marcados como pendientes desde la sección 18 — este hallazgo puntual del contador de intentos y del token bucket son parte de ese mismo tema todavía sin completar, no algo nuevo y separado.

### 20. Confirmación pedida por el cliente (2026-07-17): que ninguna tarea se solape, se acumule o rompa el backend

El cliente pidió una garantía general de que las tareas terminan sin problemas, respetan sus colas, no se solapan ni se acumulan. Se revisó punta a punta y se encontraron y corrigieron dos huecos reales de infraestructura (no de lógica) que sí podían romper el sistema en producción, más una mejora de reintentos. El mecanismo de exclusión mutua en sí (locks Redis) ya estaba correctamente diseñado y se confirma por qué:

**Por qué el patrón lock+skip ya garantiza "nunca dos corridas de la misma tarea en paralelo":** cada tarea con lock hace `acquire(blocking=False)`; si ya está tomado, la nueva instancia se registra como `skipped` y no ejecuta nada. Esto se cumple en **todos** los puntos de entrada -- un disparo nuevo de Beat, un reintento (`self.retry()`) que vuelve a ejecutar la tarea desde el principio, o un click manual en el admin -- todos pasan por el mismo `acquire()` contra la misma llave en Redis antes de tocar cualquier dato. La única ventana real es que, si una corrida falla y se libera el lock antes de que el reintento se dispare (con backoff, hasta varios minutos después), otra invocación *podría* entrar en el medio -- pero eso no es un solapamiento: sigue habiendo como máximo un ejecutor activo en todo momento (el lock lo garantiza), simplemente puede que sea "el reemplazo" en vez de "el reintento original" quien termine haciendo el trabajo. No hay escenario en el que dos corridas escriban la misma tabla al mismo tiempo.

**Hueco real #1 — la cola `compare_reconcile` no tenía ningún worker consumiéndola en el deploy (resuelto, requiere acción en el servidor).** Al implementar `compare_and_update_subscribers_task` cada 5 minutos (sección 15) se le dio una cola dedicada (`compare_reconcile`) para no competir con el pipeline incremental, pero nunca se agregó la unit de systemd correspondiente en `deploy/systemd/`. Resultado: en cualquier entorno desplegado con los scripts de este repo, esos mensajes se **acumulan sin procesarse nunca** -- exactamente el riesgo que se preguntó. Se agregó `deploy/systemd/panaccess-celery-worker-compare.service` (mismo patrón que `-pipeline`/`-full`, `-Q compare_reconcile -c 1`), se actualizaron `deploy/enable_boot_services.sh` y `deploy/manage_services.sh` para incluirlo, y se documentó en `docs/DEPLOYMENT_UBUNTU_NATIVE.md`. **Importante: esto es un archivo de configuración nuevo -- si el servidor ya está desplegado, hace falta copiarlo a `/etc/systemd/system/`, `daemon-reload` y `enable --now` manualmente (comandos exactos en el doc de deploy); actualizar el código por sí solo no arranca el worker.**

**Hueco real #2 — 4 tareas caían en la cola default de Celery, que tampoco tiene worker (resuelto, sin acción pendiente).** `finish_subscriber_provisioning_task`, `send_welcome_credentials_email_task`, `send_password_reset_email_task` y `send_verification_email_task` no tenían entrada en `CELERY_TASK_ROUTES`, así que caían en la cola default (`celery`) -- que tampoco tiene worker en este deploy (mismo problema que el hueco #1, pero sin siquiera una cola dedicada visible en el código para notarlo). A diferencia del hueco #1, este se resolvió rutéandolas a `sync_pipeline` en `settings.py` -- ya hay un worker corriendo ahí, así que **no requiere ninguna acción adicional en el servidor**, el fix toma efecto en el próximo deploy/restart del worker de pipeline.

**Mejora de reintentos — 3 tareas de email/enriquecimiento no reintentaban nunca.** Además de `finish_subscriber_provisioning_task`, se encontró el mismo patrón en `send_password_reset_email_task` y `send_verification_email_task` (a diferencia de `send_welcome_credentials_email_task`, que ya sí reintentaba): un fallo transitorio de SMTP perdía el correo para siempre, sin aviso. Ambas ahora reintentan con el mismo patrón (`bind=True, max_retries=3, default_retry_delay=60`, `self.retry()` en el `except`). `send_verification_email_task` también manda las alertas de cierre parcial agotado, así que este fix aplica también ahí.

**`finish_subscriber_provisioning_task` no reintentaba nunca (detalle).** Los parámetros `max_retries`/`default_retry_delay` del decorador estaban puestos pero sin uso real: cada paso (contactos, license block, producto de prueba) atrapaba `PanAccessException` en general y se rendía en silencio para siempre, sin distinguir un error de negocio de un problema de conectividad transitorio. Ahora los pasos dejan escapar específicamente `PanAccessConnectionError`/`PanAccessTimeoutError`/`PanAccessSessionError`/`PanAccessRateLimitError` hacia un bloque que llama a `self.retry()` con backoff+jitter (hasta 4 intentos); los errores de negocio se siguen absorbiendo localmente igual que antes. Si se agotan los reintentos, se revierte la reserva de trial (`eligible_for_trial=False`) antes de rendirse definitivamente, para no dejarla bloqueada para siempre esperando un otorgamiento que ya no va a llegar por esa vía.

### 23. Implementados los 9 puntos accionables del checklist del 2026-07-19 (sección 21 quedó aparte, diferida)

De los hallazgos de la sección 21, el cliente pidió arreglar todo lo accionable salvo los 4 ítems ya marcados ahí como "para más adelante" (crypto_tv.py AES-CBC, websocket_utils.py token bucket, contador de UDID, orden de subscriber_code_generator.py). Resumen de lo implementado:

- **`panaccess_auth.py`/`panaccess_client.py` sin `requests.Session` (resuelto).** Sesión compartida con `HTTPAdapter(pool_connections=20, pool_maxsize=20)`, creada una sola vez (`get_panaccess_session()`, singleton con lock) y reutilizada en `login()`, `logged_in()` y `PanAccessClient.call()` -- antes cada llamada abría/cerraba su propia conexión TCP+TLS contra PanAccess.
- **`create_subscriber.py` buscaba al suscriptor recién creado paginando todo el catálogo (resuelto).** Los dos puntos (enriquecimiento post-alta y refresco de smartcards tras el license block) ahora piden directo `CallGetSubscriber(subscriber_code=...)` en vez de paginar `getListOfExtendedSubscribers` -- una sola llamada sin importar el tamaño del catálogo, y ya no falla en falso si el suscriptor no cae en las primeras páginas.
- **`countryCode`/`regionId`/`technicalNotes`/`caf` sin pasar por el serializer (resuelto).** `CreateSubscriberSerializer` ahora valida los 4 campos (incluye que `countryCode` sea alfabético) antes de que lleguen a `_create_subscriber_core`; `raw_extra` (el `request.data` crudo) ya no se usa para nada dentro de esa función, se conserva solo por compatibilidad de firma.
- **Comparación de password no era de tiempo constante (resuelto).** `subscriber_auth.py:_check_password_hash` y ambos `check_password()` de `models.py` usan `hmac.compare_digest` en vez de `==`.
- **Mensajes de excepción crudos expuestos al cliente (resuelto).** `profile/views.py` (`profile_password_view`, `profile_close_account_view`) y `change_password.py` ya no devuelven `str(e)` en la respuesta -- loguean el detalle con `logger.exception(...)` y responden un mensaje genérico.
- **Login social sin throttle propio (resuelto).** Nuevo `SocialLoginThrottle` (`scope="social_login"`, default `20/minute` vía `DRF_THROTTLE_SOCIAL_LOGIN`) en `GoogleLoginView`/`FacebookLoginView` -- antes caían en el límite anónimo genérico (60/minute), mucho más laxo para un endpoint que dispara aprovisionamiento en PanAccess.
- **`getSubscriber.py` cargaba toda la tabla en memoria y guardaba fila por fila (resuelto).** `compare_and_update_all_subscribers` ya no arma un diccionario con **todos** los suscriptores locales de una vez: mantiene solo el *set* de códigos (strings, no objetos) para saber qué existe y para el borrado final por diferencia de conjuntos, y por cada página remota (~100 filas) consulta a la BD nada más esos códigos (`filter(code__in=...)`). Los objetos modificados se acumulan y se escriben en bloque con `bulk_update()` (batches de `PANACCESS_DB_WRITE_CHUNK_SIZE`) en vez de un `.save()` por fila -- mismo patrón que ya usaba `getProducts.py`. `store_all_subscribers_in_chunks` (usada por la sync completa e incremental) recibió el mismo cambio de `.save()` por fila a `bulk_update()` por chunk.
- **`log_buffer.py` podía perder hasta ~100 eventos de auditoría si el proceso caía (resuelto).** Cada `add()` ahora también hace `RPUSH` de una copia serializada a una lista de Redis (`wind:audit_log:durable_queue`) antes de encolar en memoria; esa copia se retira (`LREM` por valor exacto, no `LPOP` por cantidad -- ver por qué en el código) recién cuando el `bulk_create` a `AuthAuditLog` confirma éxito. Nueva tarea periódica `recover_pending_audit_logs_task` (cada `CELERY_LOG_BUFFER_RECOVERY_MINUTES`, default 5) escribe a la BD cualquier evento que haya quedado en la cola durable sin confirmarse -- red de seguridad para cuando el proceso se cae entre el `RPUSH` y el `bulk_create`.
- **`create_subscriber.py` — fallos parciales no abortaban el registro, dejando suscriptores a medio aprovisionar sin ninguna señal (resuelto).** Nuevos campos en `ListOfSubscriber`: `provisioning_status` (`complete`/`partial`), `provisioning_pending_steps` (lista de pasos que faltaron: `email_contact`, `phone_contact`, `license_block`, `trial_product`) y `provisioning_retry_count` (migración `0005_partial_provisioning_state`, aditiva, sin backfill). Tanto el flujo síncrono (`_create_subscriber_core`) como el async (`finish_subscriber_provisioning_task`, incluyendo el caso de agotar sus propios reintentos de conectividad) evalúan al final qué pasos no se lograron y lo guardan ahí. Nueva tarea periódica `retry_partial_provisioning_task` (cada `CELERY_PROVISIONING_RETRY_MINUTES`, default 15, hasta `CELERY_PROVISIONING_RETRY_MAX_ATTEMPTS` intentos, default 8) reintenta solo los pasos pendientes -- son idempotentes en PanAccess (ya lo usa `finish_subscriber_provisioning_task` para su propia lógica de reintento), así que repetirlos es seguro. Al agotar los intentos manda la misma alerta por correo que ya existía para cierres parciales agotados (`_alert_provisioning_exhausted`, mismo patrón que `retry_partial_closures_task`). También se cubrió un caso más grave encontrado de paso: si `getSubscriber` fallaba justo después de un `addSubscriber` exitoso, el suscriptor podía quedar existiendo en PanAccess **sin ninguna fila local** -- ahora se crea una fila mínima con los datos ya conocidos de la request para que quede rastreable de inmediato en vez de invisible hasta la próxima sync completa.

**Los 4 ítems diferidos de la sección 21 (crypto_tv.py, websocket_utils.py, contador de UDID, orden de subscriber_code_generator.py) siguen sin acción, tal como pidió el cliente.**

**Verificación:** todo el árbol de `wind/`, `appConfig.py` y `panaccess_wind_integration/settings.py` compila limpio (`py_compile`). La migración `0005_partial_provisioning_state.py` se generó con `makemigrations` contra un `DATABASES` temporal (mismo procedimiento que `0004_db_performance_indexes`, ya que este entorno de verificación no tiene acceso a Postgres) y Django la aceptó como consistente con el estado del modelo -- son solo `AddField` aditivos con default, sin backfill. El intento de `migrate` contra SQLite en este entorno falló por un error de E/S del propio sandbox (no relacionado con el contenido de la migración) y no se pudo completar acá. **Pendiente antes de desplegar:** correr `python manage.py migrate` contra un entorno real (Postgres o SQLite con disco normal) para confirmar que aplica limpio, y `python manage.py test` para la suite completa -- en particular `wind/tests/test_subscriber_sync_closure.py` (cubre `_update_subscriber_from_row`/`_delete_local_subscribers_not_in_remote`, tocados en el fix de `getSubscriber.py`), que no se pudo ejecutar desde este entorno por falta de conexión a Postgres.

### 24. Revisión amplia del resto del proyecto (2026-07-20) — detalle adicional sobre el pairing UDID/Smart TV, ya marcado como pendiente

Se pidió una revisión general del proyecto (fuera de los 9 puntos de la sección 23). La mayoría de las áreas nuevas revisadas (modelos, `admin.py` -- no existe, ninguno registrado --, serializers fuera de UDID, permisos, `password_reset.py`, `jwt_invalidation.py`, SQL crudo -- no hay --, versiones de `requirements.txt` -- no verificables contra CVEs reales sin acceso a internet desde este entorno) no arrojaron hallazgos nuevos de peso. Donde sí apareció material nuevo fue profundizando en el flujo de pairing de Smart TV (UDID), que ya estaba anotado como "lógica no completada, revisar más adelante" en las secciones 5, 13, 16, 18, 19, 21 y 23. Confirmado por lectura directa del código (no solo barrido automático), el detalle es más preciso y más serio de lo que constaba hasta ahora:

- **`ValidateAndAssociateUDIDView` y `DisassociateUDIDView` (`wind/views.py:163` y `:659`) son `AllowAny` sin ninguna verificación de identidad del llamador.** Basta con `subscriber_code` + `sn` (serial de smartcard) + un `operator_id` de texto libre sin validar (`UDIDAssociationSerializer.validate`, `wind/serializers.py:226-267`) para asociar el UDID de un TV al abonado de otra persona, o para desvincular cualquier pairing activo con solo conocer el `udid`. Esto es más específico que la nota genérica ya existente ("lógica no completada") -- es concretamente la ausencia total de control de acceso, no solo una función a medio implementar.
- **`AuthenticateWithUDIDView` (`wind/views.py:454`) confirmado que revienta siempre con `AttributeError`** (`json.serialize_credentials` no existe en el módulo estándar `json`) -- ya estaba anotado en la sección 5, se confirma acá que el error es exactamente ese y que el propio archivo ya importa (sin usar) la función correcta y ya probada `authenticate_with_udid_service`/`json_serialize_credentials` de `wind/services/udid_auth_service.py`, que sí filtra correctamente `is_compromised`/`is_usable()` al elegir la llave RSA -- reutilizar esa función en vez de la lógica duplicada resuelve de una vez el bug de sintaxis y el punto siguiente.
- **Bypass de llaves comprometidas, más amplio de lo ya anotado en la sección 19.** No es solo `get_cached_app_credentials` (`wind/views.py:52-69`, sección 19) -- `hybrid_encrypt_for_app` (`wind/utils/crypto_tv.py:68-116`) hace su **propia** consulta interna a `AppCredentials` ignorando cuál credential ya resolvió el llamador, sin filtrar `is_compromised` y con `.get()` en vez de un filtro (lanzaría `MultipleObjectsReturned` si hay más de una credential activa para el mismo `app_type`).
- **Rate limit por "fingerprint de dispositivo" (`wind/utils/websocket_utils.py:75-102`) bypasseable de raíz.** Si el cliente manda su propio header `X-Device-Fingerprint` (32 hex), se confía tal cual sin ligarlo a ningún dispositivo real; si no, el fingerprint se arma solo con otros headers también controlados por el cliente (`X-Device-Id`, `User-Agent`, etc.) -- rotar cualquiera de ellos por request basta para saltarse el límite de "1 solicitud de pairing cada 5 min". Esto es un ángulo distinto (y más simple de explotar) que el ya anotado en la sección 21 sobre el contador no-atómico del token bucket.
- **`websocket_utils.get_client_ip()` confía en `X-Forwarded-For` sin validar el proxy** -- mismo patrón ya conocido de `sync_admin_ip_middleware._client_ip()`, pero acá alimenta los campos `client_ip` de `UDIDAuthRequest`/`AuthAuditLog`/`EncryptedCredentialsLog`: se podría falsificar la IP que queda en el rastro de auditoría del pairing.
- **Fuga de detalle de excepción (`"details": str(e)}`) en varias respuestas 500 de estas vistas** (`wind/views.py:457, 517, 753` y `wind/services/udid_auth_service.py:148, 204`) -- mismo patrón ya corregido para `/health/` (sección de fixes de 2026-07-20), reaparece acá sin corregir.
- **`rsa_encrypt_for_app()` (`wind/utils/crypto_tv.py:37-65`) está roto** (`private_key.private_key()` no existe como método), pero es código muerto -- nada lo llama hoy (`hybrid_encrypt_for_app` no lo usa). Informativo, no explota nada en el flujo actual.
- **Sin tests para todo el flujo UDID** (`RequestUDIDManualView` a `DisassociateUDIDView`) -- es justo el código que entrega contraseña y PIN de PanAccess en texto plano (cifrados para el dispositivo). Mayor hueco de cobertura del proyecto hoy.

**Estado: sin acción, sumado a la lista de "para revisar más adelante" junto con el resto de UDID (secciones 5, 13, 16, 18, 19, 21, 23), tal como se pidió.** Cuando se retome el desarrollo de este flujo, el orden natural sería: (1) reemplazar la lógica duplicada de `AuthenticateWithUDIDView` por una llamada directa a `authenticate_with_udid_service()` -- resuelve de un solo cambio el crash y el bypass de llaves comprometidas --, (2) decidir qué mecanismo de autenticación/autorización real debe tener `ValidateAndAssociateUDIDView`/`DisassociateUDIDView` antes de considerar el feature completo, y (3) los puntos más chicos (fingerprint, XFF, fuga de excepciones, tests) de paso mientras se trabaja en el resto.
