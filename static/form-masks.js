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
  // textarea as a comma-separated list (no duplicates), then resets itself.
  // Used by the anonymous forms to pick deputies without losing free-text entry.
  function attachPicker(select) {
    var target = document.getElementById(select.getAttribute("data-target"));
    if (!target) return;
    select.addEventListener("change", function () {
      var value = select.value;
      if (!value) return;
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

  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll('input[name="meeting_time"]')
      .forEach(function (el) { attach(el, TIME_STOPS, true); });
    document
      .querySelectorAll(
        'input[name="follow_up_date"], input[name="first_contacted"], input[name="meeting_date"], input[name="mail_date"], input[name="alt_dates"]'
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
