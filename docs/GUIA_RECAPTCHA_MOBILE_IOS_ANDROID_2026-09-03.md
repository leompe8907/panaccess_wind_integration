# Guía: reCAPTCHA (Fraud Defense) para las apps nativas iOS/Android

Fecha: 2026-09-03
Para: equipo mobile (integración del SDK) + quien administre la cuenta de Google Cloud del proyecto (creación de llaves).
Referencia: `docs/GUIA_CREAR_LLAVES_RECAPTCHA_2026-08-26.md` (la guía equivalente para web, ya implementada), `docs/AUDITORIA_CONSOLIDADA_2026-08-24.md` (`Posibles mejoras #31`), `wind/utils/recaptcha.py` (backend).
Fuente: documentación oficial de Google, verificada al momento de escribir esto -- `docs.cloud.google.com/recaptcha/docs/instrument-android-apps` e `.../instrument-ios-apps`.

**Estado: llaves ya creadas (2026-09-03), falta todo lo demás.** Ni el SDK en las apps, ni el cambio de backend que hace falta para poder verificar estos tokens, están implementados (ver sección final -- es el punto más importante de todo este documento).

## Llaves ya creadas

| Plataforma | Site key |
|---|---|
| Android | `6LepF6ctAAAAAGM8RuGfH6JpA_dVSGTdXVZtbzJ6` |
| iOS | `6LfcuaYtAAAAAE-C1kYG4s61I3-pGnzZupzBLJfg` |

Son públicas (equivalentes a la Site key del flujo web), no secretos -- van directo en el código de cada app nativa como `KEY_ID` (ver Paso 2 y Paso 3 más abajo). No confundir con la legacy secret key del flujo web, que sí es privada.

## Por qué esto es distinto de la guía de web

La guía de web (`GUIA_CREAR_LLAVES_RECAPTCHA_2026-08-26.md`) usa reCAPTCHA v3 "Score based", que corre en el navegador (`grecaptcha.execute()`) y se verifica en el backend contra el endpoint clásico `https://www.google.com/recaptcha/api/siteverify`. **Ese mecanismo no existe para apps nativas** -- no hay navegador, no hay dominio. Para iOS/Android, Google usa **Google Cloud Fraud Defense** (el nombre actual de "reCAPTCHA Enterprise"), con SDKs nativos dedicados y un tipo de llave distinto (atada al package name/bundle ID de la app, no a un dominio).

## Paso 1 -- Crear las llaves (Android y iOS, una cada una) -- YA HECHO

A diferencia de la llave web (que se crea en `google.com/recaptcha/admin/create`), las llaves de app se crean en la **consola de Google Cloud** (`https://console.cloud.google.com/security/recaptcha`, "Create key" → **Platform: Android**/**iOS**). Las dos llaves de Wind ya están creadas (ver tabla arriba). Queda como referencia el detalle de qué se configuró en cada una, por si hay que crear una llave equivalente para otra marca/brand más adelante:

- **Android:** atada al *package name* de la app (ver hallazgo `#39` de la auditoría, que señala un mismatch de este mismo valor en otro lugar de la configuración -- vale la pena confirmar que la llave quedó con el package name correcto) y a la **huella SHA-256** del certificado de firma de la app (se obtiene con `keytool` sobre el keystore de firma, o desde Play Console).
- **iOS:** atada al *bundle ID* de la app. La sección "Apple Developer settings" (private key `.p8` con DeviceCheck habilitado, Key identifier, Team ID) es **"recommended", no obligatoria** -- si no se completó, la key igual funciona, solo que con menos señales para el score de fraude (no puede usar App Attest/DeviceCheck de Apple). Quien tiene que generar el `.p8` es quien administra la cuenta de Apple Developer del cliente (rol Admin o App Manager).

No hay "secret key" del lado del cliente como en web -- las site keys de arriba son lo único que el SDK nativo necesita como `KEY_ID`. La validación real pasa del lado del servidor (ver Paso 4, todavía pendiente).

## Paso 2 -- Integración en Android

```gradle
// build.gradle (app-level)
implementation 'com.google.android.recaptcha:recaptcha:18.9.3'
```

Agregar permiso de Internet en el manifest. Instanciar el cliente **una sola vez** en la vida de la app (ideal: `onCreate()` de la `Application`):

```kotlin
class CustomApplication : Application() {
  private lateinit var recaptchaClient: RecaptchaClient
  private val recaptchaScope = CoroutineScope(Dispatchers.IO)

  override fun onCreate() {
    super.onCreate()
    recaptchaScope.launch {
      try {
        recaptchaClient = Recaptcha.fetchClient(this@CustomApplication, "KEY_ID")
      } catch (e: RecaptchaException) {
        // manejar error
      }
    }
  }
}
```

Por cada acción a proteger (ej. login), ejecutar y obtener el token:

```kotlin
recaptchaClient.execute(RecaptchaAction.LOGIN)
  .onSuccess { token -> /* mandar `token` al backend como recaptcha_token */ }
  .onFailure { exception -> /* manejar error */ }
