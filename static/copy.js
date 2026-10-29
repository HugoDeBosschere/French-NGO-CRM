/* Bouton « copier » : tout élément [data-copy] copie sa valeur dans le
 * presse-papiers.
 *
 * Les boutons sont rendus `hidden` et ce script seul les révèle, et seulement
 * si le navigateur offre le presse-papiers (il l'exige en HTTPS ou sur
 * localhost). Sans lui, l'adresse reste sélectionnable à la main comme avant.
 */
(function () {
  // Une coche le temps de confirmer : la couleur seule ne se voit pas assez.
  var CHECK = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" ' +
    'fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

  if (!navigator.clipboard || !navigator.clipboard.writeText) return;

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var label = btn.getAttribute("title") || "";
    var icon = btn.innerHTML;
    btn.hidden = false;
    btn.addEventListener("click", function (event) {
      event.preventDefault();
      navigator.clipboard.writeText(btn.getAttribute("data-copy")).then(function () {
        btn.classList.add("copied");
        btn.innerHTML = CHECK;
        btn.setAttribute("title", "Copié !");
        setTimeout(function () {
          btn.classList.remove("copied");
          btn.innerHTML = icon;
          btn.setAttribute("title", label);
        }, 1500);
      });
    });
  });
})();
