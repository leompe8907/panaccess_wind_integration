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

### 16. Hallazgo nuevo, aún sin resolver — login no revisa `status=closed` localmente

Al revisar el flujo de login a raíz de una pregunta sobre cuentas cerradas: `authenticate_portal_user`/`verify_panaccess_credentials` nunca consultan `ListOfSubscriber.status`, y `get_or_create_portal_user` (`subscriber_auth.py:256`) pone `user.is_active = True` sin condición en cada login exitoso por credenciales de PanAccess. Si PanAccess todavía acepta esas credenciales (recordar la duda abierta sobre si `deleteSubscriber` borra de verdad — sección 10), un suscriptor "cerrado" localmente podría seguir iniciando sesión con normalidad, y si su `User` de Django había quedado desactivado por el cierre, este mismo login lo reactiva sin darse cuenta. Los productos de prueba no se ven afectados por esto (solo se otorgan en el registro, nunca en el login), pero el acceso en sí a una cuenta "cerrada" sí queda expuesto. Pendiente de confirmación del cliente para implementar el chequeo explícito.
