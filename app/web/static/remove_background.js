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
    var previewEl = document.getElementById("rb-preview");
    var skippedEl = document.getElementById("rb-skipped");
    var againBtn = document.getElementById("rb-again");
    var rerunBtn = document.getElementById("rb-rerun");
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
    var previewUrls = [];
    var lastFiles = [];
    var lastSkipped = [];
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

    function releaseResults() {
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            objectUrl = null;
        }
        previewUrls.forEach(function (url) {
            URL.revokeObjectURL(url);
        });
        previewUrls = [];
        previewEl.textContent = "";
        metadataEl.textContent = "";
    }

    function metadataRow(label, value) {
        var row = el("div", "flex justify-between gap-4 text-sm");
        row.appendChild(el("span", "text-gray-500", label));
        row.appendChild(el("span", "text-gray-200 font-mono", value));
        return row;
    }

    function u16(view, offset) {
        return view.getUint16(offset, true);
    }

    function u32(view, offset) {
        return view.getUint32(offset, true);
    }

    /* Minimal ZIP reader: walks the central directory, returns {name, method, data}. */
    function extractZipEntries(buffer) {
        var view = new DataView(buffer);
        var eocd = -1;
        var scanFrom = Math.max(0, buffer.byteLength - 65557);
        for (var i = buffer.byteLength - 22; i >= scanFrom; i--) {
            if (u32(view, i) === 0x06054b50) {
                eocd = i;
                break;
            }
        }
        if (eocd < 0) throw new Error("not a zip archive");

        var entries = [];
        var count = u16(view, eocd + 10);
        var pos = u32(view, eocd + 16);
        var decoder = new TextDecoder();
        for (var e = 0; e < count && pos < buffer.byteLength; e++) {
            if (u32(view, pos) !== 0x02014b50) break;
            var method = u16(view, pos + 10);
            var compSize = u32(view, pos + 20);
            var nameLen = u16(view, pos + 28);
            var extraLen = u16(view, pos + 30);
            var commentLen = u16(view, pos + 32);
            var localOff = u32(view, pos + 42);
            var name = decoder.decode(new Uint8Array(buffer, pos + 46, nameLen));
            var dataStart = localOff + 30 + u16(view, localOff + 26) + u16(view, localOff + 28);
            entries.push({
                name: name,
                method: method,
                data: buffer.slice(dataStart, dataStart + compSize),
            });
            pos += 46 + nameLen + extraLen + commentLen;
        }
        return entries;
    }

    function inflateRaw(bytes) {
        var stream = new Blob([bytes])
            .stream()
            .pipeThrough(new DecompressionStream("deflate-raw"));
        return new Response(stream).arrayBuffer();
    }

    /* Builds output_name -> object URL for every entry; degrades to {} on failure. */
    function previewMapFromZip(blob) {
        if (typeof DecompressionStream === "undefined") return Promise.resolve({});
        return blob
            .arrayBuffer()
            .then(extractZipEntries)
            .then(function (entries) {
                var map = {};
                return Promise.all(
                    entries.map(function (entry) {
                        var bytes =
                            entry.method === 8
                                ? inflateRaw(entry.data)
                                : Promise.resolve(entry.data);
                        return bytes.then(function (data) {
                            var url = URL.createObjectURL(new Blob([data], { type: "image/png" }));
                            previewUrls.push(url);
                            map[entry.name] = url;
                        });
                    })
                ).then(function () {
                    return map;
                });
            })
            .catch(function () {
                return {};
            });
    }

    function openLightbox(src, alt) {
        var overlay = el(
            "div",
            "fixed inset-0 z-50 bg-black/80 flex items-center justify-center cursor-zoom-out"
        );
        var stage = el("div", "rb-checkerboard rounded p-2");
        var img = el("img", "max-h-[95vh] max-w-[95vw] object-contain");
        img.src = src;
        img.alt = alt || "cutout preview";
        stage.appendChild(img);
        overlay.appendChild(stage);
        function close() {
            document.removeEventListener("keydown", onKey);
            overlay.remove();
        }
        function onKey(event) {
            if (event.key === "Escape") close();
        }
        overlay.addEventListener("click", close);
        document.addEventListener("keydown", onKey);
        document.body.appendChild(overlay);
    }

    function checkerboardImg(src, alt, imgClass, wrapClass) {
        var wrap = el("div", "rb-checkerboard rounded " + (wrapClass || ""));
        var img = el("img", "object-contain cursor-zoom-in " + (imgClass || ""));
        img.src = src;
        img.alt = alt || "cutout preview";
        img.title = "Click to enlarge";
        img.addEventListener("click", function () {
            openLightbox(src, alt);
        });
        wrap.appendChild(img);
        return wrap;
    }

    function renderCard(item, previewUrl) {
        var card = el("div", "bg-gray-800/60 border border-gray-700 rounded p-3 space-y-1");
        if (previewUrl) {
            card.appendChild(
                checkerboardImg(previewUrl, item.output_name, "h-64 mx-auto", "mb-2 p-1")
            );
        }
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
        releaseResults();
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

        if (!isZip) {
            previewEl.appendChild(
                checkerboardImg(
                    objectUrl,
                    items[0] && items[0].output_name,
                    "max-h-[70vh] mx-auto",
                    "border border-gray-700 p-2 flex justify-center"
                )
            );
        }

        var previewsReady = isZip
            ? previewMapFromZip(blob)
            : Promise.resolve({});

        previewsReady.then(function (previewMap) {
            items.forEach(function (item) {
                var url = isZip ? previewMap[item.output_name] : null;
                metadataEl.appendChild(renderCard(item, url));
            });
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
        lastFiles = sendable;
        lastSkipped = skipped;
        upload(sendable, skipped);
    }

    function reset() {
        releaseResults();
        lastFiles = [];
        lastSkipped = [];
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
    rerunBtn.addEventListener("click", function () {
        if (!busy && lastFiles.length) upload(lastFiles, lastSkipped);
    });
    retryBtn.addEventListener("click", reset);
})();
