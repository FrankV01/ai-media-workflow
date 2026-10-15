(function () {
    "use strict";

    var STORAGE_KEY = "ai-media-workflow.recent-jobs-filters";
    var FILTER_NAMES = ["workflow", "status", "quality", "q"];
    var form = document.getElementById("job-filters");
    if (!form) return;

    function field(name) {
        return form.elements.namedItem(name);
    }

    function restore() {
        var saved;
        try {
            saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY));
        } catch (_error) {
            return;
        }
        if (!saved || typeof saved !== "object" || Array.isArray(saved)) return;

        FILTER_NAMES.forEach(function (name) {
            var control = field(name);
            var value = saved[name];
            if (!control || typeof value !== "string") return;
            if (control.tagName === "SELECT") {
                var exists = Array.prototype.some.call(control.options, function (option) {
                    return option.value === value;
                });
                if (exists) control.value = value;
                return;
            }
            control.value = value.slice(0, control.maxLength > 0 ? control.maxLength : value.length);
        });
    }

    function save() {
        var values = {};
        FILTER_NAMES.forEach(function (name) {
            var control = field(name);
            if (control) values[name] = control.value;
        });
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
        } catch (_error) {
            return;
        }
    }

    restore();
    form.addEventListener("change", save);
    var search = field("q");
    if (search) search.addEventListener("input", save);
})();
