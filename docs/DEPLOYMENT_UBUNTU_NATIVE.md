# Guía de Despliegue Nativo en Ubuntu Server (PanAccess - Wind)

Esta guía detalla el proceso paso a paso para realizar un despliegue nativo (sin Docker) de la aplicación de integración PanAccess - Wind sobre un servidor físico o virtual con **Ubuntu Server** limpio (por ejemplo, Ubuntu 22.04 LTS o 24.04 LTS).

Plantillas listas para copiar: carpeta [`deploy/`](../deploy/) (systemd, nginx, script de reinicio).

**Producción Wind:** dominio `backend.wind.do` | usuario de servicio `wind` | admin `sw4`.

---

## Índice

1. [Arquitectura del sistema](#arquitectura-del-sistema)
2. [Requisitos del servidor](#requisitos-del-servidor)
3. [Perfil recomendado: 32 GB / 16 cores](#perfil-recomendado-32-gb--16-cores)
4. [Pasos 1–10: instalación y configuración](#paso-1-conexión-ssh-y-actualización-inicial)
5. [Paso 11: diagnóstico, refresco y mantenimiento](#paso-11-comandos-de-diagnóstico-y-mantenimiento)
6. [Paso 12: verificación post-despliegue](#paso-12-verificación-post-despliegue)
7. [Paso 13: solución de problemas](#paso-13-solución-de-problemas)
8. [Paso 14: checklist final](#paso-14-checklist-final)

---

## Arquitectura del Sistema

Despliegue **monolítico en una sola instancia**: todos los componentes corren en el mismo servidor Ubuntu.

```
Internet → Nginx (443/80) → Daphne (127.0.0.1:8000, ASGI)
                                ├── PostgreSQL (5432)
                                └── Redis (6379) ← Celery workers + Beat
```

| Componente | Rol |
|------------|-----|
| **PostgreSQL** | Base de datos (`systemd`) |
| **Redis** | Broker Celery, caché Django, channel layer WebSockets |
| **Daphne** | HTTP + WebSockets (emparejamiento Smart TV) en puerto `8000` |
| **Celery** | 2 workers (`sync_pipeline`, `full_sync`) + Beat |
| **Nginx** | Proxy inverso, SSL, estáticos, rate limiting |

Servicios `systemd` de aplicación (4):

| Servicio | Cola / rol |
|----------|------------|
| `panaccess-wind.service` | Daphne (API + `/ws/`) |
| `panaccess-celery-worker-pipeline.service` | Cola `sync_pipeline` |
| `panaccess-celery-worker-full.service` | Cola `full_sync` |
| `panaccess-celery-worker-compare.service` | Cola `compare_reconcile` |
| `panaccess-celery-beat.service` | Agenda de tareas periódicas |

> **Importante:** Este proyecto requiere **Daphne (ASGI)**, no Gunicorn solo. Gunicorn no sirve WebSockets de Django Channels.

---

## Requisitos del Servidor

| Componente | Mínimo | Recomendado | Alto rendimiento (tu VM) |
|------------|--------|-------------|--------------------------|
| **CPU** | 2 cores | 4 cores | **16 cores** |
| **RAM** | 4 GB | 8 GB | **32 GB** |
| **Disco** | 20 GB SSD | 50 GB SSD | 80+ GB SSD/NVMe |
| **SO** | Ubuntu 22.04 LTS | Ubuntu 22.04 / 24.04 LTS | Ubuntu 22.04 / 24.04 LTS |

| Perfil | Instancias Daphne | Conexiones simultáneas orientativas |
|--------|-------------------|-------------------------------------|
| Mínimo (2–4 cores) | 1 | ~500 |
| Medio (4–8 cores) | 2–4 | ~1 000 |
| **32 GB / 16 cores** | **8** (hasta 12 si hace falta) | ~3 000–5 000 |

> Daphne no tiene `--workers` como Gunicorn. Para escalar se levantan **varias instancias** en puertos distintos y Nginx reparte con `upstream`.

### Usuarios Linux (servidor Wind)

| Usuario | Rol |
|---------|-----|
| **`sw4`** | Admin humano: SSH, `sudo`, `git pull` (como `sudo -u wind`), mantenimiento |
| **`wind`** | Dueño de `/opt/panaccess-wind`, `.env` y procesos systemd (`User=wind`) |

Todos los `.service` en `deploy/systemd/` usan `User=wind`. No existe usuario `ubuntu` en este servidor.

```bash
sudo chown -R wind:wind /opt/panaccess-wind
sudo chmod 600 /opt/panaccess-wind/.env
grep "^User=" /etc/systemd/system/panaccess-*.service   # debe ser User=wind
```

Para cargas mayores, ajusta `maxmemory` de Redis (~25% de la RAM total):

```bash
sudo nano /etc/redis/redis.conf
# maxmemory 8gb          # perfil 32 GB RAM
# maxmemory-policy allkeys-lru
sudo systemctl restart redis-server
```

---

## Perfil recomendado: 32 GB / 16 cores

Con tu VM conviene el **modo escalado** (8 instancias Daphne). Celery sigue en `-c 1` por cola: el sync pesado no debe paralelizarse ahí.

### Distribución de memoria orientativa

| Componente | RAM asignada |
|------------|--------------|
| Sistema operativo | ~4 GB |
| PostgreSQL | ~8 GB (`shared_buffers`) |
| Redis | ~8 GB (`maxmemory`) |
| Daphne (8 × ~500 MB) | ~4 GB |
| Celery (2 workers + Beat) | ~2 GB |
| Nginx + margen | ~6 GB |

### 1. PostgreSQL (tuning básico)

```bash
sudo nano /etc/postgresql/*/main/postgresql.conf
```

```conf
shared_buffers = 8GB
effective_cache_size = 24GB
max_connections = 200
work_mem = 16MB
```

```bash
sudo systemctl restart postgresql
```

### 2. Redis

```conf
maxmemory 8gb
maxmemory-policy allkeys-lru
```

### 3. Daphne — 8 instancias (puertos 8000–8007)

**No** uses `panaccess-wind.service` (instancia única) en este perfil.

```bash
sudo cp deploy/systemd/panaccess-wind@.service /etc/systemd/system/
sudo cp deploy/systemd/panaccess-wind.target /etc/systemd/system/
sudo sed -i 's/^User=.*/User=wind/' /etc/systemd/system/panaccess-wind@.service
sudo systemctl daemon-reload
sudo chmod +x deploy/manage_daphne.sh

# Arranque automático + inicio
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh enable
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh start
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh status
```

Verifica puertos:

```bash
sudo ss -tlnp | grep -E '800[0-7]'
```

Si tras monitorizar (`htop`, `journalctl`) la CPU de Daphne sigue alta con muchas TVs conectadas, sube a **12 instancias** (puertos 8000–8011) y añade esos servidores al `upstream` de Nginx. Con 32 GB no conviene pasar de ~12 instancias (~6 GB solo en Daphne).

### 4. Nginx — upstream con balanceo

La plantilla `deploy/nginx/panaccess-wind-scaled.conf` ya usa **`backend.wind.do`** y certificados en `/etc/nginx/cdn1.wind.do.{crt,key}`. Siempre haz backup antes de sobrescribir.

```bash
sudo cp /etc/nginx/sites-available/panaccess-wind.conf \
        /etc/nginx/sites-available/panaccess-wind.conf.bak.$(date +%F)

sudo cp deploy/nginx/panaccess-wind-scaled.conf /etc/nginx/sites-available/panaccess-wind.conf

# Verificar certificados (deben existir en /etc/nginx/)
sudo ls -l /etc/nginx/cdn1.wind.do.crt /etc/nginx/cdn1.wind.do.key
# Si faltan: copiar los archivos o usar panaccess-wind-bootstrap-http.conf (Paso 10)

sudo ln -sf /etc/nginx/sites-available/panaccess-wind.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
curl -sk https://backend.wind.do/health/
```

**Alternativa mínima:** conserva tu `server { ... }` actual y solo reemplaza el bloque `upstream django_backend` por el de 8 puertos (8000–8007) de `deploy/nginx/panaccess-wind-scaled.conf`.

### 5. Celery — sin cambios

Mantén `-c 1` en pipeline y full_sync. La concurrencia hacia PanAccess se controla por `.env`:

```env
PANACCESS_SMARTCARD_SUBSCRIBER_CONCURRENCY=4
```

Sube ese valor con cuidado si PanAccess tolera más llamadas paralelas; no aumentes `-c` del worker Celery.

### 6. Reinicio tras deploy (perfil escalado)

Resumen rápido (deploy normal). Para reset duro o verificación ampliada, ver [Paso 11](#paso-11-comandos-de-diagnóstico-y-mantenimiento).

```bash
cd /opt/panaccess-wind
source env/bin/activate
git pull
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check

DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart
sudo deploy/manage_services.sh restart
sudo nginx -t && sudo systemctl reload nginx

curl -sk https://backend.wind.do/ready/
curl -sk https://backend.wind.do/health/
```

---

## Paso 1: Conexión SSH y Actualización Inicial

1.  **Conéctate a tu servidor por SSH:**
    ```bash
    ssh usuario@ip_del_servidor
    ```
2.  **Actualiza los repositorios y paquetes del sistema:**
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```
3.  **Instala las dependencias y compiladores básicos del sistema:**
    ```bash
    sudo apt install -y git python3-pip python3-venv python3-dev build-essential libpq-dev curl ufw
    ```

---

## Paso 2: Configuración del Firewall (UFW)
Asegura los accesos al servidor abriendo únicamente los puertos indispensables:
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh             # Puerto 22
sudo ufw allow http            # Puerto 80
sudo ufw allow https           # Puerto 443
sudo ufw --force enable
```

---

## Paso 3: Instalación y Configuración de PostgreSQL

1.  **Instala PostgreSQL y sus utilidades:**
    ```bash
    sudo apt install -y postgresql postgresql-contrib
    ```
2.  **Inicia y habilita el servicio de base de datos:**
    ```bash
    sudo systemctl enable --now postgresql
    ```
3.  **Accede al prompt interactivo de PostgreSQL:**
    ```bash
    sudo -i -u postgres psql
    ```
4.  **Crea la base de datos y el usuario con privilegios** (conectado a la base `postgres`):
    ```sql
    CREATE DATABASE wind_db;
    CREATE USER wind_user WITH PASSWORD 'CONTRASEÑA_FUERTE_POSTGRES';
    ALTER ROLE wind_user SET client_encoding TO 'utf8';
    ALTER ROLE wind_user SET default_transaction_isolation TO 'read committed';
    ALTER ROLE wind_user SET timezone TO 'UTC';
    GRANT ALL PRIVILEGES ON DATABASE wind_db TO wind_user;
    ```
5.  **Conecta a `wind_db` y otorga permisos en el esquema `public`** (obligatorio; los `GRANT` anteriores no aplican a tablas ya existentes):
    ```sql
    \c wind_db

    GRANT ALL ON SCHEMA public TO wind_user;
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO wind_user;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO wind_user;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO wind_user;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO wind_user;

    ALTER DATABASE wind_db OWNER TO wind_user;
    ALTER SCHEMA public OWNER TO wind_user;
    ```
6.  **Si la base ya tenía tablas creadas por `postgres`** (p. ej. tras un `migrate` ejecutado como superusuario), transfiere la propiedad de los objetos de aplicación:
    > `REASSIGN OWNED BY postgres TO wind_user` puede fallar con *«objetos requeridos por el sistema»*. Usa este bloque en su lugar:
    ```sql
    DO $$
    DECLARE r RECORD;
    BEGIN
        FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
        LOOP
            EXECUTE format('ALTER TABLE public.%I OWNER TO wind_user', r.tablename);
        END LOOP;
        FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'
        LOOP
            EXECUTE format('ALTER SEQUENCE public.%I OWNER TO wind_user', r.sequencename);
        END LOOP;
    END $$;
    ```
    Sal del prompt:
    ```sql
    \q
    ```
7.  **Verifica la conexión con el usuario de la aplicación:**
    ```bash
    psql -U wind_user -d wind_db -h 127.0.0.1 -c "SELECT 1;"
    ```
    La contraseña debe coincidir con `DB_PASSWORD` en `.env`.

---

## Paso 4: Instalación y Configuración de Redis

Redis funcionará como caché para control de flujo (Throttling) y como broker para el encolado de tareas de Celery.

1.  **Instala Redis Server:**
    ```bash
    sudo apt install -y redis-server
    ```
2.  **Habilita e inicia el servicio de Redis:**
    ```bash
    sudo systemctl enable --now redis-server
    ```
3.  **Verifica que Redis responda correctamente:**
    ```bash
    redis-cli ping
    ```
    *(Debe responder `PONG`).*

---

## Paso 5: Despliegue de Código y Entorno Virtual

1.  **Crea el directorio de instalación y otorga permisos:**
    ```bash
    sudo mkdir -p /opt/panaccess-wind
    sudo chown -R $USER:$USER /opt/panaccess-wind
    cd /opt/panaccess-wind
    ```
2.  **Clona o copia el código en el directorio:**
    ```bash
    # Si usas Git:
    git clone https://github.com/tu-usuario/tu-repositorio.git .
    ```
3.  **Crea el entorno virtual de Python (`venv`):**
    ```bash
    python3 -m venv env
    ```
4.  **Activa el entorno e instala las dependencias:**
    ```bash
    source env/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    ```

---

## Paso 6: Configuración de Variables de Entorno (`.env`)

Crea el archivo `.env` en la raíz del proyecto para definir la configuración de producción:

```bash
nano /opt/panaccess-wind/.env
```

Pega el siguiente contenido y personaliza los valores con datos reales:

```env
# --- Configuración Básica de Django ---
DEBUG=False
SECRET_KEY=TU_LLAVE_SECRETA_SUPER_SEGURA_AQUI
ALLOWED_HOSTS=backend.wind.do,localhost,127.0.0.1

# --- Configuración de Base de Datos (Local PostgreSQL) ---
DB_NAME=wind_db
DB_USER=wind_user
DB_PASSWORD=CONTRASEÑA_FUERTE_POSTGRES
DB_HOST=127.0.0.1
DB_PORT=5432

# --- Configuración de Redis y Celery ---
REDIS_URL=redis://127.0.0.1:6379/0
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/0

# --- Integración Externa PanAccess SOAP ---
PANACCESS_SOAP_URL=https://api.panaccess.com/soap/index.php
PANACCESS_USERNAME=tu_usuario_soap
PANACCESS_PASSWORD=tu_contraseña_soap
PANACCESS_OPERATOR_ID=tu_operador_id
PANACCESS_DEFAULT_PRODUCT_ID=4639

# --- Configuración de Email (SMTP para verificación) ---
EMAIL_HOST=smtp.sendgrid.net
EMAIL_PORT=587
EMAIL_HOST_USER=apikey
EMAIL_HOST_PASSWORD=tu_smtp_password
EMAIL_FROM_ADDRESS=soporte@wind.do

# --- Autenticación Social (Google y Facebook) ---
GOOGLE_CLIENT_ID=tu_google_client_id
FACEBOOK_APP_ID=tu_facebook_app_id
```

---

## Paso 7: Migraciones, Archivos Estáticos y Sincronización Inicial

Con el entorno virtual activado, corre los siguientes comandos preparativos:

1.  **Ejecuta las migraciones de la base de datos:**
    ```bash
    python manage.py migrate
    ```
2.  **Recolecta los archivos estáticos en el directorio configurado:**
    ```bash
    python manage.py collectstatic --noinput
    ```
3.  **Crea tu superusuario administrador de Django:**
    ```bash
    python manage.py createsuperuser
    ```
4.  **Ejecuta el pre-calentamiento (Warm-up) de datos:**
    Sincroniza todos los suscriptores existentes en PanAccess SOAP a la base de datos local para evitar cuellos de botella iniciales:
    ```bash
    python manage.py run_full_sync
    ```

---

## Paso 8: Configuración de Procesos con Systemd

Crearemos cuatro servicios en systemd para mantener Daphne, los dos workers Celery y Beat corriendo de forma ininterrumpida.

**Opción rápida** (plantillas del repo):

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
# User=wind ya viene en las plantillas; verifica tras copiar:
grep "^User=" /etc/systemd/system/panaccess-*.service
sudo chmod +x deploy/manage_services.sh
```

**Opción manual:** crea cada archivo como se indica abajo.

### Orden de arranque recomendado

1. Completar `python manage.py run_full_sync` (Paso 7).
2. Iniciar Daphne + workers Celery.
3. **Activar Beat solo después** de que el warm-up inicial haya terminado correctamente.

### 1. Servicio de la Aplicación Web (Daphne/ASGI)
Crea el archivo `/etc/systemd/system/panaccess-wind.service`:
```bash
sudo nano /etc/systemd/system/panaccess-wind.service
# o: sudo cp deploy/systemd/panaccess-wind.service /etc/systemd/system/
```
Escribe el siguiente contenido:
```ini
[Unit]
Description=Servicio Web PanAccess Wind Integration (Daphne)
After=network.target postgresql.service redis-server.service

[Service]
User=wind
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/daphne -b 127.0.0.1 -p 8000 panaccess_wind_integration.asgi:application
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 2. Workers de Celery (pipeline + full sync)

Se usan **tres workers** con colas separadas:

| Worker | Cola | Rol |
|--------|------|-----|
| `panaccess-celery-worker-pipeline` | `sync_pipeline` | Pipeline periódico: suscriptores → smartcards (serie, `-c 1`). También recibe emails y tareas sueltas (`finish_subscriber_provisioning_task`, correos de bienvenida/reset/verificación) -- ver nota más abajo. |
| `panaccess-celery-worker-full` | `full_sync` | Full sync nocturno exclusivo (`-c 1`) |
| `panaccess-celery-worker-compare` | `compare_reconcile` | Reconciliación frecuente de subscribers/smartcards (cada `CELERY_COMPARE_SUBSCRIBERS_MINUTES`, default 5 min, `-c 1`) |

**Importante (corregido en auditoría, sección 20):** hasta esta revisión, `compare_and_update_subscribers_task`/`compare_and_update_smartcards_task` estaban programadas cada 5 minutos en Beat con cola propia `compare_reconcile`, pero **no existía ningún worker consumiendo esa cola** en este deploy -- los mensajes se acumulaban sin procesarse nunca. Si tu entorno ya estaba desplegado antes de esta fecha, **hace falta instalar y arrancar `panaccess-celery-worker-compare.service` manualmente** (no pasa solo con actualizar el código):

```bash
sudo cp deploy/systemd/panaccess-celery-worker-compare.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now panaccess-celery-worker-compare.service
```

Variables en `.env`:

```env
CELERY_SYNC_PIPELINE_QUEUE=sync_pipeline
CELERY_FULL_SYNC_QUEUE=full_sync
CELERY_COMPARE_SUBSCRIBERS_QUEUE=compare_reconcile
CELERY_PIPELINE_LOCK_TIMEOUT=1800
CELERY_TASK_ALWAYS_EAGER=false
```

**Pipeline** (`/etc/systemd/system/panaccess-celery-worker-pipeline.service`):

```ini
[Unit]
Description=Celery Worker pipeline (subscribers -> smartcards)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=wind
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/celery -A panaccess_wind_integration worker -Q sync_pipeline -c 1 --loglevel=info -n pipeline@%h
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**Full sync** (`/etc/systemd/system/panaccess-celery-worker-full.service`):

```ini
[Unit]
Description=Celery Worker full sync nocturno
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=wind
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/celery -A panaccess_wind_integration worker -Q full_sync -c 1 --loglevel=info -n fullsync@%h
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**Compare/reconcile** (`/etc/systemd/system/panaccess-celery-worker-compare.service`):

```ini
[Unit]
Description=Celery Worker compare_reconcile (reconciliacion frecuente subscribers/smartcards)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=wind
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/celery -A panaccess_wind_integration worker -Q compare_reconcile -c 1 --loglevel=info -n compare@%h
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Ya no hace falta un worker aparte para la cola default `celery`: `finish_subscriber_provisioning_task` y los correos (bienvenida, reset de contraseña, verificación) están ruteados explícitamente a `sync_pipeline` en `CELERY_TASK_ROUTES` (antes caían en la cola default `celery`, que tampoco tenía worker en este deploy -- mismo problema que `compare_reconcile`, corregido en la misma revisión).

> Mientras `full_sync` corre, el flag Redis `celery:flag:full_sync_in_progress` hace que el pipeline **se omita** automáticamente.

### Smartcards: sync híbrido (incremental + por abonado)

El pipeline usa `run_smartcard_sync_for_pipeline`:

1. **Incremental** (cada ciclo): `lastContact` o `lastActivation` > último sync — pocas llamadas API, escala a 10k+ abonados.
2. **Por abonado** (cada `PANACCESS_SMARTCARD_FULL_BY_SUBSCRIBER_EVERY_HOURS`, default 24h): reconcilia altas/bajas por subscriber.
3. **Full scan** (solo `full_sync` nocturno): inventario completo 500k.

Variables `.env`:

```env
PANACCESS_SMARTCARD_SYNC_INCREMENTAL=true
PANACCESS_SMARTCARD_INCREMENTAL_LOOKBACK_HOURS=24
PANACCESS_SMARTCARD_INCREMENTAL_MAX_PAGES=0
PANACCESS_SMARTCARD_SUBSCRIBER_SYNC_MAX_PAGES=0
PANACCESS_SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE=true
PANACCESS_SMARTCARD_FULL_BY_SUBSCRIBER_EVERY_HOURS=24
```

`MAX_PAGES=0` = paginar hasta agotar todos los resultados (sin corte artificial).
`PIPELINE_COMPLETE_EACH_CYCLE=true` = reconciliación completa por abonado en **cada** ciclo del pipeline.

Timestamps en Redis: `celery:smartcard_sync:last_incremental_at`, `celery:smartcard_sync:last_full_by_subscriber_at`.

### 3. Servicio de Celery Beat (Tareas Programadas)
Crea el archivo `/etc/systemd/system/panaccess-celery-beat.service`:
```bash
sudo nano /etc/systemd/system/panaccess-celery-beat.service
```
Escribe el siguiente contenido:
```ini
[Unit]
Description=Celery Beat (Agenda de Sincronizacion)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=wind  # Reemplaza por tu usuario real
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/celery -A panaccess_wind_integration beat --loglevel=info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 4. Activar e Iniciar todos los Servicios

Recarga systemd e inicia **primero** Daphne y workers (Beat al final, tras el full sync inicial):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now panaccess-wind.service panaccess-celery-worker-pipeline.service panaccess-celery-worker-full.service panaccess-celery-worker-compare.service
# Tras confirmar run_full_sync OK:
sudo systemctl enable --now panaccess-celery-beat.service
```
Verifica que estén activos sin errores:
```bash
sudo systemctl status panaccess-wind.service
sudo systemctl status panaccess-celery-worker-pipeline.service
sudo systemctl status panaccess-celery-worker-full.service
sudo systemctl status panaccess-celery-worker-compare.service
sudo systemctl status panaccess-celery-beat.service
```

---

## Paso 9: Configuración de Nginx como Proxy Inverso

1.  **Instala Nginx:**
    ```bash
    sudo apt install -y nginx
    ```
2.  **Crea el archivo de configuración del sitio:**
    ```bash
    sudo cp deploy/nginx/panaccess-wind.conf /etc/nginx/sites-available/panaccess-wind.conf
    sudo nano /etc/nginx/sites-available/panaccess-wind.conf   # ajustar dominio y SSL
    ```
    O créalo manualmente con la estructura siguiente (HTTP, WebSockets, health checks):

```nginx
# Límite de peticiones a nivel de Nginx para el endpoint de registro público
limit_req_zone $binary_remote_addr zone=win_register:10m rate=5r/m;

upstream django_backend {
    # Apunta al socket local de Daphne
    server 127.0.0.1:8000 fail_timeout=0;
}

server {
    listen 80;
    listen [::]:80;
    server_name backend.wind.do;
    
    # Redirección permanente a HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name backend.wind.do;

    # Rutas de los certificados SSL (copiados a /etc/nginx/ en el paso 10)
    ssl_certificate     /etc/nginx/cdn1.wind.do.crt;
    ssl_certificate_key /etc/nginx/cdn1.wind.do.key;
    
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    client_max_body_size 20M;

    # --- Restringir rutas de sincronización crítica a la red interna (VPN / Local) ---
    location ~ ^/wind/(sync-|compare-and-update|full-sync|singleton|ops/) {
        allow 127.0.0.1;
        # allow 10.8.0.0/24; # Descomenta y define la IP de tu VPN aquí
        deny all;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://django_backend;
    }

    # Restringir Django Admin
    location ^~ /admin/ {
        allow 127.0.0.1;
        # allow 10.8.0.0/24; # VPN de la empresa
        deny all;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://django_backend;
    }

    # Registro de usuarios: Límite suave de peticiones
    location = /wind/create-subscriber/ {
        limit_req zone=win_register burst=5 nodelay;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://django_backend;
    }

    # Probes de salud (liveness / readiness)
    location = /health/ {
        access_log off;
        proxy_set_header Host $host;
        proxy_pass http://django_backend;
    }

    location = /ready/ {
        access_log off;
        proxy_set_header Host $host;
        proxy_pass http://django_backend;
    }

    # Redirección de WebSockets (Smart TV pairing)
    location /ws/ {
        proxy_pass http://django_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_read_timeout 86400s; # Evita desconexiones por inactividad
        proxy_send_timeout 86400s;
    }

    # Servir archivos estáticos directamente desde Nginx (Alto Rendimiento)
    location /static/ {
        alias /opt/panaccess-wind/staticfiles/;
    }

    # Servir archivos multimedia
    location /media/ {
        alias /opt/panaccess-wind/mediafiles/;
    }

    # Resto de la API pública y rutas del portal web
    location / {
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_read_timeout 120s;
        proxy_pass http://django_backend;
    }
}
```

4.  **Habilita el sitio en Nginx:**
    ```bash
    sudo ln -s /etc/nginx/sites-available/panaccess-wind.conf /etc/nginx/sites-enabled/
    ```
5.  **Verifica la sintaxis e inicia Nginx:**
    ```bash
    sudo nginx -t
    sudo systemctl restart nginx
    ```

---

## Paso 10: Certificados SSL en Nginx

Los certificados se colocan directamente en `/etc/nginx/` (provistos por infraestructura / CDN):

| Archivo | Ruta |
|---------|------|
| Certificado | `/etc/nginx/cdn1.wind.do.crt` |
| Clave privada | `/etc/nginx/cdn1.wind.do.key` |

Si **aún no existen**, no uses la plantilla HTTPS directamente: Nginx fallará en `nginx -t`. Sigue este orden:

### 10.1 Copiar certificados al servidor

```bash
sudo cp cdn1.wind.do.crt /etc/nginx/cdn1.wind.do.crt
sudo cp cdn1.wind.do.key /etc/nginx/cdn1.wind.do.key
sudo chmod 644 /etc/nginx/cdn1.wind.do.crt
sudo chmod 600 /etc/nginx/cdn1.wind.do.key
sudo chown root:root /etc/nginx/cdn1.wind.do.crt /etc/nginx/cdn1.wind.do.key
```

### 10.2 Bootstrap temporal (solo HTTP, si aún no tienes los `.crt`/`.key`)

```bash
sudo cp deploy/nginx/panaccess-wind-bootstrap-http.conf /etc/nginx/sites-available/panaccess-wind.conf
sudo nginx -t && sudo systemctl reload nginx
curl -s http://backend.wind.do/health/
```

> `backend.wind.do` debe apuntar por DNS a la IP pública de este servidor y el puerto 80 debe estar abierto (UFW).

### 10.3 Config final con HTTPS + 8 Daphne

```bash
sudo cp deploy/nginx/panaccess-wind-scaled.conf /etc/nginx/sites-available/panaccess-wind.conf
sudo nginx -t && sudo systemctl reload nginx
curl -sk https://backend.wind.do/health/
```

### 10.4 Renovación

Renueva los certificados según el proceso de tu CA/CDN, vuelve a copiarlos a `/etc/nginx/` y recarga Nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## Paso 10b: Arranque automático tras reboot

El **502** tras encender el servidor casi siempre es: **Nginx arriba, Daphne abajo** (`connection refused` en el log).

Las instancias `panaccess-wind@8000`…`8007` deben estar **`enabled`** en systemd (no basta con haberlas arrancado una vez a mano).

### Habilitar todo (una vez)

```bash
cd /opt/panaccess-wind
git pull
sudo chmod +x deploy/enable_boot_services.sh deploy/manage_daphne.sh
sudo deploy/enable_boot_services.sh
```

O manualmente:

```bash
sudo cp deploy/systemd/panaccess-wind@.service deploy/systemd/panaccess-wind.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable postgresql redis-server nginx
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh enable
sudo systemctl enable panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-worker-compare panaccess-celery-beat
```

### Verificar que quedará activo al boot

```bash
systemctl is-enabled postgresql redis-server nginx panaccess-wind.target
systemctl is-enabled panaccess-wind@{8000..8007}.service
systemctl is-enabled panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-worker-compare panaccess-celery-beat
```

Todos deben responder **`enabled`**.

### Si tras reboot sigue el 502 (levantar ahora)

```bash
sudo systemctl start postgresql redis-server
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh start
sudo systemctl start panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-worker-compare panaccess-celery-beat
curl -s http://127.0.0.1:8000/health/
```

Espera 10–30 s tras el boot: PostgreSQL/Redis pueden tardar; Daphne tiene `Restart=always` y reintenta.

---

## Paso 11: Comandos de Diagnóstico y Mantenimiento

Hay **dos flujos** de actualización en producción. Úsalos según el caso:

| Flujo | Cuándo | Downtime | Script |
|-------|--------|----------|--------|
| **[Deploy normal](#deploy-normal-recomendado)** | `git pull`, migraciones, deploy rutinario | Breve (segundos) | `deploy/refresh_stack.sh` |
| **[Reset duro](#reset-duro-troubleshooting)** | 502 persistente, servicios colgados, estado raro tras cambio de infra | Total (30–60 s) | `deploy/reset_stack.sh` |

> **No ejecutes `makemigrations` en producción.** Las migraciones se generan en desarrollo, se commitean al repo y en el servidor solo corres `migrate`.

#### ¿Reset duro: bueno o malo?

**No es malo**, pero **no es el procedimiento por defecto**.

- **Bueno para:** depurar problemas (workers zombie, conexiones PostgreSQL colgadas, Redis en estado inconsistente, Nginx sirviendo mientras Daphne está caído), tras cambios en `postgresql.conf` / `redis.conf`, o cuando un `restart` normal no resuelve el síntoma.
- **Malo como rutina:** cada deploy corta API, WebSockets y colas Celery durante el `stop` + arranque; las tareas en vuelo en Redis pueden perderse; el downtime es mayor que un simple `restart`.

**Regla práctica:** deploy diario → **deploy normal**. Si algo sigue roto después → **reset duro**.

---

### Deploy normal (recomendado)

Usa este bloque tras `git pull`, cambios de dependencias o migraciones en el día a día.

**Perfil escalado (8 Daphne, producción Wind):**

```bash
cd /opt/panaccess-wind
source env/bin/activate

# --- 1. Código y Django ---
git pull
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check

# --- 2. Reinicio de aplicación (sin parar PostgreSQL/Redis) ---
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart
sudo deploy/manage_services.sh restart
sudo nginx -t && sudo systemctl reload nginx

# --- 3. Verificación rápida ---
sudo systemctl is-active postgresql redis-server nginx
redis-cli ping
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
sudo deploy/manage_services.sh status
curl -sk https://backend.wind.do/ready/ | python3 -m json.tool
curl -sk https://backend.wind.do/health/ | python3 -m json.tool
```

**Perfil mínimo (1 instancia Daphne, `panaccess-wind.service`):**

```bash
cd /opt/panaccess-wind
source env/bin/activate
git pull
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check

sudo systemctl restart panaccess-wind.service
sudo deploy/manage_services.sh restart
sudo nginx -t && sudo systemctl reload nginx

sudo systemctl is-active postgresql redis-server nginx panaccess-wind
redis-cli ping
curl -sk https://backend.wind.do/health/ | python3 -m json.tool
```

#### Script: `deploy/refresh_stack.sh` (deploy normal)

```bash
cd /opt/panaccess-wind
sudo chmod +x deploy/refresh_stack.sh
DAPHNE_INSTANCES=8 ./deploy/refresh_stack.sh
```

---

### Reset duro (troubleshooting)

Para **último recurso** o cuando el deploy normal no corrige el problema. Detiene todo el stack, espera a que los procesos liberen puertos/conexiones y arranca de cero.

**Perfil escalado (8 Daphne):**

```bash
cd /opt/panaccess-wind
source env/bin/activate

# --- 1. Código y Django (opcional: omitir si solo reinicias infra) ---
git pull
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check

# --- 2. Detención completa ---
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh stop
sudo deploy/manage_services.sh stop
sudo systemctl stop nginx
sudo systemctl stop redis-server postgresql
sleep 3

# --- 3. Arranque en orden (infra → app → proxy) ---
sudo systemctl start postgresql redis-server
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh start
sudo deploy/manage_services.sh start
sudo nginx -t && sudo systemctl start nginx

# --- 4. Verificación ---
sudo systemctl is-active postgresql redis-server nginx
redis-cli ping
sudo -u postgres psql -d wind_db -c "SELECT 1;"
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
sudo deploy/manage_services.sh status
sudo ss -tlnp | grep -E '800[0-7]'
curl -sk https://backend.wind.do/ready/ | python3 -m json.tool
curl -sk https://backend.wind.do/health/ | python3 -m json.tool
```

> Tras `systemctl stop nginx`, usa **`start`** (no `reload`): `reload` solo funciona si Nginx ya está en ejecución.

#### Script: `deploy/reset_stack.sh` (reset duro)

```bash
cd /opt/panaccess-wind
sudo chmod +x deploy/reset_stack.sh
DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
```

Para reset **sin** `git pull` ni migraciones (solo reiniciar servicios):

```bash
SKIP_DJANGO=1 DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
```

---

**Qué reiniciar según el cambio:**

| Cambio | Flujo recomendado |
|--------|-------------------|
| Código Python, migraciones, `.env` de la app | Deploy normal |
| Solo `collectstatic` o plantilla nginx | `sudo nginx -t && sudo systemctl reload nginx` |
| Config PostgreSQL / Redis | Reset duro (o stop app → restart infra → start app) |
| 502, workers colgados, estado inconsistente | Reset duro |
| Solo reiniciar sin actualizar código | [Reinicio rápido](#reinicio-rápido-sin-actualizar-código) |

> Los templates HTML (`.html`) no requieren `collectstatic`. En producción (`DEBUG=False`), reinicia Daphne si no ves cambios tras guardar.

### Logs en tiempo real

```bash
sudo journalctl -u panaccess-wind@8000.service -f
sudo journalctl -u panaccess-celery-worker-pipeline.service -f
sudo journalctl -u panaccess-celery-worker-full.service -f
sudo journalctl -u panaccess-celery-worker-compare.service -f
sudo journalctl -u panaccess-celery-beat.service -f
sudo tail -f /var/log/nginx/error.log
```

### Reinicio rápido (sin actualizar código)

**Perfil escalado:**

```bash
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh restart
sudo /opt/panaccess-wind/deploy/manage_services.sh restart
sudo nginx -t && sudo systemctl reload nginx
DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh status
sudo /opt/panaccess-wind/deploy/manage_services.sh status
```

**Perfil mínimo (1 Daphne):**

```bash
sudo systemctl restart panaccess-wind.service
sudo /opt/panaccess-wind/deploy/manage_services.sh restart
sudo /opt/panaccess-wind/deploy/manage_services.sh status
```

### Actualizar la aplicación tras cambios de código

Equivalente al [deploy normal](#deploy-normal-recomendado):

```bash
DAPHNE_INSTANCES=8 ./deploy/refresh_stack.sh
```

O manualmente:

```bash
cd /opt/panaccess-wind
source env/bin/activate
git pull
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart
sudo deploy/manage_services.sh restart
curl -sk https://backend.wind.do/ready/
```

### Monitoreo básico

```bash
free -h && df -h
sudo ss -tlnp | grep -E '(443|8000|5432|6379)'
ps aux --sort=-%mem | head -10
```

---

## Paso 12: Verificación Post-Despliegue

Ejecuta este bloque tras completar los pasos 1–10 o tras un [deploy normal](#deploy-normal-recomendado) / [reset duro](#reset-duro-troubleshooting).

### Perfil escalado (8 Daphne — producción Wind)

```bash
echo "=== Infraestructura ==="
sudo systemctl is-active postgresql redis-server nginx

echo "=== PostgreSQL ==="
sudo -u postgres psql -d wind_db -c "SELECT current_database(), current_user;"

echo "=== Redis ==="
redis-cli ping

echo "=== Daphne (8000–8007) ==="
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
sudo ss -tlnp | grep -E '800[0-7]'

echo "=== Celery ==="
sudo deploy/manage_services.sh status

echo "=== Health (local vía Nginx) ==="
curl -sk https://localhost/ready/ | python3 -m json.tool
curl -sk https://localhost/health/ | python3 -m json.tool

echo "=== Health (dominio público) ==="
curl -sk https://backend.wind.do/ready/ | python3 -m json.tool
curl -sk https://backend.wind.do/health/ | python3 -m json.tool

echo "=== Django ==="
cd /opt/panaccess-wind && source env/bin/activate
python manage.py check
python manage.py showmigrations --plan | tail -20
```

### Perfil mínimo (1 Daphne)

```bash
echo "=== Servicios ==="
for svc in postgresql redis-server nginx panaccess-wind panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-worker-compare panaccess-celery-beat; do
    echo "--- $svc ---"
    sudo systemctl is-active "$svc" 2>/dev/null || sudo systemctl is-active "${svc}.service"
done

echo "=== Redis ==="
redis-cli ping

echo "=== Puertos ==="
sudo ss -tlnp | grep -E '(443|8000|5432|6379)'

echo "=== Health ==="
curl -sk https://localhost/health/ | python3 -m json.tool
curl -sk https://localhost/ready/ | python3 -m json.tool
```

### Respuestas esperadas

| Comprobación | Resultado OK |
|--------------|--------------|
| `redis-cli ping` | `PONG` |
| `psql ... SELECT 1` | Una fila con `1` |
| `/ready/` | `{"ready": true}` |
| `/health/` | `{"healthy": true, "checks": {"database": "ok", "cache": "ok", "panaccess": "ok"}}` |
| `systemctl is-active` | `active` |
| Daphne `status` | `active (running)` en cada puerto |

Desde tu máquina local:

```bash
curl -sk https://backend.wind.do/health/ | python3 -m json.tool
```

Probar WebSocket (emparejamiento Smart TV):

```bash
sudo apt install -y websocat
websocat -k wss://backend.wind.do/ws/auth/
```

---

## Paso 13: Solución de Problemas

### Error 502 Bad Gateway (Nginx)

Daphne no está escuchando o falló al arrancar.

**Perfil escalado:**

```bash
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
sudo journalctl -u panaccess-wind@8000.service -n 50
sudo ss -tlnp | grep -E '800[0-7]'
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart
curl -s http://127.0.0.1:8000/health/
```

**Perfil mínimo:**

```bash
sudo systemctl status panaccess-wind.service
sudo journalctl -u panaccess-wind.service -n 50
sudo ss -tlnp | grep 8000
sudo systemctl restart panaccess-wind.service
```

### Permisos PostgreSQL (`permiso denegado` / `debe ser dueño de la tabla`)

Suele ocurrir si `wind_user` no tiene permisos en `wind_db` o las tablas pertenecen a `postgres`:

```bash
sudo -u postgres psql -d wind_db
```

```sql
GRANT ALL ON SCHEMA public TO wind_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO wind_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO wind_user;
ALTER DATABASE wind_db OWNER TO wind_user;
ALTER SCHEMA public OWNER TO wind_user;
```

Si `migrate` sigue fallando al alterar tablas existentes, ejecuta el bloque `DO $$ ... $$` del [Paso 3](#paso-3-instalación-y-configuración-de-postgresql) (paso 6). Luego:

```bash
cd /opt/panaccess-wind && source env/bin/activate
python manage.py migrate
```

### Celery no procesa tareas

Beat encola pero el worker no consume (cola incorrecta o servicio caído):

```bash
sudo systemctl status panaccess-celery-worker-pipeline.service
sudo systemctl status panaccess-celery-worker-full.service
sudo systemctl status panaccess-celery-worker-compare.service
redis-cli ping
sudo journalctl -u panaccess-celery-worker-pipeline.service -n 30
```

Verifica en `.env`: `CELERY_SYNC_PIPELINE_QUEUE=sync_pipeline` y `CELERY_FULL_SYNC_QUEUE=full_sync`.

### WebSocket no conecta

```bash
sudo nginx -t
sudo tail -20 /var/log/nginx/error.log
curl -i -k https://localhost/ws/auth/ -H "Upgrade: websocket" -H "Connection: Upgrade"
```

Confirma que Nginx tiene el bloque `location /ws/` con headers `Upgrade` y timeouts largos.

### Error SSL en Nginx ("cannot load certificate")

Los certificados aún no existen en `/etc/nginx/`. Copia `cdn1.wind.do.crt` y `cdn1.wind.do.key` (Paso 10) o usa temporalmente la plantilla HTTP bootstrap.

### ModuleNotFoundError tras actualizar

```bash
cd /opt/panaccess-wind && source env/bin/activate
pip install -r requirements.txt
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart
sudo deploy/manage_services.sh restart
```

### Reinicio completo (último recurso)

Usa el [reset duro](#reset-duro-troubleshooting) o el script:

```bash
DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
```

Solo reinicio de servicios (sin `git pull` ni migraciones):

```bash
SKIP_DJANGO=1 DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
```

---

## Paso 14: Checklist Final

- [ ] PostgreSQL y Redis activos (`systemctl is-active`)
- [ ] `.env` con `DEBUG=False`, `SECRET_KEY` fuerte y credenciales PanAccess
- [ ] `python manage.py migrate` y `collectstatic` ejecutados
- [ ] `python manage.py run_full_sync` completado antes de activar Beat
- [ ] Daphne: 8 instancias activas (`8000–8007`) **o** 1 instancia en despliegues pequeños
- [ ] Nginx `upstream` apunta a todas las instancias Daphne en uso
- [ ] Workers pipeline y full_sync activos (`-c 1` cada uno)
- [ ] Celery Beat activo solo tras warm-up inicial
- [ ] Nginx con SSL, `/health/`, `/ready/` y `/ws/` configurados
- [ ] Rutas `/admin/` y sync restringidas a VPN/localhost
- [ ] `curl https://backend.wind.do/health/` responde `"healthy": true`
