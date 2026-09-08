# Catálogo de tests de `wind` (133 tests, todos OK al 2026-09-08)

Lista completa de la suite (`wind/tests/`), agrupada por archivo y clase, con qué valida cada test. Referencia para saber qué cubre la suite sin tener que abrir cada archivo.

## test_auth.py — registro, login y bloqueo de cuentas cerradas

**SubscriberRegistrationTestCase**
- `test_successful_registration` — registro manual con documento crea el suscriptor y responde OK.
- `test_duplicate_email_validation` — rechaza registro si el email ya está en `SubscriberEmailRegistry`.
- `test_duplicate_document_validation` — rechaza registro si el documento ya está en `SubscriberDocumentRegistry`.

**SubscriberAuthTestCase**
- `test_get_or_create_portal_user_marks_email_verified` — al crear/actualizar el User del portal, el email queda marcado verificado.
- `test_jwt_login_success` — login por credenciales PanAccess (login1/login2) devuelve tokens JWT válidos.

**ClosedSubscriberLoginTestCase** (auditoría sección 17/21 — cerrar la cuenta no debe permitir seguir entrando)
- `test_authenticate_portal_user_rejects_closed_subscriber` — login rechazado si el abonado está `CLOSED` localmente, aunque PanAccess siga aceptando las credenciales.
- `test_authenticate_portal_user_rejects_pending_closure_subscriber` — mismo rechazo para `PENDING_CLOSURE`.
- `test_authenticate_portal_user_allows_active_subscriber` — un abonado activo sí puede loguearse.
- `test_get_or_create_portal_user_does_not_reactivate_closed_subscriber` — **(bug real arreglado hoy)** un abonado cerrado nunca queda con `User.is_active=True`.

## test_create_subscriber_hybrid.py — modo de aprovisionamiento híbrido

**HybridProvisioningTestCase**
- `test_hybrid_mode_completes_sync_when_within_budget` — con presupuesto amplio, todo el aprovisionamiento (contactos, license block, trial) se completa sincrónico en la misma request.
- `test_hybrid_mode_hands_off_to_background_when_budget_exceeded` — si `addSubscriber` ya consumió el presupuesto de tiempo, el resto se encola en background en vez de seguir bloqueando la request.

## test_crypto_tv_aead.py — cifrado de credenciales para apps TV

**HybridEncryptForAppAeadTestCase**
- `test_default_supports_aead_false_keeps_legacy_cbc_payload` — sin el flag activado, una credencial nueva se cifra igual que las existentes (CBC, compatibilidad hacia atrás).
- `test_supports_aead_true_produces_authenticated_gcm_payload` — con el flag activado para esa credencial puntual, usa GCM autenticado.
- `test_mixed_app_types_are_independent` — dos `app_types` distintos (uno con el flag, otro sin) no se pisan entre sí.

## test_device_session.py — sesiones de dispositivo y expiración

**DeviceSessionModelTestCase**
- `test_save_generates_device_token_when_missing` — el modelo genera un token de dispositivo si no viene uno.
- `test_revoke_sets_status_reason_and_timestamp` — revocar una sesión setea status/razón/timestamp correctamente.

**ExpireIdleDeviceSessionsTaskTestCase**
- `test_revokes_only_stale_active_sessions` — la tarea periódica solo revoca sesiones activas que superaron el umbral de inactividad.
- `test_session_exactly_at_threshold_is_not_touched` — una sesión justo en el límite del umbral no se toca (no hay off-by-one).
- `test_noop_when_feature_disabled` — con el feature flag apagado, la tarea no hace nada.

## test_get_smartcard.py — reconciliación de smartcards por abonado

