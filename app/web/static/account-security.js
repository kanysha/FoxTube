"use strict";

window.FoxTubePage.register((page) => {
  const forms = document.querySelectorAll("form[data-account-action]");
  if (!forms.length) return;
  const viewer = document.querySelector('meta[name="foxtube-viewer"]')?.content;
  const section = document.querySelector("[data-sessions]");
  const list = section?.querySelector("[data-sessions-list]");
  const status = section?.querySelector("[data-sessions-message]");
  let sessionPage = 1;
  let listAttempt = 0;
  let resetToken = null;
  let resetFinished = false;
  const resetForm = document.querySelector('[data-account-action="reset"]');
  const message = (form, text, success = false) => {
    if (!page.active) return;
    const region = form.querySelector("[data-account-message]");
    region.textContent = text;
    region.hidden = false;
    region.classList.toggle("is-success", success);
  };
  if (resetForm) {
    const params = new URLSearchParams(location.hash.slice(1));
    const candidate = params.get("token");
    // Keep the bearer token only in this page's memory, never in DOM/storage.
    history.replaceState(history.state, "", location.pathname);
    if (candidate && /^[A-Za-z0-9_-]{43}$/.test(candidate)) resetToken = candidate;
    if (!resetToken) {
      message(resetForm, "Ссылка недействительна. Запросите новую ссылку для восстановления.");
      resetForm.querySelector('button[type="submit"]').disabled = true;
    }
    page.cleanup(() => { resetToken = null; });
  }

  const failure = (response, data, action) => {
    if (response.status === 503 && action === "forgot") return "Восстановление пароля пока недоступно. Попробуйте позже.";
    if (response.status === 429) return "Слишком много попыток. Подождите немного и попробуйте снова.";
    if (response.status === 401) return action === "change" ? "Проверьте текущий пароль и повторите попытку." : "Сессия завершена. Войдите снова.";
    if (response.status === 403 && action === "change" && data?.detail === "Текущий пароль указан неверно.") return "Текущий пароль указан неверно.";
    if (response.status === 403) return "Не удалось подтвердить запрос. Обновите страницу и повторите попытку.";
    if (response.status >= 500) return "Сервис временно недоступен. Попробуйте позже.";
    if (typeof data?.detail === "string" && /[а-яё]/i.test(data.detail)) return data.detail.slice(0, 400);
    return action === "reset" ? "Ссылка недействительна или истекла. Запросите новую ссылку." : "Не удалось выполнить запрос. Проверьте данные и повторите попытку.";
  };
  const freshSession = async (authenticated) => {
    const response = await page.fetch("/api/v1/auth/session", { cache: "no-store", credentials: "same-origin", mode: "same-origin" });
    const data = await response.json().catch(() => null);
    page.check();
    if (!response.ok) throw new Error(failure(response, data));
    if (authenticated && (!data?.user || data.user.id !== viewer)) {
      list?.replaceChildren();
      throw new Error("Аккаунт изменился или сеанс завершён. Обновите страницу.");
    }
    if (!/^[a-f0-9]{64}$/.test(data?.csrf_token || "")) throw new Error("Не удалось подтвердить сессию. Обновите страницу.");
    return data;
  };
  const mutate = async (path, payload, authenticated, method = "POST") => {
    const session = await freshSession(authenticated);
    return page.fetch(`/api/v1/auth/${path}`, {
      method, credentials: "same-origin", mode: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
      ...(method !== "DELETE" ? { body: JSON.stringify(payload) } : {}),
    });
  };
  const loadSessions = async () => {
    if (!section) return;
    const attempt = ++listAttempt;
    try {
      await freshSession(true);
      const response = await page.fetch(`/api/v1/auth/sessions?page=${sessionPage}`, { cache: "no-store", credentials: "same-origin", mode: "same-origin" });
      const data = await response.json().catch(() => null);
      page.check();
      if (attempt !== listAttempt) return;
      if (!response.ok) throw new Error(failure(response, data));
      list.replaceChildren();
      for (const session of data.items) {
        const row = document.createElement("li");
        row.dataset.sessionId = session.id;
        const info = document.createElement("div");
        const label = document.createElement("strong");
        label.textContent = session.current ? "Этот сеанс" : "Вход в аккаунт";
        if (session.current) { label.dataset.sessionCurrent = ""; label.className = "session-current"; }
        info.append(label);
        for (const [title, value] of [["Начало", session.created_at], ["Действует до", session.expires_at]]) {
          const time = document.createElement("time");
          time.dateTime = value;
          time.textContent = `${title}: ${new Date(value).toLocaleString("ru-RU")}`;
          info.append(time);
        }
        row.append(info);
        if (!session.current) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "secondary-button";
          button.dataset.sessionRevoke = "";
          button.textContent = "Завершить сеанс";
          page.listen(button, "click", async () => {
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
            try {
              const response = await mutate(`sessions/${encodeURIComponent(session.id)}`, null, true, "DELETE");
              if (!response.ok) throw new Error(failure(response, await response.json().catch(() => null)));
              window.FoxTubePage.sessionChanged();
              await loadSessions();
            } catch (error) { if (page.active) status.textContent = error.message || "Не удалось завершить сеанс."; }
            finally { if (page.active) { button.disabled = false; button.removeAttribute("aria-busy"); } }
          });
          row.append(button);
        }
        list.append(row);
      }
      status.textContent = data.items.length ? "Список активных входов в аккаунт." : "Активных сеансов на этой странице нет.";
      section.querySelector("[data-sessions-previous]").hidden = sessionPage === 1;
      section.querySelector("[data-sessions-next]").hidden = !data.has_more;
    } catch (error) {
      if (page.active && attempt === listAttempt) { list.replaceChildren(); status.textContent = error.message || "Не удалось загрузить сеансы."; }
    }
  };
  for (const [selector, offset] of [["[data-sessions-previous]", -1], ["[data-sessions-next]", 1]]) {
    page.listen(section?.querySelector(selector), "click", () => { sessionPage = Math.max(1, sessionPage + offset); loadSessions(); });
  }
  loadSessions();

  for (const form of forms) {
    let pending = false;
    page.listen(form, "submit", async (event) => {
      event.preventDefault();
      if (pending || (form === resetForm && (!resetToken || resetFinished))) return;
      const action = form.dataset.accountAction;
      const payload = {};
      if (action === "forgot") {
        const email = form.elements.namedItem("email");
        email.value = email.value.trim();
        if (!email.validity.valid) { message(form, "Укажите корректный email."); email.focus(); return; }
        payload.email = email.value;
      } else if (action === "change" || action === "reset") {
        const next = form.elements.namedItem("new_password");
        const confirmation = form.elements.namedItem("confirm_password");
        const length = Array.from(next.value).length;
        if (length < 12 || length > 128) { message(form, "Пароль должен содержать от 12 до 128 символов."); next.focus(); return; }
        if (next.value !== confirmation.value) { message(form, "Пароли не совпадают."); confirmation.focus(); return; }
        payload.new_password = next.value;
        if (action === "change") {
          const current = form.elements.namedItem("current_password");
          if (Array.from(current.value).length < 12 || Array.from(current.value).length > 128) { message(form, "Укажите текущий пароль."); current.focus(); return; }
          payload.current_password = current.value;
        } else payload.token = resetToken;
      }
      const button = form.querySelector('button[type="submit"]');
      pending = true;
      button.disabled = true;
      form.setAttribute("aria-busy", "true");
      try {
        const path = { change: "password/change", forgot: "password/forgot", reset: "password/reset", "revoke-others": "sessions/revoke-others" }[action];
        if (!path) return;
        const response = await mutate(path, payload, action === "change" || action === "revoke-others");
        const data = await response.json().catch(() => null);
        page.check();
        if (!response.ok) { message(form, failure(response, data, action)); return; }
        form.reset();
        if (action === "change") {
          const csrfMeta = document.querySelector('meta[name="csrf-token"]');
          if (csrfMeta && data?.csrf_token) csrfMeta.content = data.csrf_token;
          message(form, "Пароль изменён. Другие сеансы завершены.", true);
          window.FoxTubePage.sessionChanged();
          sessionPage = 1;
          await loadSessions();
        } else if (action === "revoke-others") {
          message(form, "Другие сеансы завершены.", true);
          window.FoxTubePage.sessionChanged();
          sessionPage = 1;
          await loadSessions();
        } else if (action === "forgot") {
          message(form, "Если аккаунт существует, письмо со ссылкой будет отправлено. Проверьте почту и папку «Спам». Если письма нет, попробуйте позже.", true);
        } else {
          resetToken = null;
          resetFinished = true;
          message(form, "Пароль изменён. Все сеансы завершены. Теперь войдите с новым паролем.", true);
          window.FoxTubePage.sessionChanged();
        }
      } catch (error) {
        if (page.active) message(form, error.message || "Не удалось связаться с сервером. Попробуйте снова.");
      } finally {
        if (page.active) {
          pending = false;
          button.disabled = resetFinished;
          form.removeAttribute("aria-busy");
        }
      }
    });
  }
});
