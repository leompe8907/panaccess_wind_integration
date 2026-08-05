/**
 * Almacenamiento local del JWT (misma clave para login, social y dashboard).
 */
(function (global) {
  const STORAGE_KEY = "wind_auth";

  function read() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function write(payload) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  }

  let refreshInFlight = null;

  global.WindAuth = {
    getAccessToken() {
      const data = read();
      return data && data.access ? data.access : null;
    },

    getRefreshToken() {
      const data = read();
      return data && data.refresh ? data.refresh : null;
    },

    getUser() {
      const data = read();
      return data && data.user ? data.user : null;
    },

    isLoggedIn() {
      return Boolean(this.getAccessToken());
    },

    saveFromApiResponse(data) {
      const access = data.access || data.access_token || data.key;
      const refresh = data.refresh || data.refresh_token || null;
      if (!access) {
        throw new Error("La respuesta no incluye token de acceso.");
      }
      write({
        access,
        refresh,
        user: data.user || null,
        panaccess_credentials: data.panaccess_credentials || null,
        saved_at: Date.now(),
      });
    },

    authHeaders(json) {
      const headers = {};
      if (json) {
        headers["Content-Type"] = "application/json";
      }
      const token = this.getAccessToken();
      if (token) {
        headers["Authorization"] = "Bearer " + token;
      }
      return headers;
    },

    /**
     * Renueva el access token con el refresh guardado. El backend tiene
     * ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION activados (ver
     * panaccess_wind_integration/settings.py): cada refresh exitoso invalida
     * el refresh usado y entrega uno nuevo -- hay que guardar ese valor
     * nuevo o el SIGUIENTE refresh falla (mismo bug ya corregido en
     * appVideo/deviceAuthService.js). Devuelve el access nuevo, o null si el
     * refresh no sirve (expirado/invalidado/sin backend) -- en ese caso
     * también limpia la sesión guardada. `refreshInFlight` evita disparar
     * varios refresh en paralelo si hay más de un fetch pendiente a la vez.
     */
    async refreshAccessToken() {
      if (refreshInFlight) return refreshInFlight;

      const refresh = this.getRefreshToken();
      if (!refresh) return null;

      const self = this;
      refreshInFlight = (async function () {
        try {
          const res = await fetch("/api/auth/token/refresh/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh: refresh }),
          });
          if (!res.ok) {
            self.logout();
            return null;
          }
          const data = await res.json();
          if (!data || !data.access) {
            self.logout();
            return null;
          }
          const current = read() || {};
          write({
            access: data.access,
            refresh: data.refresh || current.refresh,
            user: current.user || null,
            panaccess_credentials: current.panaccess_credentials || null,
            saved_at: Date.now(),
          });
          return data.access;
        } catch (_) {
          return null;
        }
      })();

      try {
        return await refreshInFlight;
      } finally {
        refreshInFlight = null;
      }
    },

    /**
     * Fetch autenticado con reintento automático (una sola vez) si el
     * access token venció: antes de rendirse, intenta refrescar con el
     * refresh guardado (ver refreshAccessToken) y repite la misma llamada.
     * Antes de este fix, cualquier 401 -- incluyendo un simple access token
     * vencido con un refresh todavía válido por hasta 7 días -- expulsaba
     * al usuario al login sin más ("session=expired" al volver a abrir el
     * dashboard tras un rato, o incluso dentro de la misma sesión apenas
     * venciera el access token de corta duración).
     */
    async fetchApi(url, options) {
      const opts = options || {};
      const doFetch = () => {
        const merged = Object.assign({}, opts);
        merged.headers = Object.assign({}, this.authHeaders(true), opts.headers || {});
        return fetch(url, merged);
      };

      let res = await doFetch();
      if (res.status === 401) {
        const newAccess = await this.refreshAccessToken();
        if (newAccess) {
          res = await doFetch();
        }
        if (res.status === 401) {
          this.logout();
          window.location.href = "/wind/login/?session=expired";
          throw new Error("Sesión expirada");
        }
      }
      return res;
    },

    logout() {
      localStorage.removeItem(STORAGE_KEY);
    },
  };
})(window);
