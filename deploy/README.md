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
sudo ls /etc/letsencrypt/live/backend.wind.do/ || {
  echo "Sin certificados — bootstrap HTTP + Certbot (ver deploy/nginx/panaccess-wind-bootstrap-http.conf)"
  sudo apt install -y certbot python3-certbot-nginx
  sudo cp deploy/nginx/panaccess-wind-bootstrap-http.conf /etc/nginx/sites-available/panaccess-wind.conf
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot --nginx -d backend.wind.do
  sudo cp deploy/nginx/panaccess-wind-scaled.conf /etc/nginx/sites-available/panaccess-wind.conf
}
sudo nginx -t && sudo systemctl reload nginx

# Verificación
curl -sk https://backend.wind.do/health/
DAPHNE_INSTANCES=8 sudo deploy/manage_daphne.sh status
```

Guía completa: [`docs/DEPLOYMENT_UBUNTU_NATIVE.md`](../docs/DEPLOYMENT_UBUNTU_NATIVE.md).
