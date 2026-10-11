/* Rend utilisables les lignes de tableau cliquables.
 *
 * Dix listes utilisent `<tr onclick="window.location='…'">`. Ça marche à la
 * souris et seulement à la souris : impossible d'ouvrir un fil dans un nouvel
 * onglet (clic du milieu, Ctrl+clic), impossible d'y arriver au clavier, et un
 * lecteur d'écran n'y voit rien d'activable.
 *
 * Plutôt que de réécrire les dix gabarits, on réparé ici, une fois : l'URL est
 * lue dans l'attribut `onclick` déjà présent, et la ligne devient un lien à
 * part entière.
 */
(function () {
  var URL_IN_ONCLICK = /window\.location\s*=\s*'([^']+)'/;

  function target(row) {
    var code = row.getAttribute("onclick") || "";
    var found = URL_IN_ONCLICK.exec(code);
    return found ? found[1] : null;
  }

  var rows = document.querySelectorAll("tr[onclick]");
  Array.prototype.forEach.call(rows, function (row) {
    var url = target(row);
    if (!url) return;

    row.setAttribute("role", "link");
    row.setAttribute("tabindex", "0");

    // Entrée active la ligne, comme un lien. Espace est laissé au défilement.
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        window.location = url;
      }
    });

    // Ctrl+clic, Cmd+clic et clic du milieu ouvrent dans un nouvel onglet —
    // ce qu'on attend de n'importe quelle liste : garder la liste ouverte et
    // regarder une fiche à côté.
    row.addEventListener("click", function (event) {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
        window.open(url, "_blank", "noopener");
      }
    }, true);
    row.addEventListener("auxclick", function (event) {
      if (event.button === 1) {
        event.preventDefault();
        window.open(url, "_blank", "noopener");
      }
    });
  });
})();

/* Dire qu'un tableau défile, quand il défile.
 *
 * `.table-wrap` défile latéralement depuis longtemps, et c'est ce qui empêche
 * une page entière de partir de travers. Mais rien ne l'annonçait : sur
 * /organisations, neuf colonnes dans une carte trop étroite, la dernière était
 * simplement coupée au bord et personne ne pouvait deviner qu'il suffisait de
 * faire glisser. Une ombre au bord concerné — et elle disparaît quand on est
 * arrivé au bout, sinon elle ment.
 *
 * L'ombre est un élément posé ici, pas un `::before` du conteneur : un
 * pseudo-élément à l'intérieur d'une zone qui défile défile avec elle, et s'il
 * est `sticky` sa hauteur en pourcentage ne se résout pas (mesuré : 0 px). On
 * enveloppe donc chaque tableau dans un bloc qui, lui, ne bouge pas, et les
 * deux ombres s'y accrochent. Rien à changer dans les treize gabarits.
 */
(function () {
  function etat(wrap) {
    var reste = wrap.scrollWidth - wrap.clientWidth;
    var zone = wrap.parentNode;
    // 1 px de marge : les largeurs fractionnaires rendent l'égalité stricte
    // fausse à l'arrivée, et l'ombre restait allumée au bout de la course.
    zone.classList.toggle("defile-droite", reste - wrap.scrollLeft > 1);
    zone.classList.toggle("defile-gauche", wrap.scrollLeft > 1);
  }

  function envelopper(wrap) {
    if (wrap.parentNode && wrap.parentNode.classList.contains("table-zone")) {
      return wrap.parentNode;
    }
    var zone = document.createElement("div");
    zone.className = "table-zone";
    wrap.parentNode.insertBefore(zone, wrap);
    zone.appendChild(wrap);
    zone.insertAdjacentHTML("beforeend",
      '<span class="table-ombre table-ombre-g" aria-hidden="true"></span>' +
      '<span class="table-ombre table-ombre-d" aria-hidden="true"></span>');
    return zone;
  }

  function brancher() {
    document.querySelectorAll(".table-wrap").forEach(function (wrap) {
      envelopper(wrap);
      etat(wrap);
      wrap.addEventListener("scroll", function () { etat(wrap); }, { passive: true });
      // La largeur change sans que la page bouge : fenêtre redimensionnée, mais
      // aussi une colonne masquée par un filtre ou une police chargée en retard.
      if (window.ResizeObserver) {
        new ResizeObserver(function () { etat(wrap); }).observe(wrap);
      }
    });
  }

  window.addEventListener("resize", function () {
    document.querySelectorAll(".table-wrap").forEach(etat);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", brancher);
  } else {
    brancher();
  }
})();

/* Un portrait qui ne charge pas s'efface, plutôt que de laisser l'icône
 * d'image brisée du navigateur. Ces URL viennent de l'Assemblée et du Sénat :
 * elles bougent, et une liste de cinq cents lignes en afficherait cinq cents.
 *
 * Un seul écouteur en capture pour toute la page, et non un `onerror=` sur
 * chaque balise : sur /echanges, cinq cents attributs identiques pesaient à eux
 * seuls une bonne part du poids de la page. Le `error` d'une image ne remonte
 * pas, d'où la capture.
 */
document.addEventListener("error", function (e) {
  var el = e.target;
  if (el && el.tagName === "IMG" && /\bportrait/.test(el.className)) {
    // Sur la fiche d'une personne, le bloc se réduit au seul bouton.
    var bloc = el.closest(".fiche-officielle");
    if (bloc) { bloc.classList.add("sans-portrait"); }
    el.remove();
  }
}, true);
