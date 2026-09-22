# Comandos para completar la cadena SSL de `backend.wind.do` (Bajo #41)

Fecha: 2026-09-22
Referencia: `AUDITORIA_CONSOLIDADA_2026-08-24.md`, hallazgo Bajo #41.

## Qué está mal

`/etc/nginx/cdn1.wind.do.crt` (el archivo que `sites-enabled/panaccess-wind.conf` usa como `ssl_certificate`) tiene el certificado hoja (`*.wind.do`, GoDaddy) **pegado dos veces**, en vez de hoja + intermedio (`Go Daddy Secure Certificate Authority - G2`). El intermedio correcto ya existe en el servidor, sin usar, en `/etc/nginx/gd-g2_iis_intermediates.p7b` (formato PKCS#7 -- hay que convertirlo a PEM antes de poder concatenarlo).

Efecto práctico: los navegadores no lo notan (cachean/auto-completan el intermedio con lo que ya conocen), pero clientes con validación TLS estricta -- `curl` sin `-k`, algunos stacks TLS de apps móviles/TV -- pueden rechazar la conexión con `unable to get local issuer certificate`.

**Nota:** esto es un problema distinto al certificado vencido que se renovó esta semana (2026-09-18). Ese fix resolvió la fecha de expiración; este resuelve que la cadena esté completa. Si el certificado se re-emitió/re-pegó manualmente durante esa renovación, es probable que el mismo error de "pegar la hoja dos veces" se haya repetido -- por eso el paso 0 de abajo revisa el estado actual antes de asumir nada.

Como con cualquier cambio de nginx/SSL, estos comandos son **para que los corras vos mismo** en el servidor -- no los voy a ejecutar yo.

## Comandos (correr como el usuario con sudo, en el servidor)

```bash
# 0. Diagnóstico: ¿cuántos bloques de certificado hay hoy, y son iguales entre sí?
grep -c "BEGIN CERTIFICATE" /etc/nginx/cdn1.wind.do.crt
openssl x509 -in /etc/nginx/cdn1.wind.do.crt -noout -subject -issuer

# Si el resultado de arriba ya muestra "issuer" = Go Daddy Secure Certificate
# Authority - G2 (no *.wind.do), la cadena ya está bien y no hace falta seguir.
# Si "issuer" sigue mostrando *.wind.do (el mismo que "subject"), el problema
# sigue vigente -- continuar con los pasos siguientes.

# 1. Backup del archivo actual antes de tocar nada
sudo cp /etc/nginx/cdn1.wind.do.crt /etc/nginx/cdn1.wind.do.crt.bak-$(date +%Y%m%d)

# 2. Convertir el intermedio de GoDaddy (PKCS#7) a PEM
openssl pkcs7 -print_certs -in /etc/nginx/gd-g2_iis_intermediates.p7b -out /tmp/gd-g2_intermediates.pem

# 3. Extraer SOLO el primer bloque del archivo actual (la hoja real, *.wind.do)
awk '/BEGIN CERTIFICATE/{n++} n==1' /etc/nginx/cdn1.wind.do.crt > /tmp/leaf.pem

# 4. Armar la cadena correcta: hoja primero, intermedio(s) después
cat /tmp/leaf.pem /tmp/gd-g2_intermediates.pem | sudo tee /etc/nginx/cdn1.wind.do.crt > /dev/null

# 5. Verificación antes de recargar nginx: los dos bloques deben ser DISTINTOS
#    (si "diff" no imprime nada, la hoja y el intermedio quedaron iguales --
#    algo salió mal, no seguir, restaurar el backup del paso 1)
diff <(awk '/BEGIN CERTIFICATE/{n++} n==1' /etc/nginx/cdn1.wind.do.crt) \
     <(awk '/BEGIN CERTIFICATE/{n++} n==2' /etc/nginx/cdn1.wind.do.crt)

openssl x509 -in <(awk '/BEGIN CERTIFICATE/{n++} n==2' /etc/nginx/cdn1.wind.do.crt) -noout -subject
# ^ debería mostrar "Go Daddy Secure Certificate Authority - G2", no *.wind.do

# 6. Validar la config de nginx ANTES de recargar el proceso real
sudo nginx -t

# 7. Solo si el paso 6 dice "syntax is ok" / "test is successful":
sudo systemctl reload nginx

# 8. Confirmar desde afuera que la cadena ya completa (sin -k, sin bypass)
curl -vI https://backend.wind.do 2>&1 | grep -iE "issuer|subject|SSL certificate|unable to get"
```

## Si algo sale mal

Restaurar el backup y recargar:

```bash
sudo cp /etc/nginx/cdn1.wind.do.crt.bak-$(date +%Y%m%d) /etc/nginx/cdn1.wind.do.crt
sudo nginx -t && sudo systemctl reload nginx
```

## Verificación pendiente después de aplicar

- Confirmar con `curl -vI https://backend.wind.do` (sin `-k`) que ya no aparece `unable to get local issuer certificate`.
- Si alguna app de TV/móvil tenía workarounds para este error (bypass de validación TLS, `-k` en algún script interno), se pueden retirar una vez confirmado.