**CompareSmartcardsBySubscriberTestCase**
- `test_updates_existing_and_creates_new_for_subscriber` — sincroniza smartcards remotas de PanAccess: actualiza las que ya existen local y crea las nuevas.
- `test_deletes_local_orphans_for_subscriber` — borra localmente las smartcards que ya no existen en PanAccess.
- `test_skips_deletion_when_pagination_truncated` — si la paginación se cortó por el tope, NO borra (evita falsos huérfanos por datos incompletos).
- `test_routes_to_full_scan_when_forced` — fuerza un escaneo completo cuando se pide explícitamente.
- `test_call_list_omits_order_when_disabled` — no pide el parámetro de orden a PanAccess si esa opción está desactivada.
- `test_incremental_filters_or_last_contact_and_activation` — el modo incremental filtra correctamente por último contacto/activación.
- `test_should_run_full_by_subscriber_when_never_ran` — corre full scan si nunca se corrió antes para ese abonado.
- `test_should_run_full_each_cycle_when_complete_flag` — corre full scan en cada ciclo si el flag de "siempre completo" está activo.
- `test_pipeline_hybrid_runs_incremental_only` — el pipeline híbrido corre solo el paso incremental, no el full.

## test_jwt_password_invalidation.py — invalidación de tokens al cambiar contraseña

**PasswordAwareJWTAuthenticationTestCase**
- `test_allows_token_when_user_has_no_security_profile` — sin perfil de seguridad todavía, el token se acepta (no rompe usuarios viejos).
- `test_rejects_token_issued_before_password_change` — un access token emitido antes de un cambio de contraseña se rechaza.
- `test_allows_token_issued_after_password_change` — uno emitido después sí se acepta.
- `test_allows_token_when_iat_missing` — si el token no trae `iat`, no rompe la validación (fail-safe).

**MarkPasswordChangedTestCase**
- `test_creates_or_updates_security_profile_timestamp` — cambiar contraseña actualiza el timestamp de seguridad del usuario.
- `test_blacklists_outstanding_refresh_tokens` — cambiar contraseña blacklistea los refresh tokens vigentes.
- `test_previously_issued_access_token_is_rejected_after_password_change` — extremo a extremo: un access token viejo (sin pasar por blacklist, que solo cubre refresh) igual queda rechazado por el chequeo de `iat`.

## test_link_device_view.py — vincular TV por URL de deep link

**LinkDeviceViewTestCase**
- `test_valid_udid_redirects_to_dashboard_with_link_tv_param` — UDID válido redirige al dashboard con el parámetro de vinculación.
- `test_malformed_udid_redirects_to_login_instead_of_reflecting_it` — UDID malformado redirige a login, sin reflejar el input crudo en la URL (evita XSS/open redirect).
- `test_oversized_udid_is_rejected` — UDID demasiado largo se rechaza.

## test_panaccess_deprovision.py — baja/desaprovisionamiento en PanAccess

- `DeprovisionHappyPathTestCase.test_full_flow_success` — smartcard con producto asociado: se limpia con `cleanSmartcards` y se borra el suscriptor completo.
- `DeprovisionOrderWithoutSmartcardTestCase.test_orderless_order_uses_disable_order` — orden activa sin smartcard asociada usa `disableOrder` en vez de intentar limpiar una smartcard inexistente.
- `DeprovisionPartialFailureTestCase.test_delete_subscriber_fails_when_smartcard_not_detached` — si `deleteSubscriber` falla porque la smartcard no se desvinculó, se reporta el fallo (no se asume éxito silencioso).
- `DeprovisionDryRunTestCase.test_dry_run_only_reads` — con `dry_run=True` no se dispara ninguna llamada mutable a PanAccess, solo lecturas.

## test_password_reset.py — recuperación y cambio de contraseña

**PasswordResetServiceTestCase**
- `test_request_password_reset_registered_email` — email registrado: se encola el correo de recuperación.
- `test_request_password_reset_unregistered_email` — email no registrado: misma respuesta genérica (no filtra qué emails existen).
- `test_confirm_password_reset_success` — confirmar con token válido cambia la contraseña.
- `test_confirm_password_reset_listofsubscriber_fallback` — regresión: cuentas resueltas solo por el fallback a `ListOfSubscriber` (sin registro en otras tablas) también pueden resetear.
- `test_confirm_password_reset_closed_listofsubscriber_rejected` — una cuenta cerrada resuelta solo por `ListOfSubscriber` sigue rechazada para reset.
- `test_confirm_password_reset_invalid_token` — token inválido/expirado se rechaza.

