// Lightweight input helpers for the French time and date fields.
// - Time (Heure):        [0-2][0-9]:[0-9][0-9]  — colon auto-inserted after 2 digits.
// - Date (Relance/JJ...): [0-9]{2}/[0-9]{2}/[0-9]{4} — slashes auto-inserted.
//
// The separator is only inserted while the user types *forward at the end* of
// the field. Deleting or editing in the middle is left completely untouched, so
// every character can be changed independently without the value reshuffling.
// No dependencies; runs after the DOM is ready.

(function () {
  "use strict";

  // Positions (0-based, in the digit-only stream) after which a separator sits.
  var DATE_STOPS = { 2: "/", 4: "/" };
  var TIME_STOPS = { 2: ":" };

  function attach(input, stops, clampFirstHour) {
    input.addEventListener("input", function (e) {
      // Never reformat on deletion — let the user delete freely.
      if (e.inputType && e.inputType.indexOf("delete") === 0) return;
      // Only help when typing at the very end; mid-string edits are left alone.
      if (input.selectionStart !== input.value.length) return;

      var v = input.value;

      // Optional: constrain the first hour digit to 0, 1 or 2.
      if (clampFirstHour && /^[3-9]/.test(v)) {
        v = "2" + v.slice(1);
      }

      // If the last typed character completed a group, append the separator.
      var digits = v.replace(/\D/g, "").length;
      var sep = stops[digits];
      if (sep && !v.endsWith(sep)) {
        v = v + sep;
      }

      if (v !== input.value) {
        input.value = v;
        input.selectionStart = input.selectionEnd = v.length;
      }
    });
  }

  // A <select data-target="fieldName"> appends its chosen value to the named
  // field as a comma-separated list (no duplicates), then resets itself — or,
  // with data-single, replaces the field's value. Used by the anonymous forms
  // to pick a known person or organisation without losing free-text entry.
  function attachPicker(select) {
    var target = document.getElementById(select.getAttribute("data-target"));
    if (!target) return;
    var single = select.hasAttribute("data-single");
    select.addEventListener("change", function () {
      var value = select.value;
      if (!value) return;
      if (single) {
        target.value = value;
        select.value = "";
        target.focus();
        return;
      }
      var parts = target.value
        .split(",")
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      if (parts.indexOf(value) === -1) parts.push(value);
      target.value = parts.join(", ");
      select.value = "";
      target.focus();
    });
  }

  // A <select data-check-group="grp"> ticks the checkbox named `grp` whose
  // value matches the chosen option, then resets. Lets logged-in users pick a
  // person from a list to check them within a long checkbox group.
  function attachChecker(select) {
    var group = select.getAttribute("data-check-group");
    select.addEventListener("change", function () {
      var value = select.value;
      if (!value) return;
      var box = document.querySelector(
        'input[name="' + group + '"][value="' + value + '"]'
      );
      if (box) {
        box.checked = true;
        var row = box.closest("label");
        if (row) row.scrollIntoView({ block: "nearest" });
      }
      select.value = "";
    });
  }

  // An <input> or <select> carrying data-remember="key" keeps its last value
  // in the browser and offers it back the next time a form uses that key.
  //
  // This is localStorage: the value stays on their device, is scoped to this
  // site, and is never sent anywhere on its own. It reaches the server only if
  // they actually submit the form — exactly as if they had retyped it — so it
  // gives the site no information it would not otherwise have. Used on the
  // anonymous /declarer forms, and on the moderation forms' "Saisi par" style
  // dropdowns: login is a single shared password, so the server genuinely
  // cannot tell which team member is at the keyboard — only their own browser
  // can. It is a default, not an assertion: the field stays visible and
  // required, so whoever is typing sees it and can change it before saving.
  //
  // Storage can be unavailable or throw outright (private windows, blocked
  // site data), so every access is guarded and failure just means no prefill.
  function attachRemembered(input) {
    var key = "pauseia:" + input.getAttribute("data-remember");

    // Never overwrite a value the page already carries: a form redisplayed
    // after a validation error must keep what the visitor actually typed.
    if (!input.value) {
      try {
        var saved = window.localStorage.getItem(key);
        if (saved) {
          input.value = saved;
          // A <select> silently refuses a value with no matching option (a
          // moderator since deleted or renamed). Don't leave it half-set.
          if (input.tagName === "SELECT" && input.value !== saved) {
            input.value = "";
            window.localStorage.removeItem(key);
          }
        }
      } catch (e) { /* no storage — leave the field empty */ }
    }

    var form = input.form;
    if (!form) return;
    form.addEventListener("submit", function () {
      try {
        var value = input.value.trim();
        if (value) {
          window.localStorage.setItem(key, value);
        } else {
          window.localStorage.removeItem(key);
        }
      } catch (e) { /* nothing to do: remembering is a convenience only */ }
    });
  }

  // An <input data-follow-up-from="meeting_date" data-follow-up-days="5"> keeps
  // « Relance prévue » in step with the date it follows up: change the meeting
  // or mail date and the proposed relance moves with it.
  //
  // The server renders the same default, so this only matters while the form is
  // open. It deliberately stops helping once the value stops matching what it
  // last proposed: a date the user typed, or a field they cleared to say "no
  // follow-up", is theirs and must never be silently rewritten.
  function attachFollowUp(input) {
    var source = document.getElementById(input.getAttribute("data-follow-up-from"));
    var days = parseInt(input.getAttribute("data-follow-up-days"), 10);
    if (!source || isNaN(days)) return;

    var proposed = input.value;  // what the server pre-filled

    function frToParts(v) {
      var m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(v.trim());
      return m ? { d: +m[1], mo: +m[2], y: +m[3] } : null;
    }
    function pad(n) { return (n < 10 ? "0" : "") + n; }

    function sync() {
      // Only ever replace our own suggestion, never the user's own text.
      if (input.value !== proposed) return;
      var parts = frToParts(source.value);
      if (!parts) return;
      var dt = new Date(parts.y, parts.mo - 1, parts.d);
      if (isNaN(dt.getTime())) return;
      dt.setDate(dt.getDate() + days);
      proposed = pad(dt.getDate()) + "/" + pad(dt.getMonth() + 1) + "/" + dt.getFullYear();
      input.value = proposed;
    }

    source.addEventListener("input", sync);
    source.addEventListener("change", sync);
  }

  // The "Portefeuille" field only makes sense for a government post, so it
  // stays hidden until one of the roles listed in `data-portfolio-roles`
  // (rendered from PORTFOLIO_ROLES in app.py) is ticked. Server-side, app.py
  // drops the value again if no such role was submitted — this is only comfort,
  // never the thing that enforces it.
  function attachPortfolioToggle(list) {
    var field = document.getElementById("portefeuille-field");
    if (!field) return;
    var portfolioRoles = (list.getAttribute("data-portfolio-roles") || "").split("|");
    function sync() {
      var on = Array.prototype.some.call(
        list.querySelectorAll('input[type="checkbox"]:checked'),
        function (box) { return portfolioRoles.indexOf(box.value) !== -1; }
      );
      field.hidden = !on;
    }
    list.addEventListener("change", sync);
    sync();
  }

  // The twin of the above, for « Préciser »: it appears once « Bénévole » or
  // « Employé·e » is ticked (data-role-detail-roles, rendered from
  // ROLE_DETAIL_ROLES). Unlike the portefeuille the boxes live in two blocks —
  // ONG and entreprise each have their own list — so what decides is whether
  // any *visible* list has one ticked, not just this one: a hidden block's
  // boxes are cleared by the type toggle, but only once it has run.
  function attachRoleDetailToggle(list) {
    var field = document.getElementById("role-detail-field");
    if (!field) return;
    var detailRoles = (list.getAttribute("data-role-detail-roles") || "").split("|");
    var scope = list.closest("form") || document;

    function sync() {
      var on = Array.prototype.some.call(
        scope.querySelectorAll("[data-role-detail-roles]"),
        function (other) {
          if (other.closest("[data-role-block][hidden]")) return false;
          return Array.prototype.some.call(
            other.querySelectorAll('input[type="checkbox"]:checked'),
            function (box) { return detailRoles.indexOf(box.value) !== -1; }
          );
        }
      );
      field.hidden = !on;
    }
    list.addEventListener("change", sync);
    sync();
  }

  // « Format de la rencontre » drives the free-text field under it: présentiel
  // asks where you went and insists on an answer, visio reuses the same field
  // for an optional link. Hidden until a format is picked, so the question is
  // never asked before it means anything.
  //
  // app.py validates the same rule on submit — this only spares the user a
  // round trip, it is never what enforces it. The `required` attribute is set
  // only where the server actually requires the field, which the presence of
  // the « * » marker tells us: the anonymous /declarer form asks the same
  // question without ever blocking on it.
  function attachFormatToggle(group) {
    var field = document.getElementById("meeting-place-field");
    if (!field) return;
    var input = field.querySelector('input[name="meeting_place"]');
    var label = field.querySelector("[data-place-label]");
    var req = field.querySelector("[data-place-req]");
    var optional = field.querySelector("[data-place-optional]");

    function sync() {
      var picked = group.querySelector('input[name="meeting_format"]:checked');
      var value = picked ? picked.value : "";
      field.hidden = !value;
      var mandatory = value === "presentiel";
      if (label) label.textContent = mandatory ? "Lieu" : "Lien / plateforme";
      if (req) {
        req.hidden = !mandatory;
        if (input) input.required = mandatory;
      }
      if (optional) optional.hidden = mandatory;
    }
    group.addEventListener("change", sync);
    sync();
  }

  // The « Type de contact » select drives the rest of the person form: which
  // Fonction checklist is shown, which organisations can be ticked, and whether
  // the mandate-only fields appear. For a religieux·se a « Religion » select
  // inside that block narrows the fonctions one level further. Every block is in the page and all but one
  // hidden, so switching type costs no round trip. This is comfort only —
  // _save_person drops the other type's values server-side regardless.
  //
  // Hidden checkboxes still post, so the boxes of a hidden block are cleared
  // as well as hidden: otherwise retyping someone as a journaliste would
  // submit « Député·e » along with it, and the same organisation ids.
  function attachContactTypeToggle(form) {
    var select = form.querySelector("[data-contact-type]");
    if (!select) return;

    function clear(scope) {
      scope.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
        box.checked = false;
      });
      // Tell the portefeuille and « Préciser » toggles their roles are gone:
      // each listens for a change on its own checklist, and clearing boxes
      // from script fires none.
      scope.querySelectorAll("[data-portfolio-roles], [data-role-detail-roles]")
        .forEach(function (list) {
          list.dispatchEvent(new Event("change", { bubbles: true }));
        });
    }

    // One level further in, for a religieux·se only: « Religion » picks which
    // culte's fonctions are offered. Same contract as the block above — the
    // other cultes' boxes are cleared as well as hidden, so switching someone
    // from Catholicisme to Judaïsme cannot leave « Cardinal » ticked and
    // posting. _roles_from_form drops it server-side too.
    var religionSelect = form.querySelector("[data-religion]");

    function syncReligion() {
      if (!religionSelect) return;
      var religion = religionSelect.value;
      form.querySelectorAll("[data-religion-block]").forEach(function (block) {
        var on = block.getAttribute("data-religion-block") === religion;
        if (!on) clear(block);
        block.hidden = !on;
      });
      // Sixty-odd fonctions across seven cultes would be unreadable all at
      // once, so nothing is listed until a religion is chosen; the prompt says
      // so in the meantime.
      var prompt = form.querySelector("[data-religion-prompt]");
      if (prompt) prompt.hidden = !!religion;
    }

    function sync() {
      var type = select.value;
      form.querySelectorAll("[data-role-block]").forEach(function (block) {
        var on = block.getAttribute("data-role-block") === type;
        if (!on) clear(block);
        block.hidden = !on;
      });
      // « Préciser » depends on which block is now visible, so it is re-asked
      // after the blocks have been shown and hidden, never before.
      form.querySelectorAll("[data-role-detail-roles]").forEach(function (list) {
        list.dispatchEvent(new Event("change", { bubbles: true }));
      });
      // Mandate details belong to an élu·e: circonscription, and the
      // portefeuille field (which its own toggle then shows or hides on the
      // roles actually ticked).
      form.querySelectorAll("[data-politique-only]").forEach(function (el) {
        el.hidden = type !== "Politique";
      });
      // « Territoire assigné » is its religious counterpart.
      form.querySelectorAll("[data-religieux-only]").forEach(function (el) {
        el.hidden = type !== "Religieux\u00b7se";
      });
      syncReligion();
      syncOrganisations(form, type);
    }

    if (religionSelect) religionSelect.addEventListener("change", syncReligion);
    select.addEventListener("change", sync);
    sync();
  }

  // Which organisations the person form offers: médias for a journaliste,
  // groupes politiques for a politique. Ticked boxes of the wrong type are
  // cleared, so changing someone's type cannot leave them in a média and a
  // groupe at once.
  var ORG_TYPE_OF_CONTACT = {
    "Journaliste": "M\u00e9dia",
    "Politique": "Groupe politique",
    "Religieux\u00b7se": "Culte",
    "Membre d'une ONG": "ONG",
    "Membre d'une entreprise": "Entreprise",
    "Autre": "Autre"
  };

  // ONG, entreprise and autre belong to no type de contact, so they are offered
  // on top of whichever one is chosen: a journaliste can sit on an NGO board
  // and an élu·e can chair an association. app.py keeps the same set on save.
  var NEUTRAL_ORG_TYPES = ["ONG", "Entreprise", "Autre"];

  function syncOrganisations(form, contactType) {
    var own = ORG_TYPE_OF_CONTACT[contactType] || "";
    var wanted = own ? [own].concat(NEUTRAL_ORG_TYPES) : [];
    form.querySelectorAll("[data-org-checklist] [data-org-type]").forEach(function (row) {
      var on = wanted.indexOf(row.getAttribute("data-org-type")) !== -1;
      if (!on) {
        var box = row.querySelector('input[type="checkbox"]');
        if (box) box.checked = false;
      }
      row.hidden = !on;
    });
    var picker = form.querySelector("#organisation_picker");
    if (picker) {
      picker.querySelectorAll("option[data-org-type]").forEach(function (opt) {
        opt.hidden = wanted.indexOf(opt.getAttribute("data-org-type")) === -1;
      });
      picker.value = "";
    }
    // The hint under the picker says what the field means for this type, and
    // the field itself is pointless before a type is chosen.
    var field = form.querySelector("#organisation-field");
    if (field) field.hidden = !own;
    // The three neutral types get no hint of their own: their organisation is
    // named by the type itself, so there is nothing left to explain.
    var hints = {
      "Journaliste": form.querySelector("[data-org-hint-journaliste]"),
      "Politique": form.querySelector("[data-org-hint-politique]"),
      "Religieux\u00b7se": form.querySelector("[data-org-hint-religieux]")
    };
    Object.keys(hints).forEach(function (key) {
      if (hints[key]) hints[key].hidden = key !== contactType;
    });
  }

  // The same idea on the organisation form: « Type d'organisation » shows the
  // média block (type de média, orientation), the groupe politique one
  // (chambre) or the culte one (religion). Selects inside a hidden block are disabled rather than cleared,
  // since a disabled control posts nothing and a required one would otherwise
  // block submission while invisible.
  function attachOrgTypeToggle(form) {
    var select = form.querySelector("[data-org-type]");
    if (!select) return;
    function sync() {
      form.querySelectorAll("[data-org-block]").forEach(function (block) {
        var on = block.getAttribute("data-org-block") === select.value;
        block.hidden = !on;
        block.querySelectorAll("select, input").forEach(function (el) {
          el.disabled = !on;
        });
      });
    }
    select.addEventListener("change", sync);
    sync();
  }

  // Ticking a name on /todo or /repartition saves straight away — no
  // « Enregistrer » to remember. Both endpoints already take the whole set of
  // boxes and replace what was stored, so a save is just the form as it
  // stands, and a save that overtakes another is harmless: the last one wins
  // and it carries the complete answer.
  //
  // The counts, the red/green state and the Valider buttons are recomputed
  // here rather than waiting for a response, because a date's count *is* the
  // number of boxes ticked in its column — the server has nothing to add.
  var AUTOSAVE_DEBOUNCE_MS = 500;

  function attachAutosave(form) {
    var min = parseInt(form.getAttribute("data-min-participants"), 10) || 0;
    var status = form.querySelector("[data-autosave-status]");
    var fallback = form.querySelector("[data-autosave-fallback]");
    var timer = null;
    var pending = 0;

    // With the script running, the button is redundant. It stays in the markup
    // so a browser without JavaScript keeps a way to save.
    if (fallback) fallback.hidden = true;

    function say(key) {
      if (!status) return;
      status.textContent = status.getAttribute("data-" + key) || "";
      status.classList.toggle("is-failed", key === "failed");
    }

    // Each group of checkboxes that counts as one total: a date column on
    // /repartition, the sign-up list of one rencontre on /todo.
    function groups() {
      return [].slice.call(form.querySelectorAll("[data-date-col]"));
    }

    function recount() {
      groups().forEach(function (group) {
        var count = group.querySelectorAll('input[type="checkbox"]:checked').length;
        var enough = count >= min;
        // Where the colour lives: the column itself on /repartition, the
        // surrounding list item on /todo.
        var target = group.closest(".date-col, .todo-signup") || group;
        target.classList.toggle("signup-ok", enough);
        target.classList.toggle("signup-short", !enough);
        var note = target.querySelector("[data-short-note]");
        if (note) note.hidden = enough;
        [].forEach.call(target.querySelectorAll("[data-count]"), function (el) {
          el.textContent = count;
        });
        [].forEach.call(target.querySelectorAll("[data-plural]"), function (el) {
          el.textContent = count === 1 ? "" : "s";
        });

        // /repartition only: the Valider button for this date sits outside the
        // form, and must not offer to settle an understaffed date.
        var on_date = group.getAttribute("data-date-col");
        var card = form.closest(".repartition-card");
        if (!on_date || !card) return;
        var button = card.querySelector('[data-validate-date="' + on_date + '"]');
        if (!button) return;
        button.disabled = !enough;
        button.classList.toggle("btn-primary", enough);
        if (enough) {
          button.removeAttribute("title");
        } else {
          button.setAttribute("title", form.getAttribute("data-short-title") || "");
        }
        [].forEach.call(button.querySelectorAll("[data-count]"), function (el) {
          el.textContent = count;
        });
      });
    }

    function save() {
      pending += 1;
      say("saving");
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      })
        .then(function (res) {
          if (!res.ok) throw new Error(res.status);
          pending -= 1;
          if (pending === 0) say("saved");
        })
        .catch(function () {
          pending -= 1;
          say("failed");
          // Hand the button back: the save has to be completable by hand.
          if (fallback) fallback.hidden = false;
        });
    }

    form.addEventListener("change", function (event) {
      if (event.target.type !== "checkbox") return;
      recount();
      window.clearTimeout(timer);
      timer = window.setTimeout(save, AUTOSAVE_DEBOUNCE_MS);
    });

    // A tick still inside the debounce window when the page is left would be
    // lost. `keepalive` lets the request outlive the page, and keeps the header
    // that stops the server answering with a redirect and a stray flash.
    window.addEventListener("pagehide", function () {
      if (timer === null) return;
      window.clearTimeout(timer);
      timer = null;
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
        keepalive: true,
      }).catch(function () {});
    });
  }

  // « Autres dates possibles »: filling in a date reveals the heure field for
  // that alternative, so an hour can be given for any of them — and two
  // créneaux on the same day are simply the same date twice with two hours.
  // An empty row keeps its heure field out of the way; one that already holds
  // an hour stays open, since hiding a value somebody typed is a good way to
  // lose it without noticing.
  //
  // Comfort only: the server reads the two lists index for index and dedupes
  // on date + heure, so what it stores is right however the fields appeared.
  function attachAltSlots(container) {
    function sync() {
      [].forEach.call(container.querySelectorAll(".alt-slot"), function (slot) {
        var when = slot.querySelector("[data-alt-date]");
        var at = slot.querySelector("[data-alt-time]");
        if (!at) return;
        at.hidden = !((when && when.value.trim()) || at.value.trim());
      });
    }

    container.addEventListener("input", sync);
    sync();
  }

  // Une entrée de liste n'est visible que si TOUS les filtres l'acceptent :
  // celui par genre et la recherche par nom. Chacun écrit son verdict dans un
  // data-attribut, cette fonction les combine.
  function appliquerVisibilite(el) {
    var box = el.querySelector('input[type="checkbox"]');
    if (box && box.checked) { el.hidden = false; return; }  // jamais cacher un choix fait
    el.hidden = el.dataset.genreOk === "" || el.dataset.nomOk === "";
  }

  // Recherche dans une liste de cases à cocher. Remplace un <select> qui
  // reprenait toute la liste une seconde fois : ici, rien n'est ajouté à la
  // page, on filtre ce qui s'y trouve déjà. Sans JavaScript, le champ ne fait
  // rien et la liste complète reste utilisable.
  function attachChecklistFilter(champ) {
    var bloc = document.getElementById(champ.getAttribute("aria-controls"));
    if (!bloc) return;
    var entries = bloc.querySelectorAll("[data-nom]");

    function sansAccents(s) {
      return (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    }

    function filtrer() {
      // Mots dans n'importe quel ordre : « bost christine » trouve
      // « Christine Bost », comme dans la file « à rattacher ».
      var mots = sansAccents(champ.value).split(/\s+/).filter(Boolean);
      entries.forEach(function (el) {
        var cible = sansAccents(el.getAttribute("data-nom"));
        var ok = mots.every(function (m) { return cible.indexOf(m) !== -1; });
        el.dataset.nomOk = ok ? "1" : "";
        appliquerVisibilite(el);
      });
    }

    entries.forEach(function (el) {
      if (el.dataset.nomOk === undefined) el.dataset.nomOk = "1";
      if (el.dataset.genreOk === undefined) el.dataset.genreOk = "1";
    });
    champ.addEventListener("input", filtrer);
    // Une case qu'on décoche pendant une recherche doit disparaître si elle ne
    // correspond pas : sinon la liste ment sur ce qu'elle montre.
    bloc.addEventListener("change", filtrer);
  }

  // A [data-genre-filter] block narrows a list of people to those the chosen
  // genre concerns. Each entry carries data-genres (its person's genres,
  // « | »-separated — see person_genres in app.py). A box already ticked stays
  // visible whatever the genre, so an edit never hides what it is about to
  // save, and the genre stays a way of finding people rather than a rule about
  // who may be named.
  function attachGenreFilter(block) {
    var genre = document.getElementById(block.getAttribute("data-genre-filter"));
    if (!genre) return;
    var entries = block.querySelectorAll("[data-genres]");

    function matches(el) {
      var want = genre.value;
      if (!want) return true;
      var has = el.getAttribute("data-genres");
      if (!has) return true;
      return has.split("|").indexOf(want) !== -1;
    }

    function sync() {
      entries.forEach(function (el) {
        var box = el.querySelector('input[type="checkbox"]');
        // `genreOk` plutôt que `hidden` directement : la recherche par nom
        // pose son propre verdict sur les mêmes entrées, et c'est appliquer
        // les deux ensemble qui décide. Sans ça, le dernier des deux filtres
        // à s'exécuter effaçait la décision de l'autre.
        el.dataset.genreOk = (matches(el) || (box && box.checked)) ? "1" : "";
        appliquerVisibilite(el);
      });
    }
    genre.addEventListener("change", sync);
    block.addEventListener("change", sync);
    sync();
  }

  // Répartition of « À contacter ». Each utilisateurice's « Ne contacte pas »
  // list only offers the organisations ticked above it; a hidden exclusion is
  // unticked too, so it cannot linger unseen and be posted. A type's « Tout
  // cocher » ticks all its organisations (or unticks them once all are), its
  // number and unit fill in its organisations', and « Tous : <type> » in an
  // exclusion list ticks that type's organisations in that list.
  function attachSplitForm(form) {
    function orgBoxes(type) {
      return form.querySelectorAll(
        'input[name="org_ids"][data-org-type="' + type + '"]'
      );
    }
    function visibleExclusions(box, type) {
      return Array.prototype.filter.call(
        box.querySelectorAll('[data-split-org][data-org-type="' + type + '"]'),
        function (label) { return !label.hidden; }
      ).map(function (label) { return label.querySelector("input"); });
    }
    function sync() {
      var chosen = {};
      form.querySelectorAll('input[name="org_ids"]').forEach(function (b) {
        if (b.checked) chosen[b.value] = true;
      });
      form.querySelectorAll(".split-excl").forEach(function (box) {
        var any = false;
        box.querySelectorAll("[data-split-org]").forEach(function (label) {
          var on = !!chosen[label.getAttribute("data-split-org")];
          label.hidden = !on;
          if (!on) label.querySelector("input").checked = false;
          any = any || on;
        });
        box.querySelector("[data-split-none]").hidden = any;
        syncTypes(box);
      });
    }
    function syncTypes(box) {
      box.querySelectorAll("[data-split-excl-type]").forEach(function (label) {
        var inputs = visibleExclusions(box, label.getAttribute("data-split-excl-type"));
        label.hidden = !inputs.length;
        label.querySelector("input").checked = inputs.length > 0 &&
          inputs.every(function (i) { return i.checked; });
      });
    }

    // A type's number and unit are copied into every one of its
    // organisations as they are typed, overwriting what was there; any one
    // of them can still be changed afterwards.
    form.querySelectorAll("[data-type-target]").forEach(function (input) {
      var type = input.getAttribute("data-type-target");
      input.addEventListener("input", function () {
        form.querySelectorAll('[data-org-target="' + type + '"]')
          .forEach(function (o) { o.value = input.value; });
      });
    });
    form.querySelectorAll("[data-type-unit]").forEach(function (select) {
      var type = select.getAttribute("data-type-unit");
      select.addEventListener("change", function () {
        form.querySelectorAll('[data-org-unit="' + type + '"]')
          .forEach(function (o) { o.value = select.value; });
      });
    });

    form.querySelectorAll("[data-split-type-all]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var boxes = orgBoxes(btn.getAttribute("data-split-type-all"));
        var allOn = Array.prototype.every.call(boxes, function (b) {
          return b.checked;
        });
        boxes.forEach(function (b) { b.checked = !allOn; });
        sync();
      });
    });

    // « N avec les filtres » under each organisation, recounted as the
    // courriels bounds are typed, « Ne pas tirer… » is ticked or the
    // déclaration menu changes, from the [courriels, déjà en attente, a signé]
    // triples the page carries. Shown only while a
    // filter is set: otherwise it would just repeat the total.
    function number(name) {
      var el = form.querySelector('[name="' + name + '"]');
      var v = el && el.value.trim();
      return v === "" || v == null || isNaN(+v) ? null : +v;
    }
    function recount() {
      var min = number("min_mails"), max = number("max_mails");
      var skip = form.querySelector('[name="skip_pending"]');
      skip = !!(skip && skip.checked);
      var decl = form.querySelector('[name="declaration"]');
      decl = decl ? decl.value : "";
      var active = min !== null || max !== null || skip || decl !== "";
      form.querySelectorAll("[data-members]").forEach(function (counts) {
        var span = counts.querySelector("[data-match]");
        if (!active) { span.hidden = true; return; }
        var n = 0;
        JSON.parse(counts.getAttribute("data-members")).forEach(function (m) {
          if ((min === null || m[0] >= min) && (max === null || m[0] <= max) &&
              !(skip && m[1]) &&
              !(decl === "oui" && !m[2]) && !(decl === "non" && m[2])) n++;
        });
        span.textContent = " · " + n + " avec les filtres";
        span.classList.toggle("warn", n === 0);
        span.hidden = false;
      });
    }
    form.querySelectorAll("[data-split-filter]").forEach(function (el) {
      el.addEventListener("input", recount);
      el.addEventListener("change", recount);
    });
    recount();

    form.addEventListener("change", function (e) {
      if (e.target.name === "org_ids") return sync();
      var box = e.target.closest(".split-excl");
      if (!box) return;
      // An exclusion only counts for someone taking part: ticking one ticks
      // the utilisateurice too, rather than being silently ignored.
      if (e.target.checked) {
        var who = box.closest(".split-mod")
          .querySelector('input[name="moderator_ids"]');
        if (who) who.checked = true;
      }
      var typeLabel = e.target.closest("[data-split-excl-type]");
      if (typeLabel) {
        visibleExclusions(box, typeLabel.getAttribute("data-split-excl-type"))
          .forEach(function (i) { i.checked = e.target.checked; });
      }
      syncTypes(box);
    });
    sync();
  }

  // Répartition preview: choosing another utilisateurice for someone moves
  // their row onto that utilisateurice's board, in its sorted place, and the
  // boards' counts follow.
  function attachSplitPreview(form) {
    function refresh(board) {
      var n = board.querySelectorAll(".split-rows > li").length;
      board.querySelector("[data-split-board-count]").textContent = n;
      board.querySelector("[data-split-board-empty]").hidden = n > 0;
    }
    form.addEventListener("change", function (e) {
      if (!/^assign-/.test(e.target.name || "")) return;
      var row = e.target.closest("li");
      var from = row.closest("[data-split-board]");
      var to = form.querySelector('[data-split-board="' + e.target.value + '"]');
      if (!to || to === from) return;
      var list = to.querySelector(".split-rows");
      var key = row.getAttribute("data-sort");
      var next = Array.prototype.find.call(list.children, function (li) {
        return li.getAttribute("data-sort") > key;
      });
      list.insertBefore(row, next || null);
      refresh(from);
      refresh(to);
      e.target.focus();
    });
  }

  // /todo: the « Utilisateurice » filter on a section. Items carry data-by,
  // the comma-separated names of who is to do them (empty for « quiconque »).
  // Blocks left with nothing visible hide, headings and all, and the count in
  // the section title follows. The choice is remembered per browser, like
  // data-remember, since the shared login cannot say who is looking.
  function attachTodoFilter(select) {
    var key = select.getAttribute("data-todo-filter");
    var scope = document.querySelector('[data-todo-filtered="' + key + '"]');
    var count = document.querySelector('[data-todo-count="' + key + '"]');
    var storeKey = "pauseia:todo-filter:" + key;
    if (!scope) { select.hidden = true; return; }

    function apply() {
      var who = select.value, shown = 0;
      scope.querySelectorAll("[data-todo-block]").forEach(function (block) {
        var inBlock = 0;
        block.querySelectorAll("[data-by]").forEach(function (item) {
          var by = item.getAttribute("data-by");
          var names = by ? by.split(",").map(function (n) { return n.trim(); }) : [];
          var on = !who ||
            (who === "__anyone__" ? names.length === 0 : names.indexOf(who) !== -1);
          item.hidden = !on;
          if (on) inBlock++;
        });
        block.hidden = !inBlock;
        shown += inBlock;
      });
      if (count) count.textContent = shown;
      var empty = scope.querySelector("[data-todo-empty]");
      if (empty) empty.hidden = shown > 0;
    }

    try {
      var saved = window.localStorage.getItem(storeKey);
      if (saved) {
        select.value = saved;
        if (select.value !== saved) select.value = "";
      }
    } catch (e) { /* no storage — show everything */ }
    select.addEventListener("change", function () {
      try { window.localStorage.setItem(storeKey, select.value); } catch (e) {}
      apply();
    });
    apply();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll('input[name="meeting_time"], input[name="alt_times"]')
      .forEach(function (el) { attach(el, TIME_STOPS, true); });
    document
      .querySelectorAll(
        'input[name="follow_up_date"], input[name="first_contacted"], ' +
        'input[name="meeting_date"], input[name="mail_date"], ' +
        'input[name="alt_dates"], input[name="published_on"], ' +
        'input[name="intervention_date"]'
      )
      .forEach(function (el) { attach(el, DATE_STOPS, false); });
    document
      .querySelectorAll("select[data-target]")
      .forEach(attachPicker);
    document
      .querySelectorAll("input.checklist-filter")
      .forEach(attachChecklistFilter);
    document
      .querySelectorAll("select[data-check-group]")
      .forEach(attachChecker);
    document
      .querySelectorAll("[data-portfolio-roles]")
      .forEach(attachPortfolioToggle);
    document
      .querySelectorAll("[data-role-detail-roles]")
      .forEach(attachRoleDetailToggle);
    // After the portfolio toggle, so the type toggle's initial sync has the
    // last word on whether the portefeuille field is visible at all.
    document
      .querySelectorAll("[data-contact-type-form]")
      .forEach(attachContactTypeToggle);
    document
      .querySelectorAll("[data-org-type-form]")
      .forEach(attachOrgTypeToggle);
    document
      .querySelectorAll("[data-genre-filter]")
      .forEach(attachGenreFilter);
    document
      .querySelectorAll("form[data-autosave]")
      .forEach(attachAutosave);
    document
      .querySelectorAll("[data-alt-slots]")
      .forEach(attachAltSlots);
    document
      .querySelectorAll("[data-format-group]")
      .forEach(attachFormatToggle);
    document
      .querySelectorAll("[data-remember]")
      .forEach(attachRemembered);
    document
      .querySelectorAll("[data-follow-up-from]")
      .forEach(attachFollowUp);
    document
      .querySelectorAll("[data-split-form]")
      .forEach(attachSplitForm);
    document
      .querySelectorAll("[data-todo-filter]")
      .forEach(attachTodoFilter);
    document
      .querySelectorAll("[data-split-preview]")
      .forEach(attachSplitPreview);
  });
})();
