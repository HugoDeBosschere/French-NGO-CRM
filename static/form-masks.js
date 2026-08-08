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

  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll('input[name="meeting_time"]')
      .forEach(function (el) { attach(el, TIME_STOPS, true); });
    document
      .querySelectorAll(
        'input[name="follow_up_date"], input[name="first_contacted"], input[name="meeting_date"], input[name="mail_date"]'
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
  });
})();