**PasswordResetAPITestCase**
- `test_forgot_api_generic_response` — el endpoint de "olvidé contraseña" responde igual sin importar si el email existe.
- `test_forgot_api_unknown_email_same_response` — mismo caso desde la API, email desconocido.
- `test_reset_confirm_api_success` — confirmación de reset vía API funciona end-to-end.
- `test_reset_confirm_password_mismatch` — rechaza si las dos contraseñas no coinciden.

**PasswordResetThrottleMessageTestCase**
- `test_formatear_espera_en_minutos` — formatea el tiempo de espera de throttling en minutos, legible.
- `test_forgot_api_throttled_message_is_friendly` — el mensaje de throttle de DRF (que por defecto se filtraba crudo) sale traducido/amigable.

## test_register_view_feature_flag.py — gate de la página de registro

**RegisterViewFeatureFlagTestCase** *(agregado esta semana)*
- `test_register_page_renders_when_enabled` — con el feature activo, `/wind/register/` renderiza normal (200).
- `test_register_page_404s_when_disabled` — con el feature desactivado, la página da 404 en vez de mostrar un formulario que siempre va a fallar.

## test_replica_health.py — réplica de lectura y circuit breaker

**DatabaseConfigConnectTimeoutTestCase**
- `test_default_database_includes_connect_timeout` — la conexión primaria tiene `connect_timeout` configurado.
- `test_replica_database_inherits_connect_timeout` — la réplica hereda el mismo `connect_timeout`.

**ReplicaHealthCircuitBreakerTestCase**
- `test_healthy_by_default` — arranca en estado sano.
- `test_mark_unhealthy_then_healthy_again` — puede marcarse no sana y luego recuperarse.
- `test_unhealthy_mark_expires_on_its_own` — la marca de "no sana" expira sola pasado un tiempo (no queda pegada para siempre).
- `test_router_falls_back_to_default_when_replica_unhealthy` — el router de DB manda lecturas a la primaria si la réplica está mala.
- `test_router_uses_replica_when_healthy` — usa la réplica cuando está sana.
- `test_force_primary_wins_even_if_replica_healthy` — un flag de "forzar primaria" gana aunque la réplica esté sana.

**CheckReplicaHealthTaskTestCase**
- `test_marks_unhealthy_when_query_fails` — si la query de chequeo falla (simulado con `Exception: connection refused`), marca la réplica como no sana. *(el traceback en la consola es parte esperada del test, no una falla real.)*
- `test_marks_healthy_when_query_succeeds` — si la query responde bien, marca sana.
- `test_noop_when_healthcheck_disabled` — con el healthcheck desactivado, la tarea no hace nada.

## test_social_login.py — login social (Google/Facebook)

**PanAccessSocialAccountAdapterTestCase**
- `test_rejects_missing_email` — rechaza si el proveedor no devuelve email.
- `test_rejects_email_not_verified_by_provider` — rechaza si el proveedor no confirma que el email esté verificado.
- `test_accepts_verification_via_extra_data_fallback` — acepta verificación aunque venga solo en el fallback de `extra_data`.
- `test_passes_real_provider_through_not_a_hardcoded_default` — pasa el proveedor real (no un default hardcodeado) al resto del flujo.
- `test_merges_with_existing_local_user_by_email` — un login social se fusiona con un User local ya existente por email.
- `test_translates_subscriber_not_found_into_specific_message` — traduce el error interno a un mensaje específico cuando no hay suscriptor.
- `test_rejects_when_subscriber_code_could_not_be_resolved` — rechaza si no se pudo resolver ningún código de suscriptor.

**EnsureSubscriberForSocialEmailTestCase**
- `test_returns_existing_registry_code_without_hitting_panaccess` — si ya hay un código en el registro, no vuelve a pegarle a PanAccess.
- `test_links_registry_from_existing_list_of_subscriber` — vincula el registro de email desde un `ListOfSubscriber` ya existente.
- `test_raises_when_require_existing_subscriber_is_enabled` — con `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER` activo, bloquea el auto-registro si no hay suscriptor previo.
- `test_auto_creates_subscriber_when_flag_disabled` — con ese flag desactivado, auto-crea el suscriptor (comportamiento actual).

