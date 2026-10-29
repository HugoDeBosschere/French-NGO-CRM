/* Cases à cocher du TODO et de Fait, sans recharger la page.
 *
 * Chaque case est un formulaire [data-todo-tick] qui, sans JavaScript, poste
 * et recharge la page. Ici, l'élément quitte la liste tout de suite et la
 * demande part en arrière-plan : on garde sa place dans une longue liste.
 * Si le serveur ne confirme pas (connexion coupée, session expirée), l'élément
 * revient où il était, avec le message : on ne fait jamais croire qu'une case
 * est cochée alors qu'elle ne l'est pas.
 *
 * Les compteurs [data-todo-n] suivent la liste [data-todo-list] de même nom ;
 * ceux des sections filtrées sont recalculés par form-masks.js, prévenu par
 * l'évènement « todo:changed ».
 */
(function () {
  if (!window.fetch || !window.FormData) return;

  function bump(list, delta) {
    var key = list && list.getAttribute("data-todo-list");
    if (!key) return;
    document.querySelectorAll('[data-todo-n="' + key + '"]').forEach(function (el) {
      el.textContent = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
    });
  }

  function changed() {
    document.dispatchEvent(new Event("todo:changed"));
  }

  document.querySelectorAll("form[data-todo-tick]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var item = form.closest("li");
      var list = item.parentNode;
      var next = item.nextSibling;
      var old = item.querySelector(".todo-tick-error");
      if (old) old.remove();

      list.removeChild(item);
      bump(list, -1);
      changed();

      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      }).then(function (res) {
        // Une session expirée renvoie la page de connexion, pas du JSON.
        var json = (res.headers.get("Content-Type") || "").indexOf("json") !== -1;
        if (!res.ok || !json) throw new Error("refusé");
        if (!list.querySelector("li") && list.getAttribute("data-empty")) {
          var p = document.createElement("p");
          p.className = "muted";
          p.textContent = list.getAttribute("data-empty");
          list.replaceWith(p);
        }
      }).catch(function () {
        list.insertBefore(item, next);
        bump(list, 1);
        changed();
        var msg = document.createElement("p");
        msg.className = "warn todo-tick-error";
        msg.textContent = "Non enregistré : la connexion a échoué ou la session " +
          "a expiré. Rechargez la page, puis réessayez.";
        (item.querySelector(".todo-body") || item).appendChild(msg);
      });
    });
  });
})();
