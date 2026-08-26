// Toggle mostrar/ocultar contraseña, compartido por todas las páginas del
// portal que tengan inputs type="password".
//
// Uso: envolver el input en un contenedor con clase "app-input-wrap"
// (position: relative -- ver wind/static/wind/css/app.css) y agregar, junto
// al input, un botón:
//
//   <div class="app-input-wrap">
//     <input id="miInput" type="password" ... />
//     <button type="button" class="app-toggle-visibility" data-toggle-for="miInput"
//             aria-label="Mostrar contraseña" aria-pressed="false">
//       <svg class="icon-eye" ...>...</svg>
//       <svg class="icon-eye-off" ...>...</svg>
//     </button>
//   </div>
//
// Este script se encarga solo del comportamiento (cambiar type
// password/text); el estilo y los ícono ya están definidos en app.css
// (.app-input-wrap / .app-toggle-visibility / .icon-eye / .icon-eye-off).
(function () {
  function initPasswordToggles(root) {
    (root || document).querySelectorAll(".app-toggle-visibility").forEach(function (btn) {
      if (btn.dataset.toggleBound) return;
      var input = document.getElementById(btn.dataset.toggleFor);
      if (!input) return;
      btn.dataset.toggleBound = "1";
      btn.addEventListener("click", function () {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.classList.toggle("is-visible", show);
        btn.setAttribute("aria-pressed", String(show));
        btn.setAttribute("aria-label", show ? "Ocultar contraseña" : "Mostrar contraseña");
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initPasswordToggles();
    });
  } else {
    initPasswordToggles();
  }

  window.initPasswordToggles = initPasswordToggles;
})();
