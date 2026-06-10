# Guía de Despliegue Nativo en Ubuntu Server (PanAccess - Wind)

Esta guía detalla el proceso paso a paso para realizar un despliegue nativo (sin Docker) de la aplicación de integración PanAccess - Wind sobre un servidor físico o virtual con **Ubuntu Server** limpio (por ejemplo, Ubuntu 22.04 LTS o 24.04 LTS).

---

## Arquitectura del Sistema
El despliegue nativo se organizará utilizando las siguientes herramientas del sistema:
*   **Base de datos:** PostgreSQL corriendo como servicio de sistema (`systemd`).
*   **Caché y Broker:** Redis Server local gestionado por `systemd`.
*   **Servidor ASGI:** **Daphne** corriendo bajo `systemd` para atender peticiones HTTP y WebSockets concurrentemente en el puerto `8000`.
*   **Gestión de Procesos:** Tres servicios de `systemd` independientes (uno para la aplicación web Daphne, uno para el worker de Celery y uno para Celery Beat).
*   **Servidor Web Principal:** Nginx actuando como proxy inverso y manejando el SSL de Let's Encrypt.

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
    CREATE USER wind_user WITH PASSWORD 'CONTRASEÑA_FUERTE_POSTGRES';
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

Crearemos tres archivos de servicios en systemd para mantener la aplicación web, el worker y la agenda de tareas corriendo de forma ininterrumpida y que arranquen automáticamente si el servidor se reinicia.

### 1. Servicio de la Aplicación Web (Daphne/ASGI)
Crea el archivo `/etc/systemd/system/panaccess-wind.service`:
```bash
sudo nano /etc/systemd/system/panaccess-wind.service
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

### 2. Servicio del Worker de Celery
Crea el archivo `/etc/systemd/system/panaccess-celery-worker.service`:
```bash
sudo nano /etc/systemd/system/panaccess-celery-worker.service
```
Escribe el siguiente contenido:
```ini
[Unit]
Description=Celery Worker para PanAccess Wind
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=ubuntu  # Reemplaza por tu usuario real
WorkingDirectory=/opt/panaccess-wind
EnvironmentFile=/opt/panaccess-wind/.env
ExecStart=/opt/panaccess-wind/env/bin/celery -A panaccess_wind_integration worker --loglevel=info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

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
Recarga systemd, habilita el arranque automático e inicia los tres procesos:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now panaccess-wind.service panaccess-celery-worker.service panaccess-celery-beat.service
```
Verifica que estén activos sin errores:
```bash
sudo systemctl status panaccess-wind.service
sudo systemctl status panaccess-celery-worker.service
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
    sudo nano /etc/nginx/sites-available/panaccess-wind.conf
    ```
3.  **Pega la siguiente estructura adaptada para HTTP y WebSockets (Smart TV pairing):**

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

*   **Ver logs del servidor web (Daphne/Django) en tiempo real:**
    ```bash
    sudo journalctl -u panaccess-wind.service -f
    ```
*   **Ver logs del Celery Worker:**
    ```bash
    sudo journalctl -u panaccess-celery-worker.service -f
    ```
*   **Actualizar la aplicación tras cambios de código:**
    ```bash
    cd /opt/panaccess-wind
    git pull
    source env/bin/activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py collectstatic --noinput
    sudo systemctl restart panaccess-wind.service panaccess-celery-worker.service panaccess-celery-beat.service
    ```