## test_subscriber_closure.py — cierre de cuentas

**CloseSubscriberAccountNoLocalRowTestCase**
- `test_creates_tombstone_when_no_local_row_existed` — si el suscriptor no existía en `ListOfSubscriber`, el cierre crea un registro tombstone.
- `test_updates_existing_row_in_place` — si ya existía la fila local, la actualiza en el lugar.

**CloseSubscriberAccountDeactivatesPortalUserTestCase** (auditoría sección 17/21/22)
- `test_deactivates_user_even_when_panaccess_fails` — el User del portal se desactiva aunque la llamada a PanAccess falle.
- `test_deactivates_user_on_full_success` — se desactiva en el camino feliz también.

## test_subscriber_code_prefix_alphanumeric.py — el bug crítico de esta semana

**SubscriberCodePrefixConstantsTestCase**
- `test_manual_prefix_is_alphanumeric` / `test_manual_auto_prefix_is_alphanumeric` / `test_default_social_prefix_is_alphanumeric` / `test_all_social_provider_prefixes_are_alphanumeric` — las 4 constantes de prefijo (`BM`, `BMAUTO`, `BG`, y las de cada proveedor social) son alfanuméricas puras, sin el `

 que rompió PanAccess.

**SubscriberCodeSentToPanaccessIsAlphanumericTestCase**
- `test_manual_registration_with_document_sends_alphanumeric_code` / `test_manual_registration_without_document_sends_alphanumeric_code` / `test_social_registration_sends_alphanumeric_code` — no alcanza con que los prefijos sean alfanuméricos en la constante; estos 3 confirman que el código que **realmente se envía** a `addSubscriber` (manual con documento, manual sin documento/auto, y social) también lo es, de punta a punta.

## test_subscriber_preferences.py — favoritos y control parental

**PreferencesViewTestCase**
- `test_get_creates_default_row_on_first_access` — el primer GET crea la fila de preferencias por defecto.
- `test_get_rejects_unlinked_user` — rechaza si el usuario no está vinculado a un abonado.
- `test_put_updates_parental_and_favorites` — PUT actualiza control parental y favoritos.
- `test_put_partial_update_does_not_clear_other_field` — un update parcial no borra el otro campo.
- `test_get_reads_back_a_put_by_another_device` — lo que graba un dispositivo se lee desde otro (sincronizado por cuenta, no por dispositivo).
- `test_profile_key_defaults_when_blank` — sin `profile_key`, usa el default.
- `test_different_profile_keys_are_isolated` — perfiles distintos (ej. "kid-profile") no se mezclan entre sí.
- `test_validate_parental_rejects_non_object` — rechaza payload de parental que no sea un objeto.
- `test_validate_favorites_rejects_too_many` — rechaza si se pasan demasiados favoritos.

**GetOrMigratePreferencesTestCase**
- `test_first_real_profile_inherits_default` — el primer perfil real de la cuenta hereda las preferencias del perfil "default".
- `test_second_real_profile_starts_empty` — el segundo perfil real arranca vacío (no hereda de nuevo).
- `test_no_migration_when_no_default_row_exists` — si no hay fila "default", no migra nada.
- `test_existing_row_is_returned_without_touching_migration_logic` — si el perfil ya tiene su propia fila, la devuelve tal cual sin re-migrar.

## test_subscriber_sync_closure.py — protección de cuentas cerradas durante la sincronización masiva

**SubscriberSyncClosureProtectionTestCase**
- `test_closed_tombstone_is_detected` / `test_pending_closure_tombstone_is_detected` — detecta correctamente las filas tombstone de cuentas cerradas/pendientes de cierre.
- `test_active_is_not_tombstone` — una cuenta activa no se confunde con un tombstone.
- `test_delete_preserves_closed_and_pending_missing_from_remote` — al sincronizar contra PanAccess, si una cuenta cerrada/pendiente ya no aparece remoto, NO se borra local (se preserva el tombstone); solo se borran las que de verdad desaparecieron sin estar cerradas.
- `test_update_skips_closed_subscriber` — la sincronización no pisa los datos de una cuenta ya cerrada.

## test_subscriber_trial.py — elegibilidad de trial de registro

**SubscriberTrialEligibilityTestCase**
- `test_new_email_is_eligible` — un email nuevo (sin registro previo) es elegible para trial. *(el mock que le faltaba se arregló hoy.)*
- `test_closed_account_not_eligible_for_trial` — una cuenta con registro de cierre no es elegible.
- `test_active_trial_used_not_eligible` — si el trial ya se usó, no es elegible de nuevo.

**MarkTrialGrantedTestCase**
- `test_mark_trial_granted_sets_flags` — otorgar el trial marca los flags correctos en el registro.

## test_udid_account_association.py — vincular smartcard/TV por cuenta (self-service)

**AssociateUDIDByAccountViewTestCase**
- `test_anonymous_request_is_rejected` — sin sesión, rechaza.
- `test_authenticated_user_without_subscriber_gets_404` — usuario logueado sin abonado vinculado, 404.
- `test_unknown_udid_returns_400` — UDID que no existe, 400.
- `test_expired_udid_is_marked_expired_and_rejected` — UDID vencido se marca expirado y se rechaza.
- `test_non_pending_udid_is_rejected` — UDID que no está en estado "pendiente" se rechaza.
- `test_account_without_smartcard_returns_400` — cuenta sin smartcard asociada, 400.
- `test_smartcard_already_linked_to_another_udid_is_a_conflict` — smartcard ya vinculada a otro UDID, conflicto (409-style).
- `test_account_rate_limit_returns_429` — supera el límite de intentos por cuenta, 429.
- `test_happy_path_associates_and_notifies_websocket_group` — camino feliz: asocia el UDID y notifica por WebSocket al grupo correspondiente.

## test_udid_request_ip_rate_limit.py — rate limiting por IP al pedir UDID

**CheckUdidRequestIpRateLimitTestCase**
- `test_no_ip_is_not_allowed` — sin IP, no se permite.
- `test_allows_up_to_max_requests` — permite hasta el máximo configurado.
- `test_blocks_after_max_requests` — bloquea al superar el máximo.
- `test_different_ips_are_isolated` — el conteo es por IP, no se mezclan.

**RequestUDIDManualViewIpRateLimitTestCase**
- `test_ip_limit_blocks_even_with_rotating_fingerprint` — el límite por IP bloquea aunque el fingerprint del dispositivo cambie en cada intento (evita bypass rotando fingerprint).
- `test_fingerprint_limit_still_applies_within_ip_quota` — el límite por fingerprint se sigue aplicando aunque no se haya llegado al límite de IP.

## test_websocket.py — pairing en tiempo real

**WebSocketPairingTestCase**
- `test_websocket_connect_and_auth_flow` — conexión WebSocket completa: conecta, autentica y participa del flujo de pairing.

## test_welcome_email.py — email de bienvenida con credenciales

**WelcomeEmailContextTestCase**
- `test_display_name_from_first_and_last` — arma el nombre para mostrar a partir de nombre/apellido.
- `test_credentials_from_panaccess` — trae las credenciales reales desde PanAccess cuando están disponibles.
- `test_username_is_always_email_not_login2` — el username mostrado siempre es el email, nunca `login2` (aunque técnicamente ese sea el campo de login real).
- `test_credentials_fallback_when_panaccess_fails` — si PanAccess falla/timeout, cae a un fallback en vez de romper el envío del correo.

**WelcomeEmailRenderTestCase**
- `test_render_includes_credentials_and_store_links` — el HTML renderizado incluye las credenciales y los links de las stores (Android/iOS).
- `test_enqueue_dispatches_celery_task` — encolar el email dispara correctamente la tarea de Celery.
