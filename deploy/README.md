# Deploy — PanAccess Wind (producción)

| Parámetro | Valor |
|-----------|--------|
| Dominio API | `https://backend.wind.do` |
| Ruta instalación | `/opt/panaccess-wind` |
| Usuario servicio | `wind` |
| Admin SSH | `sw4` |
| Daphne (32 GB / 16 cores) | 8 instancias, puertos `8000`–`8007` |

## Aplicar plantillas en el servidor (perfil escalado)

Este servidor corre el perfil **escalado** (8 instancias, ver tabla arriba) —
`panaccess-wind.service` (instancia única, puerto 8000 fijo) y
`panaccess-wind.target` (8 instancias vía `panaccess-wind@.service`, puertos
8000-8007) **no pueden estar habilitados los dos a la vez**: compiten por el
mismo puerto 8000. Antes esto dependía de acordarse de correr el
`systemctl disable --now panaccess-wind.service` de abajo; ahora ambas
unidades ya declaran `Conflicts=` entre sí, así que si por error se arranca
una estando la otra activa, systemd para la otra automáticamente en vez de
que ambas intenten escuchar en el mismo puerto. El paso de abajo se
mantiene igual (es la forma explícita/documentada de hacerlo), el
`Conflicts=` es solo una red de seguridad adicional.

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
