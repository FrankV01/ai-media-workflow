(function () {
    "use strict";

    async function htmlText(markdown) {
        var response = await fetch("/api/convert/markup", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({direction: "markdown-to-html", text: markdown})
        });
        var result = await response.json();
        if (!response.ok) throw new Error(result.detail || "HTML conversion failed.");
        return result.output;
    }

    document.addEventListener("click", async function (event) {
        var button = event.target.closest("[data-copy-output]");
        if (!button) return;
        var panel = button.closest("[data-output-panel]");
        var status = panel.querySelector("[data-copy-status]");
        var markdown = panel.querySelector("[data-output-text]").textContent;
        button.disabled = true;
        status.textContent = "Copying…";
        try {
            if (!navigator.clipboard) {
                throw new Error("Clipboard unavailable. Use HTTPS or localhost, or select and copy the text manually.");
            }
            if (button.dataset.copyOutput === "html") {
                if (window.ClipboardItem && navigator.clipboard.write) {
                    var html = htmlText(markdown).then(function (text) {
                        return new Blob([text], {type: "text/plain"});
                    });
                    await navigator.clipboard.write([new ClipboardItem({"text/plain": html})]);
                } else {
                    await navigator.clipboard.writeText(await htmlText(markdown));
                }
            } else {
                await navigator.clipboard.writeText(markdown);
            }
            status.textContent = "Copied";
        } catch (error) {
            status.textContent = "Copy failed: " + error.message;
        } finally {
            button.disabled = false;
        }
    });
})();
