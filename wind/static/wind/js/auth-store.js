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

  global.WindAuth = {
    getAccessToken() {
      const data = read();
      return data && data.access ? data.access : null;
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

    async fetchApi(url, options) {
      const opts = options || {};
      opts.headers = Object.assign({}, this.authHeaders(true), opts.headers || {});
      const res = await fetch(url, opts);
      if (res.status === 401) {
        this.logout();
        window.location.href = "/wind/login/?session=expired";
        throw new Error("Sesión expirada");
      }
      return res;
    },

    logout() {
      localStorage.removeItem(STORAGE_KEY);
    },
  };
})(window);
