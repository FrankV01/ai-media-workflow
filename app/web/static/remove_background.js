/* Background removal drop-zone: idle / drag-hover / processing / success / error states. */
(function () {
    "use strict";

    var MAX_BYTES = 25 * 1024 * 1024;
    var ENDPOINT = "/api/remove-background/";

    var dropzone = document.getElementById("rb-dropzone");
    var fileInput = document.getElementById("rb-input");
    var processingEl = document.getElementById("rb-processing");
    var successEl = document.getElementById("rb-success");
    var errorEl = document.getElementById("rb-error");
    var errorMsg = document.getElementById("rb-error-message");
    var downloadLink = document.getElementById("rb-download");
    var metadataEl = document.getElementById("rb-metadata");
    var skippedEl = document.getElementById("rb-skipped");
    var againBtn = document.getElementById("rb-again");
    var retryBtn = document.getElementById("rb-retry");

    var keyAuto = document.getElementById("rb-key-auto");
    var keyColor = document.getElementById("rb-key-color");
    var tolerance = document.getElementById("rb-tolerance");
    var toleranceValue = document.getElementById("rb-tolerance-value");
    var feather = document.getElementById("rb-feather");
    var featherValue = document.getElementById("rb-feather-value");
    var erode = document.getElementById("rb-erode");
    var despill = document.getElementById("rb-despill");

    var objectUrl = null;
    var busy = false;

    var DRAG_CLASSES = ["border-indigo-500", "bg-indigo-500/10"];

    function setState(state) {
        dropzone.classList.toggle("hidden", state !== "idle");
        processingEl.classList.toggle("hidden", state !== "processing");
        processingEl.classList.toggle("flex", state === "processing");
        successEl.classList.toggle("hidden", state !== "success");
        errorEl.classList.toggle("hidden", state !== "error");
        errorEl.classList.toggle("flex", state === "error");
        busy = state === "processing";
    }

    function formatBytes(n) {
        if (n < 1024) return n + " B";
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
        return (n / (1024 * 1024)).toFixed(2) + " MB";
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function showError(message) {
        errorMsg.textContent = message;
        setState("error");
    }

    function metadataRow(label, value) {
        var row = el("div", "flex justify-between gap-4 text-sm");
        row.appendChild(el("span", "text-gray-500", label));
        row.appendChild(el("span", "text-gray-200 font-mono", value));
        return row;
    }

    function renderCard(item) {
        var card = el("div", "bg-gray-800/60 border border-gray-700 rounded p-3 space-y-1");
        card.appendChild(el("p", "text-sm font-medium text-gray-100 mb-1", item.output_name));
        card.appendChild(
            metadataRow("Dimensions", item.width + " × " + item.height + " px")
        );
        card.appendChild(
            metadataRow(
                "File size",
                formatBytes(item.source_bytes) + " → " + formatBytes(item.output_bytes)
            )
        );
        card.appendChild(
            metadataRow("Transparent", item.removed_pct + "% of pixels")
        );
        card.appendChild(metadataRow("Key color", item.key_color));
        return card;
    }

    function showSuccess(blob, isZip, manifest) {
        objectUrl = URL.createObjectURL(blob);
        var items = manifest.converted || [];
        var name = isZip
            ? "cutout-images.zip"
            : (items[0] && items[0].output_name) || "cutout.png";
        downloadLink.href = objectUrl;
        downloadLink.setAttribute("download", name);
        downloadLink.textContent = isZip
            ? "Download ZIP (" + items.length + " files)"
            : "Download PNG";

        metadataEl.textContent = "";
        items.forEach(function (item) {
            metadataEl.appendChild(renderCard(item));
        });

        skippedEl.textContent = "";
        (manifest.skipped || []).forEach(function (skip) {
            skippedEl.appendChild(
                el("li", "text-xs text-amber-500", "Skipped " + skip.name + ": " + skip.reason)
            );
        });

        setState("success");
    }

    function collectSettings() {
        var contiguous =
            (document.querySelector('input[name="rb-fill"]:checked') || {}).value !== "global";
        return {
            key_color: keyAuto.checked ? "" : keyColor.value,
            tolerance: tolerance.value,
            contiguous: contiguous ? "true" : "false",
            feather: feather.value,
            despill: despill.checked ? "true" : "false",
            erode: erode.value || "0",
        };
    }

    function upload(files, localSkipped) {
        setState("processing");
        var form = new FormData();
        files.forEach(function (file) {
            form.append("files", file, file.name);
        });
        var opts = collectSettings();
        Object.keys(opts).forEach(function (key) {
            form.append(key, opts[key]);
        });

        fetch(ENDPOINT, { method: "POST", body: form })
            .then(function (resp) {
                if (!resp.ok) {
                    return resp
                        .json()
                        .then(function (body) {
                            var detail = body && body.detail;
                            throw new Error(
                                typeof detail === "string"
                                    ? detail
                                    : "Removal failed (HTTP " + resp.status + ")"
                            );
                        })
                        .catch(function (err) {
                            if (err instanceof SyntaxError) {
                                throw new Error("Removal failed (HTTP " + resp.status + ")");
                            }
                            throw err;
                        });
                }
                var manifest = {};
                try {
                    manifest = JSON.parse(resp.headers.get("X-Removal-Results") || "{}");
                } catch (e) {
                    manifest = {};
                }
                manifest.skipped = localSkipped.concat(manifest.skipped || []);
                return resp.blob().then(function (blob) {
                    showSuccess(blob, resp.headers.get("Content-Type") === "application/zip", manifest);
                });
            })
            .catch(function (err) {
                showError(err.message || "Network error — could not reach the server.");
            });
    }

    function handleFiles(fileList) {
        if (busy || !fileList || !fileList.length) return;

        var skipped = [];
        var files = Array.prototype.slice.call(fileList);
        var sendable = [];

        files.forEach(function (file) {
            var isPng = /\.png$/i.test(file.name) || file.type === "image/png";
            if (!isPng) {
                skipped.push({ name: file.name, reason: "not a PNG file" });
            } else if (file.size > MAX_BYTES) {
                skipped.push({ name: file.name, reason: "exceeds the 25 MB limit" });
            } else {
                sendable.push(file);
            }
        });

        if (!sendable.length) {
            showError(
                skipped.length
                    ? "Nothing to process — " + skipped[0].name + ": " + skipped[0].reason
                    : "Drop a PNG file to remove its background."
            );
            return;
        }
        upload(sendable, skipped);
    }

    function reset() {
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            objectUrl = null;
        }
        fileInput.value = "";
        dropzone.classList.remove.apply(dropzone.classList, DRAG_CLASSES);
        setState("idle");
    }

    keyAuto.addEventListener("change", function () {
        keyColor.disabled = keyAuto.checked;
    });
    tolerance.addEventListener("input", function () {
        toleranceValue.textContent = tolerance.value;
    });
    feather.addEventListener("input", function () {
        featherValue.textContent = feather.value;
    });

    ["dragenter", "dragover"].forEach(function (eventName) {
        dropzone.addEventListener(eventName, function (event) {
            event.preventDefault();
            if (!busy) dropzone.classList.add.apply(dropzone.classList, DRAG_CLASSES);
        });
    });
    ["dragleave", "drop"].forEach(function (eventName) {
        dropzone.addEventListener(eventName, function (event) {
            event.preventDefault();
            // Ignore dragleave that merely moves between child elements
            if (eventName === "drop" || !dropzone.contains(event.relatedTarget)) {
                dropzone.classList.remove.apply(dropzone.classList, DRAG_CLASSES);
            }
        });
    });
    dropzone.addEventListener("drop", function (event) {
        handleFiles(event.dataTransfer.files);
    });
    ["dragover", "drop"].forEach(function (eventName) {
        window.addEventListener(eventName, function (event) {
            event.preventDefault();
        });
    });

    dropzone.addEventListener("click", function () {
        if (!busy) fileInput.click();
    });
    dropzone.addEventListener("keydown", function (event) {
        if (!busy && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            fileInput.click();
        }
    });
    fileInput.addEventListener("change", function () {
        handleFiles(fileInput.files);
    });

    againBtn.addEventListener("click", reset);
    retryBtn.addEventListener("click", reset);
})();