```

## Paso 3 -- Integración en iOS

CocoaPods:

```ruby
pod "RecaptchaEnterprise", "18.10.0-beta01"
```

o Swift Package Manager con `https://github.com/GoogleCloudPlatform/recaptcha-enterprise-mobile-sdk`. Mínimo iOS 15.

```swift
import RecaptchaEnterprise

class ViewController: UIViewController {
  var recaptchaClient: RecaptchaClient?

  override func viewDidLoad() {
    super.viewDidLoad()
    Task {
      do {
        self.recaptchaClient = try await Recaptcha.fetchClient(withSiteKey: "KEY_ID")
      } catch let error as RecaptchaError {
        print(error.errorMessage)
      }
    }
  }
}
```

Ejecutar y obtener el token:

```swift
let token = try await recaptchaClient.execute(withAction: RecaptchaAction.login)
// mandar `token` al backend como recaptcha_token
```

Si se configuraron los datos de Apple Developer (Paso 1), agregar en Xcode la capability **App Attest** y setear el entitlement de App Attest a `production` antes de probar en un dispositivo real (en simulador/desarrollo se puede usar una llave de testing con score fijo, sin esto).

## Dónde implementar esto en las apps -- las 6 áreas exactas

El backend ya tiene `verify_recaptcha()` conectado en 6 puntos (hoy solo validan tokens del flujo web/clásico -- ver Paso 4). Estas son, archivo por archivo, las pantallas nativas que van a necesitar llamar al SDK y mandar el token en `recaptcha_token`. La acción (`RecaptchaAction`) sugerida en cada fila es la misma que ya usa el flujo web (`getRecaptchaToken(action)`, ver `docs/PLAN_ACTIVACION_RECAPTCHA_2026-08-26.md`), para que los scores queden agrupados de forma consistente en el panel de Google.

| Pantalla nativa | Endpoint backend | Archivo | Acción sugerida |
|---|---|---|---|
| Login (usuario/contraseña) | `POST /api/auth/login/` | `wind/auth_serializers.py` (`PanAccessLoginSerializer`) | `login` |
| Registro / alta de suscriptor | `POST /wind/create-subscriber/` | `wind/functions/create_subscriber.py` | `register` |
| Olvidé mi contraseña | `POST /api/auth/password/forgot/` | `wind/api/password_reset/views.py` (`password_forgot_view`) | `forgot_password` |
| Confirmar restablecimiento de contraseña | `POST /api/auth/password/reset-confirm/` | `wind/api/password_reset/views.py` (`password_reset_confirm_view`) | `reset_password` |
| Cambiar contraseña (dentro de la cuenta, logueado) | `POST /api/v1/profile/password/` | `wind/api/profile/views.py` (`profile_password_view`) | `change_password` |
| Eliminar cuenta | `POST /api/v1/profile/account/close/` | `wind/api/profile/views.py` (`profile_close_account_view`) | `close_account` |

Notas sobre esta lista:

