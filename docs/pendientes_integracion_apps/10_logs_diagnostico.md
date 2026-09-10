# Logs de diagnóstico para desarrolladores

Estado: backend listo (2026-09-01, confirmado 2026-09-03). Implementado en appVideo. El propio diseño de esta funcionalidad está pensado explícitamente para que **iOS/Android la implementen desde el día uno** -- no depende de nada exclusivo de web.
Referencia: `docs/GUIA_INTEGRACION_UNIFICADA.md` sección 7, `docs/LOGS_DIAGNOSTICO_2026-09-01.md`.

## Qué es

Las apps pueden reportar errores/crashes al backend para que el equipo los revise sin depender de que el suscriptor los reporte manualmente. **No es telemetría de negocio ni analítica de uso** -- es diagnóstico técnico puro (stack traces, breadcrumbs de navegación/red antes del error).

## Prerrequisito: ninguno, a propósito (esto es distinto a todo lo demás en la guía)

Este endpoint **no requiere JWT** -- el caso de uso central es capturar errores que ocurren antes de poder loguearse (ej. una pantalla de login rota). Si hay un JWT válido disponible, conviene mandarlo igual (queda asociado al suscriptor); si no hay uno, o está vencido, el reporte se manda igual y queda sin asociar.

Lo que sí es obligatorio es una **API key propia de la integración** (`X-App-Log-Key`, no es un JWT, no expira, no es por usuario) -- **hay que pedir este valor al equipo de backend antes de integrar.** Sin ella, o con una incorrecta, el endpoint devuelve 401 sin importar el resto del body.

## Contrato

`POST /api/v1/logs/`, header `X-App-Log-Key: <api key de la integración>`, y opcionalmente `Authorization: Bearer <jwt>` si hay sesión activa.

```json
{
  "platform": "ios",
  "level": "error",
  "message": "TypeError: no se pudo cargar el EPG",
  "stack": "TypeError: ...\n  at EpgLoader.swift:42",
  "breadcrumbs": [
    { "category": "nav", "message": "abrió BouquetPage" },
    { "category": "http", "message": "GET /api/v1/epg -> 500" }
  ],
  "appVersion": "2.4.0",
  "deviceType": "ios"
}
```

| Campo | Obligatorio | Notas |
|---|---|---|
| `platform` | Sí | Uno de: `web`, `tv_tizen`, `tv_webos`, `ios`, `android`. |
| `level` | No | `error` (default), `warning`, `info`. |
| `message` | Sí | Hasta 2000 caracteres. |
| `stack` | No | Hasta 8000 caracteres. |
| `breadcrumbs` | No | Lista de objetos libres (máx. 100) -- lo último que pasó antes del error. Sin shape fijo, pero conviene al menos `category`/`message`. |
| `extra` | No | Objeto libre, hasta ~20KB serializado. |
| `appVersion` | No | Versión de la app que reporta. |
| `deviceType` | No | Texto libre (modelo, SO, etc.). |

Respuesta 201: `{"success": true}`. Errores: 401 (`X-App-Log-Key` ausente/incorrecta), 400 (`{"success": false, "errors": {...}}`, validación del body), 429 (rate limit, 30/minuto por defecto -- no bloquear ni reintentar en loop).

## Patrón recomendado del lado cliente (no es parte del contrato del backend, pero conviene replicarlo)

- **Buffer local + envío solo si hay error** (mismo patrón que appVideo en `errorReporting.js`): mantener en memoria un ring buffer chico (~50 entradas) de breadcrumbs -- navegación, llamadas de red, acciones del usuario -- y adjuntarlos recién cuando ocurre un error real. No hace falta mandar nada mientras la app funciona bien.
- **Nunca debe bloquear ni romper la app**: si el envío falla (sin red, endpoint caído), descartar o guardar para reintentar más tarde -- el reporte de diagnóstico nunca debe generar un error nuevo ni afectar la experiencia del usuario.
- Deduplicar en el cliente antes de mandar (mismo mensaje+contexto repitiéndose en loop) para no gastar el rate limit en la primera ráfaga de un error que se repite -- el backend igual agrupa por fingerprint del lado servidor, pero evitar el envío redundante ahorra red en el dispositivo.

## Estado en appVideo (web)

Implementado (`src/utils/errorReporting.js`).

## Qué debe implementar iOS/Android nativo

- Pedir a backend la API key de integración (`X-App-Log-Key`) antes de empezar -- sin eso no se puede probar nada, ni siquiera en desarrollo.
- El endpoint mismo (`POST /api/v1/logs/`) -- sin JWT obligatorio, así que puede engancharse incluso antes de tener resuelto el login.
- El patrón de buffer local + envío solo ante error real, igual que appVideo -- evita ruido y gasto de rate limit.
- Enganchar esto al inicio del desarrollo de la app (no como una feature tardía) -- es precisamente la herramienta que sirve para diagnosticar problemas durante el resto de la integración de todo lo demás en esta carpeta.
