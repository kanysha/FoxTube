"use strict";

(() => {
  const states = {
    UPLOADING: "Ожидает загрузки",
    QUEUED: "В очереди",
    PROCESSING: "Обрабатывается",
    READY: "Готово к просмотру",
    FAILED: "Ошибка обработки",
  };
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  const endpoint = (id) => `/api/v1/videos/${encodeURIComponent(id)}`;
  const messageFor = (status, body) => {
    if (status === 401) return "Сессия истекла. Войдите снова и повторите попытку.";
    if (status === 403) return "Не удалось подтвердить запрос. Обновите страницу и повторите попытку.";
    if (status === 404) return "Видео или пользователь недоступны. Проверьте данные.";
    if (status === 413) return "Файл превышает допустимый размер. Выберите видео до 500 МиБ.";
    if (status === 429) return "Слишком много загрузок. Подождите немного и повторите попытку.";
    if (typeof body?.detail === "string" && /[а-яё]/i.test(body.detail)) return body.detail.slice(0, 400);
    if (status >= 500) return "Сервис временно недоступен. Попробуйте ещё раз немного позже.";
    if (status === 422) return "Проверьте название, описание и выбранный файл.";
    return "Не удалось выполнить запрос. Проверьте соединение и повторите попытку.";
  };
  class RequestError extends Error {
    constructor(status, body) {
      super(messageFor(status, body));
      this.status = status;
    }
  }
  const api = async (url, method = "GET", data) => {
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      if (!csrf) throw new Error("Не удалось подтвердить сессию. Обновите страницу.");
      headers["X-CSRF-Token"] = csrf;
      if (data !== undefined) headers["Content-Type"] = "application/json";
    }
    const response = await fetch(url, {
      method, headers, credentials: "same-origin", mode: "same-origin",
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) throw new RequestError(response.status, body);
    return body;
  };
  const showError = (form, error) => {
    const region = form.querySelector("[data-video-error]");
    if (!region) return;
    region.textContent = error instanceof Error && /[а-яё]/i.test(error.message)
      ? error.message : "Не удалось связаться с сервером. Проверьте соединение и повторите попытку.";
    region.hidden = false;
    region.focus();
  };
  const clearError = (form) => {
    const region = form.querySelector("[data-video-error]");
    if (region) { region.textContent = ""; region.hidden = true; }
  };
  const setBusy = (form, busy) => {
    form.setAttribute("aria-busy", String(busy));
    for (const field of form.querySelectorAll("input, textarea, select, button")) {
      if (!field.matches("[data-upload-cancel]")) field.disabled = busy;
    }
  };

  const uploadForm = document.querySelector("[data-upload-form]");
  if (uploadForm) {
    const fileInput = uploadForm.elements.namedItem("file");
    const titleInput = uploadForm.elements.namedItem("title");
    const progressCard = document.querySelector("[data-upload-progress]");
    const statusLabel = document.querySelector("[data-upload-status]");
    const loader = document.querySelector("[data-upload-loader]");
    const showLoader = (variant) => {
      loader.hidden = !variant;
      loader.className = `fox-loader fox-loader-${variant || "orbit"} upload-loader`;
    };
    const sendProgress = document.querySelector("#send-progress");
    const sendPercent = document.querySelector("[data-send-percent]");
    const note = document.querySelector("[data-processing-note]");
    const watchLink = document.querySelector("[data-upload-watch]");
    const manageLink = document.querySelector("[data-upload-manage]");
    const cancelButton = document.querySelector("[data-upload-cancel]");
    let draftId = null;
    let draftFile = null;
    let xhr = null;
    let sending = false;
    let busy = false;
    let generation = 0;

    fileInput.addEventListener("change", () => {
      if (!titleInput.value.trim() && fileInput.files[0]) {
        titleInput.value = fileInput.files[0].name.replace(/\.[^.]+$/, "").slice(0, 200);
      }
    });
    window.addEventListener("beforeunload", (event) => {
      if (sending) { event.preventDefault(); event.returnValue = ""; }
    });
    const finish = (video) => {
      showLoader(null);
      uploadForm.setAttribute("aria-busy", "false");
      sending = false;
      cancelButton.hidden = true;
      statusLabel.textContent = states[video.status] || "Подготовка видео";
      manageLink.href = `/watch/${encodeURIComponent(video.id)}`;
      manageLink.hidden = false;
      if (video.status === "READY") {
        note.textContent = "Обработка завершена — 100%. Видео готово к просмотру.";
        watchLink.href = `/watch/${encodeURIComponent(video.id)}`;
        watchLink.hidden = false;
      } else {
        note.textContent = video.error || "Не удалось обработать файл. Проверьте формат и ограничения.";
        manageLink.textContent = "Открыть настройки или удалить видео →";
      }
      const another = document.createElement("a");
      another.className = "text-link";
      another.href = "/upload";
      another.textContent = "Загрузить ещё одно видео →";
      progressCard.append(another);
    };
    const monitor = async (id, currentGeneration) => {
      if (generation !== currentGeneration) return;
      try {
        const video = await api(endpoint(id));
        if (generation !== currentGeneration) return;
        statusLabel.textContent = states[video.status] || "Подготовка видео";
        if (["READY", "FAILED"].includes(video.status)) { finish(video); return; }
        note.textContent = video.status === "PROCESSING"
          ? "Файл отправлен. Готовим версию для просмотра; это может занять несколько минут."
          : "Файл отправлен. Ожидаем свободное место в обработке.";
      } catch (error) {
        if ([401, 403, 404].includes(error.status)) {
          busy = false;
          showLoader(null);
          uploadForm.setAttribute("aria-busy", "false");
          cancelButton.hidden = true;
          showError(uploadForm, error);
          return;
        }
        note.textContent = "Связь временно потеряна. Проверяем готовность…";
      }
      if (generation === currentGeneration) window.setTimeout(() => monitor(id, currentGeneration), 2000);
    };
    const sendFile = (id, file) => new Promise((resolve, reject) => {
      xhr = new XMLHttpRequest();
      xhr.open("PUT", `${endpoint(id)}/source`);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-CSRF-Token", csrf || "");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.min(100, Math.round(event.loaded / event.total * 100));
        sendProgress.value = percent;
        sendPercent.textContent = `${percent}%`;
        if (percent === 100) {
          statusLabel.textContent = "Сохраняем файл";
          note.textContent = "Отправка завершена. Ждём подтверждения сервера.";
        }
      });
      xhr.addEventListener("load", () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch { /* Nginx may return HTML. */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new RequestError(xhr.status, body));
      });
      xhr.addEventListener("error", () => reject(new RequestError(0)));
      xhr.addEventListener("abort", () => reject(new Error("Загрузка отменена.")));
      xhr.send(file);
    });

    uploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy || !uploadForm.reportValidity()) return;
      clearError(uploadForm);
      const file = fileInput.files[0];
      if (!file || file.size < 1 || file.size > 500 * 1024 * 1024) {
        showError(uploadForm, new Error("Выберите непустой MP4, MOV или WebM размером до 500 МиБ."));
        return;
      }
      const metadata = {
        title: titleInput.value.trim(),
        description: uploadForm.elements.namedItem("description").value.trim(),
        visibility: uploadForm.elements.namedItem("visibility").value,
      };
      if (!metadata.title) { showError(uploadForm, new Error("Введите название видео.")); return; }
      const currentGeneration = ++generation;
      busy = true;
      setBusy(uploadForm, true);
      progressCard.hidden = false;
      showLoader("orbit");
      cancelButton.hidden = false;
      cancelButton.disabled = true;
      watchLink.hidden = true;
      statusLabel.textContent = "Подготовка к загрузке";
      try {
        if (draftId && draftFile !== file) {
          await api(endpoint(draftId), "DELETE");
          draftId = null;
        }
        if (draftId) await api(endpoint(draftId), "PATCH", metadata);
        else {
          const draft = await api("/api/v1/videos", "POST", { ...metadata, expected_size: file.size });
          draftId = draft.id;
          draftFile = file;
        }
        if (generation !== currentGeneration) {
          if (draftId) await api(endpoint(draftId), "DELETE");
          return;
        }
        sending = true;
        cancelButton.disabled = false;
        statusLabel.textContent = "Отправляем файл";
        note.textContent = "Оставьте страницу открытой до окончания отправки.";
        await sendFile(draftId, file);
        sending = false;
        if (generation !== currentGeneration) return;
        sendProgress.value = 100;
        sendPercent.textContent = "100%";
        statusLabel.textContent = "В очереди";
        showLoader("pulse");
        await monitor(draftId, currentGeneration);
      } catch (error) {
        sending = false;
        if (generation !== currentGeneration) return;
        // A lost response may follow an accepted upload. Check before offering retry.
        if (draftId) {
          try {
            const accepted = await api(endpoint(draftId));
            if (accepted.status !== "UPLOADING") {
              showLoader("pulse");
              await monitor(draftId, currentGeneration);
              return;
            }
          } catch { /* Keep the draft for a full-file retry. */ }
        }
        busy = false;
        setBusy(uploadForm, false);
        cancelButton.hidden = false;
        cancelButton.disabled = false;
        showLoader(null);
        statusLabel.textContent = "Отправка не завершена";
        note.textContent = "Повторите отправку целиком или отмените загрузку.";
        showError(uploadForm, error);
      }
    });

    cancelButton.addEventListener("click", async () => {
      ++generation;
      if (xhr && sending) xhr.abort();
      sending = false;
      cancelButton.disabled = true;
      clearError(uploadForm);
      try {
        if (draftId) await api(endpoint(draftId), "DELETE");
        draftId = null;
        draftFile = null;
        busy = false;
        setBusy(uploadForm, false);
        cancelButton.hidden = true;
        showLoader(null);
        statusLabel.textContent = "Загрузка отменена";
        note.textContent = "Можно выбрать другой файл и начать заново.";
        sendProgress.value = 0;
        sendPercent.textContent = "0%";
      } catch (error) {
        showError(uploadForm, error);
        cancelButton.disabled = false;
        showLoader(null);
        uploadForm.setAttribute("aria-busy", "false");
        note.textContent = "Не удалось отменить загрузку. Проверьте соединение и повторите отмену.";
      }
    });
  }

  const watch = document.querySelector("[data-watch-video]");
  if (watch && ["QUEUED", "PROCESSING"].includes(watch.dataset.videoStatus)) {
    const refreshStatus = async () => {
      try {
        const video = await api(endpoint(watch.dataset.watchVideo));
        if (["READY", "FAILED"].includes(video.status)) { window.location.reload(); return; }
        watch.querySelector("[data-watch-status]").textContent = states[video.status] || "Подготовка видео";
      } catch (error) {
        if ([401, 403, 404].includes(error.status)) {
          watch.querySelector("[data-watch-note]").textContent = error.message;
          watch.querySelector(".fox-loader")?.remove();
          return;
        }
      }
      window.setTimeout(refreshStatus, 2000);
    };
    window.setTimeout(refreshStatus, 2000);
  }
  const ownerPanel = document.querySelector("[data-video-owner]");
  if (ownerPanel) {
    const id = ownerPanel.dataset.videoOwner;
    const editForm = ownerPanel.querySelector("[data-video-edit]");
    const grantForm = ownerPanel.querySelector("[data-video-grant]");
    editForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (editForm.getAttribute("aria-busy") === "true" || !editForm.reportValidity()) return;
      clearError(editForm);
      editForm.querySelector("[data-edit-success]").hidden = true;
      const data = {
        title: editForm.elements.namedItem("title").value.trim(),
        description: editForm.elements.namedItem("description").value.trim(),
        visibility: editForm.elements.namedItem("visibility").value,
      };
      if (!data.title) { showError(editForm, new Error("Введите название видео.")); return; }
      setBusy(editForm, true);
      try {
        await api(endpoint(id), "PATCH", data);
        window.location.reload();
      } catch (error) { showError(editForm, error); setBusy(editForm, false); }
    });
    ownerPanel.querySelector("[data-video-delete]").addEventListener("click", async () => {
      if (!window.confirm("Удалить видео? Оно станет недоступно зрителям. Это действие нельзя отменить.")) return;
      clearError(editForm);
      setBusy(editForm, true);
      try { await api(endpoint(id), "DELETE"); window.location.assign("/studio"); }
      catch (error) { showError(editForm, error); setBusy(editForm, false); }
    });

    const loadGrants = async () => {
      const list = ownerPanel.querySelector("[data-grants-list]");
      const empty = ownerPanel.querySelector("[data-grants-empty]");
      try {
        const result = await api(`${endpoint(id)}/grants`);
        list.replaceChildren();
        for (const person of result.items) {
          const row = document.createElement("li");
          const label = document.createElement("div");
          label.className = "grant-person";
          label.textContent = person.display_name;
          const email = document.createElement("span");
          email.textContent = person.email;
          label.append(email);
          const revoke = document.createElement("button");
          revoke.type = "button";
          revoke.className = "danger-button";
          revoke.textContent = "Отозвать";
          revoke.setAttribute("aria-label", `Отозвать доступ: ${person.email}`);
          revoke.addEventListener("click", async () => {
            revoke.disabled = true;
            revoke.setAttribute("aria-busy", "true");
            clearError(grantForm);
            try { await api(`${endpoint(id)}/grants/${encodeURIComponent(person.user_id)}`, "DELETE"); await loadGrants(); }
            catch (error) { showError(grantForm, error); }
            finally { revoke.disabled = false; revoke.removeAttribute("aria-busy"); }
          });
          row.append(label, revoke);
          list.append(row);
        }
        empty.hidden = result.items.length > 0;
        empty.textContent = "Разрешения пока не выданы.";
      } catch (error) {
        empty.hidden = false;
        empty.textContent = "Не удалось получить список разрешений.";
        showError(grantForm, error);
      }
    };
    grantForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (grantForm.getAttribute("aria-busy") === "true" || !grantForm.reportValidity()) return;
      clearError(grantForm);
      const email = grantForm.elements.namedItem("email").value.trim();
      setBusy(grantForm, true);
      try { await api(`${endpoint(id)}/grants`, "POST", { email }); grantForm.reset(); await loadGrants(); }
      catch (error) { showError(grantForm, error); }
      finally { setBusy(grantForm, false); }
    });
    loadGrants();
  }

  const pendingCards = [...document.querySelectorAll("[data-studio-video]")].filter(
    (card) => ["QUEUED", "PROCESSING"].includes(card.dataset.videoStatus),
  );
  if (pendingCards.length) {
    const refreshCards = async () => {
      let pending = false;
      for (const card of pendingCards) {
        if (!["QUEUED", "PROCESSING"].includes(card.dataset.videoStatus)) continue;
        try {
          const video = await api(endpoint(card.dataset.studioVideo));
          card.dataset.videoStatus = video.status;
          card.querySelector("[data-status-label]").textContent = states[video.status] || video.status;
          if (["READY", "FAILED"].includes(video.status)) {
            const pendingLoader = card.querySelector(".thumbnail-loader");
            if (pendingLoader) {
              const placeholder = document.createElement("span");
              placeholder.className = "thumbnail-placeholder";
              placeholder.setAttribute("aria-hidden", "true");
              placeholder.textContent = video.status === "FAILED" ? "!" : "▶";
              pendingLoader.replaceWith(placeholder);
            }
          }
          if (video.status === "READY" && video.poster_url) {
            const thumbnail = card.querySelector(".video-thumbnail");
            const image = document.createElement("img");
            image.src = video.poster_url;
            image.alt = "";
            image.width = 640;
            image.height = 360;
            thumbnail.replaceChildren(image);
            if (video.duration) {
              const duration = document.createElement("span");
              duration.className = "video-duration";
              const seconds = Math.floor(video.duration);
              duration.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
              thumbnail.append(duration);
            }
            if (video.playback_url) thumbnail.dataset.previewSrc = video.playback_url;
          }
        } catch (error) {
          if ([401, 403, 404].includes(error.status)) { card.dataset.videoStatus = "UNAVAILABLE"; card.querySelector(".thumbnail-loader")?.remove(); card.querySelector("[data-status-label]").textContent = "Недоступно"; }
        }
        if (["QUEUED", "PROCESSING"].includes(card.dataset.videoStatus)) pending = true;
      }
      if (pending) window.setTimeout(refreshCards, 2000);
    };
    window.setTimeout(refreshCards, 2000);
  }
})();
