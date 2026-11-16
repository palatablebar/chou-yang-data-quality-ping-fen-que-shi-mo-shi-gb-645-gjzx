/* 回传核对：上传名单 + 附件，成功后跳到同一批次的新版本；失败就地提示。 */
(function () {
  "use strict";

  const state = { busy: false };

  function byId(id) {
    return document.getElementById(id);
  }

  function status(text, kind) {
    const node = byId("form-status");
    if (!node) return;
    node.textContent = text;
    node.className = "status" + (kind ? " status--" + kind : "");
  }

  function busy(disabled) {
    [byId("upload-btn"), byId("sample-btn")].forEach(function (button) {
      if (button) button.disabled = disabled;
    });
  }

  async function send(url, formData) {
    const response = await fetch(url, { method: "POST", body: formData });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const message = payload.message || payload.detail || response.statusText;
      throw new Error(message + "（HTTP " + response.status + "）");
    }
    return payload;
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const message = payload.message || payload.detail || response.statusText;
      throw new Error(message + "（HTTP " + response.status + "）");
    }
    return payload;
  }

  function form() {
    return byId("returns-form");
  }

  async function submit(useSample) {
    if (state.busy) return;
    const node = form();
    if (!node) return;
    state.busy = true;
    busy(true);
    status(useSample ? "载入回传样例中…" : "核对中…");
    try {
      let payload;
      if (useSample) {
        payload = await postJSON("/api/returns/check", {
          batch: node.getAttribute("data-batch") || "",
          sample: true
        });
      } else {
        const data = new FormData(node);
        data.delete("roster");
        data.delete("attachment");
        const roster = byId("roster-input");
        const attachment = byId("attachment-input");
        if (roster && roster.files.length) data.append("roster", roster.files[0]);
        if (attachment && attachment.files.length) {
          data.append("attachment", attachment.files[0]);
        }
        payload = await send("/api/returns/upload", data);
      }
      status("完成，正在刷新…", "good");
      window.location.assign("/returns?batch=" +
        encodeURIComponent(payload.run.batch));
    } catch (error) {
      status(error.message, "bad");
      busy(false);
    } finally {
      state.busy = false;
    }
  }

  function bind() {
    const node = form();
    if (!node) return;
    node.addEventListener("submit", function (event) {
      event.preventDefault();
      submit(false);
    });
    const sample = byId("sample-btn");
    if (sample) {
      sample.addEventListener("click", function () { submit(true); });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
