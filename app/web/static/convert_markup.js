/* Markdown ⇄ HTML tool: debounced auto-convert, copy, swap, clear. */
(function () {
    "use strict";

    var ENDPOINT = "/api/convert/markup";
    var MAX_CHARS = 1000000;
    var DEBOUNCE_MS = 400;

    var input = document.getElementById("mk-input");
    var output = document.getElementById("mk-output");
    var inputLabel = document.getElementById("mk-input-label");
    var outputLabel = document.getElementById("mk-output-label");
    var inputCount = document.getElementById("mk-input-count");
    var outputCount = document.getElementById("mk-output-count");
    var convertBtn = document.getElementById("mk-convert");
    var copyBtn = document.getElementById("mk-copy");
    var swapBtn = document.getElementById("mk-swap");
    var clearBtn = document.getElementById("mk-clear");
    var errorEl = document.getElementById("mk-error");
    var statusEl = document.getElementById("mk-status");

    if (!input || !output) return;

    var requestCounter = 0;
    var debounceTimer = null;

    var LABELS = {
        "markdown-to-html": ["Markdown", "HTML"],
        "html-to-markdown": ["HTML", "Markdown"]
    };

    function direction() {
        var checked = document.querySelector('input[name="mk-direction"]:checked');
        return checked ? checked.value : "markdown-to-html";
    }

    function formatChars(n) {
        return n.toLocaleString("en-US") + " chars";
    }

    function showError(message) {
        errorEl.textContent = message;
        errorEl.classList.remove("hidden");
    }

    function clearError() {
        errorEl.textContent = "";
        errorEl.classList.add("hidden");
    }

    function updateCounts() {
        inputCount.textContent = formatChars(input.value.length);
        outputCount.textContent = formatChars(output.value.length);
    }

    function updateCopyState() {
        copyBtn.disabled = output.value.length === 0;
    }

    function updateLabels() {
        var pair = LABELS[direction()] || LABELS["markdown-to-html"];
        inputLabel.textContent = pair[0];
        outputLabel.textContent = pair[1];
    }

    function setStatus(text) {
        statusEl.textContent = text;
    }

    function invalidate() {
        requestCounter++;
        if (debounceTimer) {
            clearTimeout(debounceTimer);
            debounceTimer = null;
        }
    }

    function clearOutput() {
        output.value = "";
        updateCounts();
        updateCopyState();
    }

    function convert() {
        var requestId = ++requestCounter;
        var text = input.value;
        if (!text.trim()) {
            clearOutput();
            clearError();
            setStatus("");
            return;
        }
        if (text.length > MAX_CHARS) {
            showError("Text exceeds the 1,000,000 character limit.");
            return;
        }

        clearError();
        setStatus("Converting…");

        fetch(ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ direction: direction(), text: text })
        })
            .then(function (resp) {
                if (!resp.ok) {
                    return resp
                        .json()
                        .then(function (body) {
                            var detail = body && body.detail;
                            throw new Error(
                                typeof detail === "string"
                                    ? detail
                                    : "Conversion failed (HTTP " + resp.status + ")"
                            );
                        })
                        .catch(function (err) {
                            if (err instanceof SyntaxError) {
                                throw new Error("Conversion failed (HTTP " + resp.status + ")");
                            }
                            throw err;
                        });
                }
                return resp.json();
            })
            .then(function (data) {
                if (requestId !== requestCounter) return;
                output.value = data.output;
                updateCounts();
                updateCopyState();
                setStatus("");
            })
            .catch(function (err) {
                if (requestId !== requestCounter) return;
                setStatus("");
                showError(err.message || "Network error — could not reach the server.");
            });
    }

    function scheduleConvert() {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(convert, DEBOUNCE_MS);
    }

    input.addEventListener("input", function () {
        invalidate();
        clearOutput();
        clearError();
        setStatus("");
        scheduleConvert();
    });

    var radios = document.querySelectorAll('input[name="mk-direction"]');
    Array.prototype.forEach.call(radios, function (radio) {
        radio.addEventListener("change", function () {
            invalidate();
            updateLabels();
            clearOutput();
            convert();
        });
    });

    convertBtn.addEventListener("click", function () {
        invalidate();
        convert();
    });

    copyBtn.addEventListener("click", function () {
        function copied() {
            copyBtn.textContent = "Copied";
            setTimeout(function () {
                copyBtn.textContent = "Copy output";
            }, 1500);
        }
        function failed() {
            output.focus();
            output.select();
            showError("Copy failed — select the output and copy it manually.");
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(output.value).then(copied, failed);
        } else {
            output.select();
            var ok = false;
            try {
                ok = document.execCommand("copy");
            } catch (e) {
                ok = false;
            }
            if (ok) copied(); else failed();
        }
    });

    swapBtn.addEventListener("click", function () {
        if (!output.value) return;
        invalidate();
        var nextDirection =
            direction() === "markdown-to-html" ? "html-to-markdown" : "markdown-to-html";
        input.value = output.value;
        var radio = document.querySelector(
            'input[name="mk-direction"][value="' + nextDirection + '"]'
        );
        if (radio) radio.checked = true;
        updateLabels();
        convert();
    });

    clearBtn.addEventListener("click", function () {
        invalidate();
        input.value = "";
        clearOutput();
        clearError();
        setStatus("");
        input.focus();
    });

    updateLabels();
    updateCounts();
    updateCopyState();
})();
