/* Generic tab switching: hash sync + arrow-key navigation.
   Finds [role=tablist] buttons and toggles their aria-controls panels. */
(function () {
    "use strict";

    var ACTIVE_CLASSES = ["border-indigo-500", "text-white"];
    var INACTIVE_CLASSES = ["text-gray-400", "hover:text-gray-200", "border-transparent"];

    var tablists = document.querySelectorAll("[role=tablist]");
    tablists.forEach(function (tablist) {
        var tabs = Array.prototype.slice.call(tablist.querySelectorAll("[role=tab]"));
        if (!tabs.length) return;

        function panelFor(tab) {
            return document.getElementById(tab.getAttribute("aria-controls"));
        }

        function hashFor(tab) {
            var id = panelFor(tab);
            return id ? id.id.replace(/^panel-/, "") : "";
        }

        function select(tab, updateHash) {
            tabs.forEach(function (t) {
                var active = t === tab;
                var panel = panelFor(t);
                t.setAttribute("aria-selected", active ? "true" : "false");
                t.classList.remove.apply(t.classList, active ? INACTIVE_CLASSES : ACTIVE_CLASSES);
                t.classList.add.apply(t.classList, active ? ACTIVE_CLASSES : INACTIVE_CLASSES);
                if (panel) panel.hidden = !active;
            });
            if (updateHash !== false) {
                var hash = hashFor(tab);
                if (hash && window.history && history.replaceState) {
                    history.replaceState(null, "", "#" + hash);
                }
            }
        }

        tabs.forEach(function (tab, index) {
            tab.addEventListener("click", function () {
                select(tab);
            });
            tab.addEventListener("keydown", function (event) {
                var next = null;
                if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
                if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
                if (next) {
                    event.preventDefault();
                    select(next);
                    next.focus();
                }
            });
        });

        var wanted = (window.location.hash || "").replace(/^#/, "");
        var initial = tabs[0];
        tabs.forEach(function (tab) {
            if (hashFor(tab) === wanted) initial = tab;
        });
        select(initial, false);
    });
})();