- Es la misma lista de acciones que ya protege el flujo web (login, registro, olvidé/restablecer contraseña, eliminar cuenta) -- **la única que se suma respecto a los "4 flujos" originales es "cambiar contraseña logueado"** (`profile_password_view`), que en el frontend web todavía no manda `recaptcha_token` aunque el backend ya lo acepta (revisar si conviene sumarlo también ahí, es un gap aparte de este documento).
- **Login social (Google/Facebook, `wind/auth_views.py`) no está en esta lista** -- `GoogleLoginView`/`FacebookLoginView` no llaman `verify_recaptcha()` hoy. Si la app nativa usa login social como método principal, ese camino queda sin protección de reCAPTCHA sin importar qué SDK se integre -- es una decisión aparte (¿vale la pena agregarlo ahí también?), no algo que dependa de este trabajo mobile.
- Ninguna de las 6 va a bloquear nada hasta que se resuelva el Paso 4: hoy `verify_recaptcha()` solo entiende tokens del endpoint clásico `siteverify`, así que un token generado por el SDK nativo de Fraud Defense llegaría y **fallaría la verificación** (o, según cómo quede escrito el chequeo, podría interpretarse como token inválido y rechazar el request) en vez de simplemente no evaluarse. No conviene activar el envío de `recaptcha_token` desde las apps nativas hasta que el backend soporte explícitamente el tipo de token de Enterprise.

## Paso 4 -- El cambio que falta en el backend (el punto más importante)

**Esto todavía no está hecho y bloquea que lo anterior sirva de algo.** `wind/utils/recaptcha.py` (`verify_recaptcha()`) hoy solo sabe hablar con el endpoint clásico:

```python
_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
```

Los tokens que generan estos SDKs de Fraud Defense/Enterprise **no son compatibles con ese endpoint** -- son un tipo de token distinto, que se valida contra la **Assessment API** de Google Cloud (`projects.assessments.create`), no contra `siteverify`. Esa API necesita credenciales distintas (un API key de Google Cloud o una service account, más el `project_id`), no la `RECAPTCHA_SECRET_KEY` que ya existe en `.env`.

Cuando se decida avanzar con esto, el trabajo de backend es:

1. Agregar credenciales de Google Cloud (API key o service account) al `.env`/`appConfig.py` -- una variable nueva, no reemplaza `RECAPTCHA_SECRET_KEY` (esa sigue sirviendo para web).
2. Extender (o duplicar) `verify_recaptcha()` para que, según de dónde venga el request (web vs. app nativa -- se puede distinguir por un campo extra en el body, o por el `User-Agent`/`app_type` que ya mandan las apps en otros endpoints), llame a `siteverify` o a la Assessment API según corresponda.
3. Decidir el mismo tipo de umbral (`RECAPTCHA_MIN_SCORE`) para los tokens de Enterprise -- el score viene en un campo distinto de la respuesta (`riskAnalysis.score` en vez de `score` plano).

No se dimensionó el esfuerzo de esto todavía -- es candidato a planificarse como una tarea aparte una vez que el equipo mobile tenga las llaves creadas y el SDK integrado.

## Quién hace qué

| Tarea | Responsable |
|---|---|
| Crear las llaves Android/iOS en Google Cloud | **Hecho** (2026-09-03) -- cliente |
| Generar el `.p8`/Key identifier de Apple Developer (opcional) | Quien administra la cuenta de Apple Developer del cliente |
| Integrar el SDK en la app Android | Equipo Android |
| Integrar el SDK en la app iOS | Equipo iOS |
| Extender `verify_recaptcha()` para aceptar tokens de Enterprise | Backend (Wind) -- pendiente, no dimensionado |

## Referencias

- `https://docs.cloud.google.com/recaptcha/docs/instrument-android-apps`
- `https://docs.cloud.google.com/recaptcha/docs/instrument-ios-apps`
- `https://docs.cloud.google.com/recaptcha/docs/create-key-mobile`
- `https://docs.cloud.google.com/recaptcha/docs/create-assessment-mobile`
