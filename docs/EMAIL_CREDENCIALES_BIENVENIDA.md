# Correo de bienvenida con credenciales

**Estado:** implementado (versión solo texto, sin imágenes).

Documento de referencia para el envío automático del correo de bienvenida WindTV al finalizar el registro del suscriptor.

---

## 1. Objetivo

Al completar el registro (HTTP `/wind/create-subscriber/` o vía login social Google/Facebook), el suscriptor recibe un correo electrónico que:

- Usa fondo oscuro y acentos teal (solo HTML/CSS, sin imágenes).
- Saluda por nombre y apellido.
- Muestra credenciales de acceso (usuario y contraseña).
- Incluye enlaces a Google Play y App Store.
- Incluye pie legal y datos de contacto de soporte.

---

## 2. Componentes implementados

| Componente | Ubicación |
|------------|-----------|
| Servicio de renderizado y encolado | `wind/services/welcome_email.py` |
| Plantilla HTML | `wind/templates/wind/emails/welcome_credentials.html` |
| Plantilla texto plano | `wind/templates/wind/emails/welcome_credentials.txt` |
| Tarea Celery (3 reintentos) | `wind/tasks.py` → `send_welcome_credentials_email_task` |
| Disparo post-registro | `wind/functions/create_subscriber.py` |
| Flag cuenta social | `wind/adapters.py` → `request.wind_is_social_account` |
| Configuración | `appConfig.EmailConfig` + variables `.env` |

---

## 3. Variables de entorno

```env
EMAIL_WELCOME_SUBJECT=Bienvenido a WindTV — tus datos de acceso
EMAIL_SUPPORT_ADDRESS=info@wind.do
EMAIL_SUPPORT_PHONE=809.200.3000
EMAIL_TERMS_URL=                          # opcional, enlace en pie
WIND_APP_GOOGLE_PLAY_URL=...
WIND_APP_APP_STORE_URL=...
DEFAULT_FROM_EMAIL=noreply@windtv.wind.do
EMAIL_HOST=...
EMAIL_HOST_USER=...
EMAIL_HOST_PASSWORD=...
```

---

## 4. Flujo

```
Registro exitoso (201)
    ├─ CallGetSubscriberLoginInfo(subscriber_code)  [omitido si es social]
    ├─ render welcome_credentials.html / .txt
    └─ send_welcome_credentials_email_task.delay(...)
```

### Credenciales mostradas

| Tipo de registro | Usuario | Contraseña |
|------------------|---------|------------|
| Web (email) | `login2` o email | Password de PanAccess |
| Google / Facebook | Email social | *Cuenta social no usa contraseña.* |

Si PanAccess no devuelve credenciales, el correo se envía igual con mensaje de fallback (el registro no falla).

---

## 5. Pruebas

```bash
# Ver HTML local sin SMTP ni registro
python manage.py send_welcome_email_test --preview --preview-file preview_welcome.html

# Enviar correo de prueba a tu bandeja (datos ficticios)
python manage.py send_welcome_email_test --to tu@email.com

# Variante cuenta social
python manage.py send_welcome_email_test --to tu@email.com --social

# Credenciales reales de un suscriptor PanAccess existente
python manage.py send_welcome_email_test --to tu@email.com --subscriber-code AUTO12345

# Tests unitarios
python manage.py test wind.tests.test_welcome_email wind.tests.test_auth.SubscriberRegistrationTestCase
```

---

## 6. Seguridad

- La contraseña se incluye en el correo según requerimiento de negocio (mockup).
- No se loguean contraseñas en los logs del servicio.
- El registro HTTP no falla si el envío del correo falla; Celery reintenta hasta 3 veces.

---

## 7. Diseño original (referencia)

El mockup inicial incluía imágenes de cabecera y badges de tiendas. La versión implementada replica los **textos y colores** sin assets gráficos, para mayor compatibilidad y menor dependencia de CDN.
