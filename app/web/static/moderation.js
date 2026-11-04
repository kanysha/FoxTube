(() => {
  "use strict";

  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
  const message = (form, value, failed = false) => {
    const output = form.querySelector("[data-moderation-message]");
    if (!output) return;
    output.textContent = value;
    output.classList.toggle("is-error", failed);
    output.hidden = false;
  };
  const request = async (path, method = "GET", payload) => {
    const headers = {"Accept": "application/json"};
    if (payload !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = csrf();
    }
    const response = await fetch(path, {
      method, headers, credentials: "same-origin",
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "Не удалось выполнить действие. Проверьте данные и повторите.";
      throw new Error(detail);
    }
    return data;
  };
  const submit = (form, action) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      if (button.disabled) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      try { await action(new FormData(form)); }
      catch (error) { message(form, error.message || "Ошибка соединения. Повторите позже.", true); }
      finally { button.disabled = false; button.removeAttribute("aria-busy"); }
    });
  };

  document.querySelectorAll("[data-video-report]").forEach((form) => {
    submit(form, async (data) => {
      await request(`/api/v1/videos/${form.dataset.videoReport}/reports`, "POST", {reason: data.get("reason")});
      form.reset();
      message(form, "Жалоба отправлена на проверку.");
    });
  });
  document.querySelectorAll("[data-video-moderation]").forEach((form) => {
    submit(form, async (data) => {
      const video = form.closest("[data-report-card]").dataset.videoId;
      await request(`/api/v1/moderation/videos/${video}`, "PATCH", {status: data.get("status"), reason: data.get("reason")});
      window.location.reload();
    });
  });
  document.querySelectorAll("[data-report-resolution]").forEach((form) => {
    submit(form, async (data) => {
      const report = form.closest("[data-report-card]").dataset.reportId;
      await request(`/api/v1/moderation/reports/${report}/resolve`, "POST", {resolution: data.get("resolution")});
      window.location.reload();
    });
  });
  const lookup = document.querySelector("[data-role-lookup]");
  const roleForm = document.querySelector("[data-role-change]");
  if (lookup && roleForm) {
    lookup.addEventListener("input", () => { roleForm.hidden = true; });
    submit(lookup, async (data) => {
      roleForm.hidden = true;
      const query = new URLSearchParams({[data.get("kind")]: String(data.get("target")).trim()});
      const user = await request(`/api/v1/admin/users?${query}`);
      roleForm.querySelector('[name="user_id"]').value = user.id;
      roleForm.querySelector('[name="role"]').value = user.role;
      roleForm.querySelector('[name="reason"]').value = "";
      roleForm.querySelector("[data-role-target]").textContent = `${user.display_name} · ${user.email} · ${user.id}`;
      roleForm.hidden = false;
      message(lookup, "Аккаунт найден.");
    });
    submit(roleForm, async (data) => {
      await request("/api/v1/admin/roles", "PATCH", {user_id: data.get("user_id"), role: data.get("role"), reason: data.get("reason")});
      window.location.reload();
    });
  }
})();
