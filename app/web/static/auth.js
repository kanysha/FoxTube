"use strict";

(() => {
  const messages = {
    email: "Укажите корректный email.",
    password: "Пароль должен содержать от 12 до 128 символов.",
    display_name: "Укажите имя от 1 до 80 символов.",
  };

  const showError = (form, message, field) => {
    const region = form.querySelector("[data-auth-error]");
    if (region) {
      region.textContent = message;
      region.hidden = false;
    }
    if (field) {
      field.setAttribute("aria-invalid", "true");
      field.focus();
    } else if (region) {
      region.focus();
    }
  };

  const errorMessage = (status, data, action) => {
    if (status === 401) {
      return action === "login"
        ? "Неверный email или пароль. Проверьте данные и попробуйте ещё раз."
        : "Сессия истекла. Обновите страницу и войдите снова.";
    }
    if (status === 403) {
      return "Не удалось подтвердить запрос. Обновите страницу и повторите попытку.";
    }
    if (status === 409 && action === "register") {
      return "Аккаунт с таким email уже существует. Перейдите ко входу.";
    }
    if (status === 429) {
      return "Слишком много попыток. Подождите немного и попробуйте снова.";
    }
    if (status >= 500) {
      return "Сервис временно недоступен. Попробуйте ещё раз немного позже.";
    }
    if (Array.isArray(data?.detail)) {
      const fieldMessages = data.detail.map((error) => {
        const field = Array.isArray(error.loc) ? error.loc.at(-1) : null;
        return messages[field] || "Проверьте заполненные поля.";
      });
      return [...new Set(fieldMessages)].join(" ");
    }
    if (typeof data?.detail === "string" && /[а-яё]/i.test(data.detail)) {
      return data.detail.slice(0, 400);
    }
    return "Не удалось выполнить запрос. Проверьте данные и попробуйте ещё раз.";
  };

  for (const form of document.querySelectorAll("form[data-auth-form]")) {
    let pending = false;
    form.addEventListener("input", (event) => {
      if (event.target instanceof HTMLInputElement) {
        event.target.removeAttribute("aria-invalid");
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (pending) return;

      const action = form.dataset.authForm;
      if (!["login", "register", "logout"].includes(action)) return;
      const region = form.querySelector("[data-auth-error]");
      if (region) {
        region.textContent = "";
        region.hidden = true;
      }
      const payload = {};
      if (action !== "logout") {
        const email = form.elements.namedItem("email");
        const password = form.elements.namedItem("password");
        if (!(email instanceof HTMLInputElement) || !(password instanceof HTMLInputElement)) return;
        email.value = email.value.trim();
        if (!email.validity.valid) {
          showError(form, messages.email, email);
          return;
        }
        const passwordLength = Array.from(password.value).length;
        if (passwordLength < 12 || passwordLength > 128) {
          showError(form, messages.password, password);
          return;
        }
        payload.email = email.value;
        payload.password = password.value;
        if (action === "register") {
          const name = form.elements.namedItem("display_name");
          if (!(name instanceof HTMLInputElement)) return;
          const displayName = name.value.trim();
          if (Array.from(displayName).length < 1 || Array.from(displayName).length > 80) {
            showError(form, messages.display_name, name);
            return;
          }
          payload.display_name = displayName;
        }
      }

      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
      if (!csrf) {
        showError(form, "Не удалось подтвердить сессию. Обновите страницу и повторите попытку.");
        return;
      }
      const button = form.querySelector('button[type="submit"]');
      const initialLabel = button?.textContent;
      pending = true;
      form.setAttribute("aria-busy", "true");
      if (button) {
        button.disabled = true;
        if (button.dataset.pendingLabel) button.textContent = button.dataset.pendingLabel;
      }

      try {
        const response = await fetch(`/api/v1/auth/${action}`, {
          method: "POST",
          credentials: "same-origin",
          mode: "same-origin",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": csrf,
          },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => null);
          showError(form, errorMessage(response.status, data, action));
          return;
        }
        form.reset();
        window.location.assign(action === "logout" ? "/" : "/account");
      } catch {
        showError(form, "Не удалось связаться с сервером. Проверьте соединение и попробуйте ещё раз.");
      } finally {
        pending = false;
        form.removeAttribute("aria-busy");
        if (button) {
          button.disabled = false;
          if (button.dataset.pendingLabel) button.textContent = initialLabel;
        }
      }
    });
  }
})();
