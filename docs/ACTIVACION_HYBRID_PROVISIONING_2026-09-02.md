# Activación del modo `hybrid` de aprovisionamiento (Alto #3)

Fecha: 2026-09-02
Referencia: `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (Alto #3), `docs/APROVISIONAMIENTO_HIBRIDO_SUSCRIPTOR_2026-08-26.md` (diseño e implementación original, sin activar).

## Qué se hizo

La herramienta (modo `hybrid` de `create_subscriber_view`) ya estaba completamente implementada desde el 2026-08-26, apagada por defecto (`sync`). Quedaban 3 cosas antes de activarla en producción, listadas en ese mismo documento:

1. **Coordinar con iOS/Android** el caso `"provisioning_status": "partial"`. Confirmado por el cliente que ya está coordinado.
2. **Instrumentar/loguear los cortes** -- ya existía el log `[Provisioning] Resto del aprovisionamiento de %s encolado en background (modo=%s, motivo=%s)`, suficiente para grep/alertas manuales; no se agregó nada nuevo acá, ver "Pendiente" más abajo.
3. **Probar el corte y el traspaso a background en la práctica** -- hecho en esta sesión (ver más abajo).

Con las 3 confirmadas/hechas, se activó `CREATE_SUBSCRIBER_PROVISIONING_MODE=hybrid` en `.env` (antes `sync`). `CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS` se dejó en `30` (valor que ya estaba configurado, no el default de 8 del código -- alguien lo había subido antes de esta sesión).

## Test nuevo: `wind/tests/test_create_subscriber_hybrid.py`

Dos casos, con los mismos mocks de PanAccess que ya usa `wind.tests.test_auth.SubscriberRegistrationTestCase` (no se llama a PanAccess real):

1. **`test_hybrid_mode_completes_sync_when_within_budget`** -- con presupuesto amplio (8s) y PanAccess "rápido" (mocks instantáneos), confirma que el modo `hybrid` devuelve la respuesta síncrona completa de siempre (con `token`, sin `provisioning_status: partial`) y que `finish_subscriber_provisioning_task.delay` **no** se llama. Es la garantía de "no rompe el caso normal".
2. **`test_hybrid_mode_hands_off_to_background_when_budget_exceeded`** -- simula que PanAccess fue lento: se mockea `time.monotonic()` para que, entre el cálculo del `sync_deadline` y el primer checkpoint (justo antes del lookup), ya haya pasado más tiempo del presupuesto (sin `sleep()` real, sin tests lentos ni flaky). Confirma que se corta ahí, se llama a `finish_subscriber_provisioning_task.delay(subscriber_code=...)` una sola vez con el código correcto, y que la respuesta trae `"provisioning_status": "partial"` sin los campos síncronos (`token`, etc.) -- el contrato exacto que se coordinó con las apps.

Ambos tests corrieron contra Postgres real (`pgserver`, efímero, ver nota de sandbox abajo) -- **2/2 OK**.

### Nota: hallazgo de paso, Redis también hace falta para estos tests

Al escribir el primer test se descubrió que `acquire_registration_locks` (`wind/services/registration_lock.py`) necesita un Redis real (lock distribuido) -- sin él, la vista revienta con `ConnectionError` antes de llegar a ningún otro código. Se mockeó `acquire_registration_locks`/`release_registration_locks` directamente en el test (no es lo que este test evalúa). Esto también aplica a los tests de registro preexistentes (`wind.tests.test_auth.SubscriberRegistrationTestCase`) -- explica, junto con la falta de mock de `verify_recaptcha` ya señalada en `docs/RECAPTCHA_LOGIN_Y_CAMBIO_PASSWORD_2026-09-01.md`, por qué esos tests siguen rotos en este sandbox. No se tocaron esos tests en este cambio (fuera de alcance de A3).

## Cómo revertir si hace falta

Un solo valor: `CREATE_SUBSCRIBER_PROVISIONING_MODE=sync` en `.env` del servidor + restart de Daphne. Sin redeploy de código.

## Pendiente (no bloqueante, seguimiento normal post-activación)

- Vigilar en producción cuántas veces se dispara el corte de verdad (grep del log de arriba, o armar una alerta/métrica sobre esa línea si el volumen lo justifica) para ajustar `CREATE_SUBSCRIBER_SYNC_BUDGET_SECONDS` con datos reales.
- Deploy pendiente: mismo `refresh_stack.sh` de siempre para que el `.env` actualizado y el test nuevo lleguen al servidor.
