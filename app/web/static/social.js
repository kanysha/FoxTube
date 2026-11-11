"use strict";

window.FoxTubePage.register((page) => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  const signedIn = Boolean(document.querySelector('[data-auth-form="logout"]'));
  const idPath = (id) => encodeURIComponent(id);
  const api = async (url, method = "GET", data) => {
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      if (!csrf) throw new Error("Обновите страницу и войдите в аккаунт.");
      headers["X-CSRF-Token"] = csrf;
      if (data !== undefined) headers["Content-Type"] = "application/json";
    }
    const response = await page.fetch(url, {
      method, headers, credentials: "same-origin", mode: "same-origin",
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    page.check();
    if (!response.ok) {
      let message = "Не удалось выполнить запрос. Повторите позже.";
      if (response.status === 401) message = "Войдите в аккаунт и повторите действие.";
      else if (response.status === 403) message = "Обновите страницу и повторите действие.";
      else if (response.status === 404) message = "Этот объект больше недоступен.";
      else if (response.status === 429) message = "Слишком много действий. Подождите и повторите позже.";
      else if (response.status === 422) message = "Проверьте текст и выбранные настройки.";
      if (typeof body?.detail === "string" && /[а-яё]/i.test(body.detail)) message = body.detail;
      throw new Error(message);
    }
    return body;
  };
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const errorRegion = (root) => root.querySelector("[data-social-error]");
  const clearError = (root) => {
    if (!page.active) return;
    const region = errorRegion(root);
    if (region) { region.hidden = true; region.textContent = ""; }
  };
  const showError = (root, error) => {
    if (!page.active) return;
    let region = errorRegion(root);
    if (!region) {
      region = element("p", "", "form-message");
      region.dataset.socialError = "";
      region.setAttribute("role", "status");
      root.append(region);
    }
    region.textContent = error instanceof Error && /[а-яё]/i.test(error.message)
      ? error.message : "Не удалось связаться с сервером. Проверьте соединение.";
    region.hidden = false;
  };
  const withButton = async (button, root, action) => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    clearError(root);
    try { await action(); } catch (error) { showError(root, error); }
    finally { if (page.active) { button.disabled = false; button.removeAttribute("aria-busy"); } }
  };

  for (const mount of document.querySelectorAll("[data-channel-subscription]")) {
    const url = `/api/v1/channels/${idPath(mount.dataset.channelSubscription)}/subscription`;
    const render = (state) => {
      if (!page.active) return;
      mount.replaceChildren();
      const row = element("div", undefined, "subscription-control");
      const count = element("span", `${state.subscribers_count} подписчиков`, "form-hint");
      if (state.can_subscribe) {
        const button = element("button", state.subscribed ? "Вы подписаны" : "Подписаться", state.subscribed ? "secondary-button" : "primary-button");
        button.type = "button";
        button.setAttribute("aria-pressed", String(state.subscribed));
        page.listen(button, "click", () => withButton(button, mount, async () => {
          await api(url, state.subscribed ? "DELETE" : "PUT");
          render(await api(url));
        }));
        row.append(button);
      } else if (!signedIn) {
        const login = element("a", "Подписаться", "primary-button");
        login.href = "/login";
        row.append(login);
      } else row.append(element("span", "Ваш канал", "form-hint"));
      row.append(count);
      mount.append(row);
    };
    api(url).then(render).catch((error) => showError(mount, error));
  }

  for (const root of document.querySelectorAll("[data-social-video]")) {
    const videoId = root.dataset.socialVideo;
    const url = `/api/v1/videos/${idPath(videoId)}`;
    const like = root.querySelector("[data-like]");
    const list = root.querySelector("[data-comments-list]");
    const empty = root.querySelector("[data-comments-empty]");
    const more = root.querySelector("[data-comments-more]");
    let liked = false;
    let commentsPage = 1;
    const loadStats = async () => {
      const state = await api(`${url}/social`);
      liked = state.liked;
      root.querySelector("[data-likes-count]").textContent = state.likes_count;
      root.querySelector("[data-comments-count]").textContent = state.comments_count;
      if (like) { like.setAttribute("aria-pressed", String(liked)); like.disabled = false; }
    };
    const loadComments = async (number = 1) => {
      const response = await api(`${url}/comments?page=${number}`);
      if (number === 1) list.replaceChildren();
      for (const comment of response.items) {
        const article = element("article", undefined, "comment");
        article.dataset.commentId = comment.id;
        const heading = element("div", undefined, "comment-heading");
        const author = element("a", comment.author_name, "comment-author");
        author.href = `/channels/${idPath(comment.channel_id)}`;
        const date = element("time", new Date(comment.created_at).toLocaleDateString("ru-RU"));
        date.dateTime = comment.created_at;
        heading.append(author, date);
        article.append(heading, element("p", comment.body, "comment-body"));
        if (comment.can_delete) {
          const button = element("button", "Удалить", "comment-delete");
          button.type = "button";
          page.listen(button, "click", () => withButton(button, root, async () => {
            await api(`/api/v1/comments/${idPath(comment.id)}`, "DELETE");
            await Promise.all([loadComments(), loadStats()]);
          }));
          article.append(button);
        }
        list.append(article);
      }
      commentsPage = number;
      empty.textContent = "Пока нет комментариев. Будьте первым.";
      empty.hidden = response.total > 0;
      more.hidden = number * response.page_size >= response.total;
    };
    loadStats().catch((error) => showError(root, error));
    loadComments().catch((error) => { if (page.active) { empty.hidden = true; showError(root, error); } });
    page.listen(like, "click", () => withButton(like, root, async () => {
      await api(`${url}/like`, liked ? "DELETE" : "PUT");
      await loadStats();
    }));
    page.listen(more, "click", () => withButton(more, root, () => loadComments(commentsPage + 1)));
    const form = root.querySelector("[data-comment-form]");
    page.listen(form, "submit", (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const body = form.elements.namedItem("body");
      withButton(button, root, async () => {
        await api(`${url}/comments`, "POST", { body: body.value });
        body.value = "";
        page.clean(form);
        await Promise.all([loadComments(), loadStats()]);
      });
    });
    const saveToggle = root.querySelector("[data-save-toggle]");
    const saveForm = root.querySelector("[data-save-playlist]");
    page.listen(saveToggle, "click", () => withButton(saveToggle, root, async () => {
      saveForm.hidden = !saveForm.hidden;
      saveToggle.setAttribute("aria-expanded", String(!saveForm.hidden));
      if (saveForm.hidden) return;
      const select = saveForm.elements.namedItem("playlist_id");
      const first = await api("/api/v1/playlists?page_size=50");
      const items = first.items;
      if (first.total > 50) items.push(...(await api("/api/v1/playlists?page_size=50&page=2")).items);
      select.replaceChildren(element("option", items.length ? "Выберите плейлист" : "Создайте плейлист"));
      select.firstElementChild.value = "";
      for (const playlist of items) {
        const option = element("option", playlist.title);
        option.value = playlist.id;
        select.append(option);
      }
    }));
    page.listen(saveForm, "submit", (event) => {
      event.preventDefault();
      const button = saveForm.querySelector('button[type="submit"]');
      withButton(button, root, async () => {
        const playlistId = saveForm.elements.namedItem("playlist_id").value;
        if (!playlistId) throw new Error("Выберите или создайте плейлист.");
        await api(`/api/v1/playlists/${idPath(playlistId)}/items/${idPath(videoId)}`, "PUT");
        page.clean(saveForm);
        const result = saveForm.querySelector("[data-save-result]");
        const link = element("a", "Открыть плейлист", "text-link");
        link.href = `/playlists/${idPath(playlistId)}`;
        result.replaceChildren(document.createTextNode("Видео добавлено. "), link);
        result.hidden = false;
      });
    });
  }

  for (const form of document.querySelectorAll("[data-playlist-create], [data-playlist-edit]")) {
    const playlistId = form.dataset.playlistEdit;
    page.listen(form, "submit", (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      withButton(button, form, async () => {
        const data = Object.fromEntries(new FormData(form));
        const result = await api(playlistId ? `/api/v1/playlists/${idPath(playlistId)}` : "/api/v1/playlists", playlistId ? "PATCH" : "POST", data);
        window.location.assign(`/playlists/${idPath(result.id)}`);
      });
    });
    const remove = form.querySelector("[data-playlist-delete]");
    page.listen(remove, "click", () => {
      if (!window.confirm("Удалить плейлист? Видео останутся на сайте.")) return;
      withButton(remove, form, async () => {
        await api(`/api/v1/playlists/${idPath(playlistId)}`, "DELETE");
        window.location.assign("/playlists");
      });
    });
  }
  for (const root of document.querySelectorAll("[data-playlist-items]")) {
    for (const button of root.querySelectorAll("[data-remove-playlist-video]")) {
      const item = button.closest(".playlist-edit-item");
      page.listen(button, "click", () => withButton(button, item, async () => {
        await api(`/api/v1/playlists/${idPath(root.dataset.playlistItems)}/items/${idPath(button.dataset.removePlaylistVideo)}`, "DELETE");
        window.location.reload();
      }));
    }
  }
});
