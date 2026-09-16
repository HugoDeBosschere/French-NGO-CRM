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
  // the mandate-only fields appear. Every block is in the page and all but one
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
      // Tell the portefeuille toggle its roles are gone: it listens for a
      // change on its own checklist, and clearing boxes from script fires none.
      scope.querySelectorAll("[data-portfolio-roles]").forEach(function (list) {
        list.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }

    function sync() {
      var type = select.value;
      form.querySelectorAll("[data-role-block]").forEach(function (block) {
        var on = block.getAttribute("data-role-block") === type;
        if (!on) clear(block);
        block.hidden = !on;
      });
      // Mandate details belong to an élu·e: circonscription, and the
      // portefeuille field (which its own toggle then shows or hides on the
      // roles actually ticked).
      form.querySelectorAll("[data-politique-only]").forEach(function (el) {
        el.hidden = type !== "Politique";
      });
      syncOrganisations(form, type);
    }

    select.addEventListener("change", sync);
    sync();
  }

  // Which organisations the person form offers: médias for a journaliste,
  // groupes politiques for a politique. Ticked boxes of the wrong type are
  // cleared, so changing someone's type cannot leave them in a média and a
  // groupe at once.
  var ORG_TYPE_OF_CONTACT = { "Journaliste": "Média", "Politique": "Groupe politique" };

  function syncOrganisations(form, contactType) {
    var wanted = ORG_TYPE_OF_CONTACT[contactType] || "";
    form.querySelectorAll("[data-org-checklist] [data-org-type]").forEach(function (row) {
      var on = row.getAttribute("data-org-type") === wanted;
      if (!on) {
        var box = row.querySelector('input[type="checkbox"]');
        if (box) box.checked = false;
      }
      row.hidden = !on;
    });
    var picker = form.querySelector("#organisation_picker");
    if (picker) {
      picker.querySelectorAll("option[data-org-type]").forEach(function (opt) {
        opt.hidden = opt.getAttribute("data-org-type") !== wanted;
      });
      picker.value = "";
    }
    // The hint under the picker says what the field means for this type, and
    // the field itself is pointless before a type is chosen.
    var field = form.querySelector("#organisation-field");
    if (field) field.hidden = !wanted;
    var hints = {
      "Journaliste": form.querySelector("[data-org-hint-journaliste]"),
      "Politique": form.querySelector("[data-org-hint-politique]")
    };
    Object.keys(hints).forEach(function (key) {
      if (hints[key]) hints[key].hidden = key !== contactType;
    });
  }

  // The same idea on the organisation form: « Type d'organisation » shows the
  // média block (type de média, orientation) or the groupe politique one
  // (chambre). Selects inside a hidden block are disabled rather than cleared,
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
      .querySelectorAll("select[data-check-group]")
      .forEach(attachChecker);
    document
      .querySelectorAll("[data-portfolio-roles]")
      .forEach(attachPortfolioToggle);
    // After the portfolio toggle, so the type toggle's initial sync has the
    // last word on whether the portefeuille field is visible at all.
    document
      .querySelectorAll("[data-contact-type-form]")
      .forEach(attachContactTypeToggle);
    document
      .querySelectorAll("[data-org-type-form]")
      .forEach(attachOrgTypeToggle);
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
  });
})();
