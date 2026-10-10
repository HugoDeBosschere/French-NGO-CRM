/* Sélecteur de personne à recherche, pour la file « À rattacher ».
 *
 * Le gabarit posait un <select> par ligne, chacun contenant TOUTES les fiches :
 * 1 792 personnes. Avec vingt adresses en attente, la page portait plus de
 * 35 000 <option>, et trouver quelqu'un voulait dire faire défiler une liste
 * alphabétique de milliers d'entrées. Illisible, et lourd à charger.
 *
 * Ici, la liste n'est envoyée qu'UNE FOIS, en JSON, et chaque ligne a un champ
 * de recherche qui filtre à la frappe. La recherche ignore les accents et la
 * casse, accepte les mots dans n'importe quel ordre (« bost christine » trouve
 * « Christine Bost ») et montre le type de contact pour départager les homonymes.
 *
 * Sans JavaScript, le <select> d'origine reste en place et fonctionne : c'est
 * lui que ce script remplace, une fois seulement qu'il a pu se construire.
 */
(function () {
  var source = document.getElementById("people-data");
  if (!source) return;

  var PEOPLE;
  try {
    PEOPLE = JSON.parse(source.textContent);
  } catch (e) {
    return; // données illisibles : on laisse le <select> natif en place
  }
  if (!PEOPLE.length) return;

  /* Minuscules sans accents : « Édouard » se trouve en tapant « edouard ». */
  function fold(s) {
    return (s || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();
  }

  PEOPLE.forEach(function (p) {
    p.cherche = fold(p.n + " " + (p.t || ""));
  });

  var MAX = 8; // au-delà, la liste redevient un mur : mieux vaut affiner

  function chercher(q) {
    var mots = fold(q).split(/\s+/).filter(Boolean);
    if (!mots.length) return [];
    var exacts = [];
    var autres = [];
    for (var i = 0; i < PEOPLE.length; i++) {
      var p = PEOPLE[i];
      var ok = true;
      for (var m = 0; m < mots.length; m++) {
        if (p.cherche.indexOf(mots[m]) === -1) { ok = false; break; }
      }
      if (!ok) continue;
      // Un nom qui COMMENCE par ce qu'on tape passe devant : en tapant « mar »
      // on veut Marie avant Jean-Marc.
      (fold(p.n).indexOf(mots[0]) === 0 ? exacts : autres).push(p);
      if (exacts.length >= MAX) break;
    }
    return exacts.concat(autres).slice(0, MAX);
  }

  function construire(form) {
    var champ = form.querySelector('input[name="person_nom"]');
    var cache = form.querySelector('input[name="person_id"]');
    if (!champ || !cache) return;

    // Le datalist est le repli sans JavaScript. Une fois ce script en place,
    // il ferait double emploi avec notre liste : deux menus superposés.
    champ.removeAttribute("list");
    champ.setAttribute("role", "combobox");
    champ.setAttribute("aria-expanded", "false");
    champ.setAttribute("aria-autocomplete", "list");

    var boite = document.createElement("div");
    boite.className = "picker";
    champ.parentNode.insertBefore(boite, champ);
    boite.appendChild(champ);

    var liste = document.createElement("ul");
    liste.className = "picker-list";
    liste.setAttribute("role", "listbox");
    liste.hidden = true;
    boite.appendChild(liste);

    var actif = -1;
    var visibles = [];

    function fermer() {
      liste.hidden = true;
      liste.innerHTML = "";
      champ.setAttribute("aria-expanded", "false");
      actif = -1;
      visibles = [];
    }

    function surligner(i) {
      var items = liste.querySelectorAll("li");
      for (var k = 0; k < items.length; k++) {
        items[k].classList.toggle("actif", k === i);
        items[k].setAttribute("aria-selected", k === i ? "true" : "false");
      }
      actif = i;
    }

    function choisir(p) {
      cache.value = p.i;
      champ.value = p.n;
      champ.dataset.choisi = "1";
      fermer();
    }

    function ouvrir() {
      visibles = chercher(champ.value);
      liste.innerHTML = "";
      if (!visibles.length) { fermer(); return; }
      visibles.forEach(function (p, i) {
        var li = document.createElement("li");
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.innerHTML = "";
        var nom = document.createElement("span");
        nom.className = "picker-nom";
        nom.textContent = p.n;
        var type = document.createElement("span");
        type.className = "picker-type";
        type.textContent = p.t || "";
        li.appendChild(nom);
        li.appendChild(type);
        // mousedown et non click : le clic arrive après le blur du champ,
        // qui aurait déjà refermé la liste.
        li.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          choisir(p);
        });
        li.addEventListener("mouseenter", function () { surligner(i); });
        liste.appendChild(li);
      });
      liste.hidden = false;
      champ.setAttribute("aria-expanded", "true");
      surligner(0);
    }

    champ.addEventListener("input", function () {
      // Toute frappe invalide le choix précédent : sans ça, corriger le nom
      // après coup aurait rattaché le courriel à la mauvaise personne.
      cache.value = "";
      champ.dataset.choisi = "";
      ouvrir();
    });

    champ.addEventListener("keydown", function (ev) {
      if (liste.hidden) {
        if (ev.key === "ArrowDown") { ouvrir(); ev.preventDefault(); }
        return;
      }
      if (ev.key === "ArrowDown") {
        surligner((actif + 1) % visibles.length); ev.preventDefault();
      } else if (ev.key === "ArrowUp") {
        surligner((actif - 1 + visibles.length) % visibles.length); ev.preventDefault();
      } else if (ev.key === "Enter") {
        if (actif >= 0) { choisir(visibles[actif]); ev.preventDefault(); }
      } else if (ev.key === "Escape") {
        fermer();
      }
    });

    champ.addEventListener("blur", function () { window.setTimeout(fermer, 120); });

    // Pas de garde sur l'envoi : le champ peut être facultatif (page de dépôt),
    // et quand il ne l'est pas, `required` suffit. Un nom tapé sans avoir
    // cliqué dans la liste est résolu côté serveur, qui sait aussi dire
    // « plusieurs fiches portent ce nom » — ce qu'un blocage muet ne saurait
    // pas faire.
  }

  var formulaires = document.querySelectorAll("form[data-person-picker]");
  for (var i = 0; i < formulaires.length; i++) construire(formulaires[i]);
})();
