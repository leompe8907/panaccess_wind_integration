# Guía de Despliegue Nativo en Ubuntu Server (PanAccess - Wind)

Esta guía detalla el proceso paso a paso para realizar un despliegue nativo (sin Docker) de la aplicación de integración PanAccess - Wind sobre un servidor físico o virtual con **Ubuntu Server** limpio (por ejemplo, Ubuntu 22.04 LTS o 24.04 LTS).

Plantillas listas para copiar: carpeta [`deploy/`](../deploy/) (systemd, nginx, script de reinicio).

---

## Índice

1. [Arquitectura del sistema](#arquitectura-del-sistema)
2. [Requisitos del servidor](#requisitos-del-servidor)
3. [Perfil recomendado: 32 GB / 16 cores](#perfil-recomendado-32-gb--16-cores)
4. [Pasos 1–10: instalación y configuración](#paso-1-conexión-ssh-y-actualización-inicial)
5. [Paso 11: diagnóstico y mantenimiento](#paso-11-comandos-de-diagnóstico-y-mantenimiento)
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
sudo systemctl daemon-reload
sudo chmod +x deploy/manage_daphne.sh

# Arranque automático + inicio
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh enable
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh start
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
```

Verifica puertos:

```bash
sudo ss -tlnp | grep -E '800[0-7]'
```

Si tras monitorizar (`htop`, `journalctl`) la CPU de Daphne sigue alta con muchas TVs conectadas, sube a **12 instancias** (puertos 8000–8011) y añade esos servidores al `upstream` de Nginx. Con 32 GB no conviene pasar de ~12 instancias (~6 GB solo en Daphne).

### 4. Nginx — upstream con balanceo

```bash
sudo cp deploy/nginx/panaccess-wind-scaled.conf /etc/nginx/sites-available/panaccess-wind.conf
sudo nano /etc/nginx/sites-available/panaccess-wind.conf   # dominio + SSL
sudo ln -sf /etc/nginx/sites-available/panaccess-wind.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 5. Celery — sin cambios

Mantén `-c 1` en pipeline y full_sync. La concurrencia hacia PanAccess se controla por `.env`:

```env
PANACCESS_SMARTCARD_SUBSCRIBER_CONCURRENCY=4
```

Sube ese valor con cuidado si PanAccess tolera más llamadas paralelas; no aumentes `-c` del worker Celery.

### 6. Reinicio tras deploy (perfil escalado)

```bash
cd /opt/panaccess-wind
git pull && source env/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart
sudo systemctl restart panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-beat
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
    sudo apt install -y git python3-pip python3-venv python3-dev build-essential libpq-dev curl certbot python3-certbot-nginx ufw
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
4.  **Crea la base de datos y el usuario con privilegios:**
    Ejecuta las siguientes consultas SQL dentro del prompt (`psql`):
    ```sql
    CREATE DATABASE wind_db;
    CREATE USER wind_user WITH PASSWORD 'parana771';
    ALTER ROLE wind_user SET client_encoding TO 'utf8';
    ALTER ROLE wind_user SET default_transaction_isolation TO 'read committed';
    ALTER ROLE wind_user SET timezone TO 'UTC';
    GRANT ALL PRIVILEGES ON DATABASE wind_db TO wind_user;
    \q
    ```

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
ALLOWED_HOSTS=api.tudominio.com,localhost,127.0.0.1

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
EMAIL_FROM_ADDRESS=soporte@tudominio.com

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
# Edita User= en cada archivo si no usas "ubuntu"
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
User=ubuntu  # Reemplaza por tu usuario real de Ubuntu
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/daphne -b 127.0.0.1 -p 8000 panaccess_wind_integration.asgi:application
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 2. Workers de Celery (pipeline + full sync)

Se usan **dos workers** con colas separadas:

| Worker | Cola | Rol |
|--------|------|-----|
| `panaccess-celery-worker-pipeline` | `sync_pipeline` | Pipeline periódico: suscriptores → smartcards (serie, `-c 1`) |
| `panaccess-celery-worker-full` | `full_sync` | Full sync nocturno exclusivo (`-c 1`) |

Variables en `.env`:

```env
CELERY_SYNC_PIPELINE_QUEUE=sync_pipeline
CELERY_FULL_SYNC_QUEUE=full_sync
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
User=ubuntu
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
User=ubuntu
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/celery -A panaccess_wind_integration worker -Q full_sync -c 1 --loglevel=info -n fullsync@%h
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Opcional: worker general para emails y tareas sueltas (`-Q celery`).

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
User=ubuntu  # Reemplaza por tu usuario real
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
sudo systemctl enable --now panaccess-wind.service panaccess-celery-worker-pipeline.service panaccess-celery-worker-full.service
# Tras confirmar run_full_sync OK:
sudo systemctl enable --now panaccess-celery-beat.service
```
Verifica que estén activos sin errores:
```bash
sudo systemctl status panaccess-wind.service
sudo systemctl status panaccess-celery-worker-pipeline.service
sudo systemctl status panaccess-celery-worker-full.service
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
    server_name api.tudominio.com; # Cambia por tu dominio real
    
    # Redirección permanente a HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name api.tudominio.com; # Cambia por tu dominio real

    # Rutas de los certificados SSL (generados por Let's Encrypt en el paso 10)
    ssl_certificate     /etc/letsencrypt/live/api.tudominio.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.tudominio.com/privkey.pem;
    
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

## Paso 10: Obtención de Certificado SSL Seguro (Let's Encrypt)

Certbot configurará automáticamente los certificados SSL y los inyectará en la configuración de Nginx:

1.  **Ejecuta Certbot para tu dominio:**
    ```bash
    sudo certbot --nginx -d api.tudominio.com
    ```
2.  **Verificación de Renovación Automática:**
    ```bash
    sudo certbot renew --dry-run
    ```

---

## Paso 11: Comandos de Diagnóstico y Mantenimiento

### Logs en tiempo real

```bash
sudo journalctl -u panaccess-wind.service -f
sudo journalctl -u panaccess-celery-worker-pipeline.service -f
sudo journalctl -u panaccess-celery-worker-full.service -f
sudo journalctl -u panaccess-celery-beat.service -f
sudo tail -f /var/log/nginx/error.log
```

### Reinicio rápido de servicios de aplicación

```bash
sudo /opt/panaccess-wind/deploy/manage_services.sh restart
sudo /opt/panaccess-wind/deploy/manage_services.sh status
```

> Tras `git pull`, reinicia **solo** servicios que ejecutan código Python (Daphne + Celery). Nginx, PostgreSQL y Redis no suelen necesitarlo.

### Actualizar la aplicación tras cambios de código

```bash
cd /opt/panaccess-wind
git pull
source env/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo deploy/manage_services.sh restart
```

### Monitoreo básico

```bash
free -h && df -h
sudo ss -tlnp | grep -E '(443|8000|5432|6379)'
ps aux --sort=-%mem | head -10
```

---

## Paso 12: Verificación Post-Despliegue

Ejecuta este bloque tras completar los pasos 1–10:

```bash
echo "=== Servicios ==="
for svc in postgresql redis-server nginx panaccess-wind panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-beat; do
    echo "--- $svc ---"
    sudo systemctl is-active "$svc" 2>/dev/null || sudo systemctl is-active "${svc}.service"
done

echo "=== Redis ==="
redis-cli ping

echo "=== Puertos ==="
sudo ss -tlnp | grep -E '(443|8000|5432|6379)'

echo "=== Health ==="
curl -sk https://localhost/health/
curl -sk https://localhost/ready/
```

Desde tu máquina local (sustituye el dominio):

```bash
curl -s https://api.tudominio.com/health/
```

Probar WebSocket (emparejamiento Smart TV):

```bash
sudo apt install -y websocat
websocat -k wss://api.tudominio.com/ws/auth/
```

Respuesta esperada de `/health/`: JSON con `"healthy": true` y checks de DB, caché y sesión PanAccess.

---

## Paso 13: Solución de Problemas

### Error 502 Bad Gateway (Nginx)

Daphne no está escuchando o falló al arrancar:

```bash
sudo systemctl status panaccess-wind.service
sudo journalctl -u panaccess-wind.service -n 50
sudo ss -tlnp | grep 8000
sudo deploy/manage_services.sh restart
```

### Celery no procesa tareas

Beat encola pero el worker no consume (cola incorrecta o servicio caído):

```bash
sudo systemctl status panaccess-celery-worker-pipeline.service
sudo systemctl status panaccess-celery-worker-full.service
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

Los certificados aún no existen. Ejecuta Certbot (Paso 10) o comenta temporalmente las líneas `ssl_certificate` y usa solo HTTP hasta obtenerlos.

### ModuleNotFoundError tras actualizar

```bash
cd /opt/panaccess-wind && source env/bin/activate
pip install -r requirements.txt
sudo deploy/manage_services.sh restart
```

### Reinicio completo (último recurso)

```bash
sudo deploy/manage_services.sh stop
sudo systemctl stop nginx
sudo systemctl stop redis-server postgresql
sleep 3
sudo systemctl start postgresql redis-server
sudo deploy/manage_services.sh start
sudo systemctl start nginx
sudo deploy/manage_services.sh status
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
- [ ] `curl https://api.tudominio.com/health/` responde `"healthy": true`
