# Deploy — PanAccess Wind (producción)

| Parámetro | Valor |
|-----------|--------|
| Dominio API | `https://backend.wind.do` |
| Ruta instalación | `/opt/panaccess-wind` |
| Usuario servicio | `wind` |
| Admin SSH | `sw4` |
| Daphne (32 GB / 16 cores) | 8 instancias, puertos `8000`–`8007` |

## Aplicar plantillas en el servidor (perfil escalado)

```bash
cd /opt/panaccess-wind
git pull

# Systemd — Daphne escalado + target
sudo cp deploy/systemd/panaccess-wind@.service /etc/systemd/system/
sudo cp deploy/systemd/panaccess-wind.target /etc/systemd/system/
sudo cp deploy/systemd/panaccess-celery-*.service /etc/systemd/system/
sudo systemctl disable --now panaccess-wind.service 2>/dev/null || true
sudo systemctl daemon-reload
sudo chmod +x deploy/manage_daphne.sh deploy/manage_services.sh

DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh enable
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh restart

# Nginx — backend.wind.do + upstream 8 puertos
sudo cp /etc/nginx/sites-available/panaccess-wind.conf \
        /etc/nginx/sites-available/panaccess-wind.conf.bak.$(date +%F) 2>/dev/null || true
sudo cp deploy/nginx/panaccess-wind-scaled.conf /etc/nginx/sites-available/panaccess-wind.conf
sudo ls -l /etc/nginx/cdn1.wind.do.crt /etc/nginx/cdn1.wind.do.key || {
  echo "Sin certificados — copiar a /etc/nginx/ o usar bootstrap HTTP (deploy/nginx/panaccess-wind-bootstrap-http.conf)"
  sudo cp deploy/nginx/panaccess-wind-bootstrap-http.conf /etc/nginx/sites-available/panaccess-wind.conf
  sudo nginx -t && sudo systemctl reload nginx
  exit 1
}
sudo nginx -t && sudo systemctl reload nginx

# Verificación
curl -sk https://backend.wind.do/health/
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
```

### Deploy normal (día a día)

```bash
sudo chmod +x deploy/refresh_stack.sh
DAPHNE_INSTANCES=8 ./deploy/refresh_stack.sh
```

### Reset duro (troubleshooting)

```bash
sudo chmod +x deploy/reset_stack.sh
DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
# Solo reiniciar servicios, sin git/migrate:
SKIP_DJANGO=1 DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
```

Guía completa: [`docs/DEPLOYMENT_UBUNTU_NATIVE.md`](../docs/DEPLOYMENT_UBUNTU_NATIVE.md) — Paso 11.
