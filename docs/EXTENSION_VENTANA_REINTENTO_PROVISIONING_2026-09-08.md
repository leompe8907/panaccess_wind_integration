# Extensión de la ventana de reintento de aprovisionamiento parcial

Fecha: 2026-09-08
Referencia: `docs/FIX_PREFIJOS_SUBSCRIBER_CODE_2026-09-07.md` (el bug que dejó ver este problema), `docs/APROVISIONAMIENTO_HIBRIDO_SUSCRIPTOR_2026-08-26.md` (diseño del modo `hybrid`, no relacionado con este cambio).

## Qué

`CELERY_PROVISIONING_RETRY_MINUTES`/`CELERY_PROVISIONING_RETRY_MAX_ATTEMPTS` (`.env`, usados por `wind.tasks.retry_partial_provisioning_task`) pasan de **15 min x 8 intentos (2 horas)** a **60 min x 48 intentos (48 horas)**.

## Por qué

Se detectaron 2 suscriptores reales (`BM05700168916`, `BM22300694134`, registrados el 2026-09-07 vía el formulario público `/wind/create-subscriber/`) cuyo paso `trial_product` nunca se completó: el reintento automático se rindió a las 2 horas porque el suscriptor "todavía no tiene smartcards asignadas" (log de `wind/services/subscriber_provisioning.py`).

Investigación de fondo (importante, no es un bug de código):

- **Este backend nunca crea smartcards** -- solo lee las que ya existen (`getListOfSmartcards`) y les asigna un producto (`addProductToSmartcards`). No hay, ni hubo nunca, ninguna llamada en este repo que provisione una smartcard nueva.
- La asignación real de la smartcard depende de un proceso **externo**, no de este backend. El cliente confirmó que tienen un bot que asigna smartcards hablando **directo contra PanAccess**, sin ninguna integración con Back-Wind-V2 -- ni lo llama, ni este backend lo llama a él.
- **No hay confirmación de que ese bot procese subscriptores creados por este formulario público específicamente** (a diferencia de los que el cliente crea por su propia vía). El cliente en un primer momento dijo "nosotros no los creamos" al ver estos 2 casos, lo cual generó la duda -- no quedó 100% resuelta si el bot los cubre más lento, o si nunca los va a cubrir.

Dado que no hay visibilidad ni control sobre el timing de ese proceso externo, **2 horas es una ventana claramente insuficiente** para cualquier proceso que no sea instantáneo. 48 horas es un margen mucho más razonable para un bot/proceso externo asíncrono, sin llegar a reintentar indefinidamente (que generaría ruido/carga sin límite para casos que nunca se van a resolver).

## Importante -- esto es una mitigación, no una solución garantizada

Si el bot del cliente **nunca** procesa a los suscriptores que entran por este formulario público (en vez de solo tardar más de lo esperado), **ningún valor de esta ventana va a resolver el problema** -- va a seguir fallando, solo que tardará 48hs en vez de 2hs en avisar. En ese escenario, las alternativas reales son:

1. Coordinar con el cliente para que su bot también cubra estos suscriptores.
2. Desactivar el registro público (`CREATE_SUBSCRIBER_PUBLIC_ENABLED=False`) hasta que se resuelva la integración, ya que hoy genera cuentas que nunca reciben su producto de prueba sin intervención manual.

Ninguna de las dos se implementó en este cambio -- quedan pendientes de una decisión de negocio con el cliente.

## Cómo se verificó

- `manage.py shell`: confirmado que `CeleryConfig.PROVISIONING_RETRY_MINUTES` resuelve a `60` y `PROVISIONING_RETRY_MAX_ATTEMPTS` a `48` con el `.env` actual.
- No se tocó código, solo configuración -- el mecanismo de reintento ya soportaba estos valores por `.env` desde que se implementó (`appConfig.py`), no hizo falta ningún cambio de lógica.

## Archivos tocados

- `.env` (`CELERY_PROVISIONING_RETRY_MINUTES`, `CELERY_PROVISIONING_RETRY_MAX_ATTEMPTS`)
