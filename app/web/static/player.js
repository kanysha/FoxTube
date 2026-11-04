"use strict";

(() => {
  const formatTime = (time) => {
    const value = Number.isFinite(time) ? Math.max(0, Math.floor(time)) : 0;
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const seconds = String(value % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
  };

  for (const root of document.querySelectorAll("[data-fox-player]")) {
    const video = root.querySelector("video");
    const controls = root.querySelector("[data-fox-controls]");
    const center = root.querySelector("[data-fox-center]");
    const toggle = root.querySelector("[data-fox-toggle]");
    const mute = root.querySelector("[data-fox-mute]");
    const volume = root.querySelector("[data-fox-volume]");
    const seek = root.querySelector("[data-fox-seek]");
    const fullscreen = root.querySelector("[data-fox-fullscreen]");
    const loading = root.querySelector("[data-fox-loading]");
    const message = root.querySelector("[data-fox-message]");
    if (!(video instanceof HTMLVideoElement) || !controls || !center || !toggle || !mute || !volume || !seek || !fullscreen || !loading || !message) continue;

    const listeners = new AbortController();
    const listen = (element, event, handler) => element.addEventListener(event, handler, { signal: listeners.signal });
    let hideTimer;
    let keyboardFocus = false;
    let waiting = false;
    let previousVolume = 1;
    let playPending = false;
    let mediaFailed = Boolean(video.error) || video.networkState === HTMLMediaElement.NETWORK_NO_SOURCE;
    const showMessage = (text) => {
      message.textContent = text;
      message.hidden = !text;
    };
    const reveal = () => {
      clearTimeout(hideTimer);
      root.classList.remove("is-controls-hidden");
      controls.inert = false;
    };
    const scheduleHide = () => {
      reveal();
      if (video.paused || video.ended || mediaFailed || video.error || keyboardFocus || !message.hidden) return;
      hideTimer = setTimeout(() => {
        if (video.paused || video.ended || mediaFailed || video.error || keyboardFocus || !message.hidden) return;
        root.classList.add("is-controls-hidden");
        controls.inert = true;
      }, 2500);
    };
    const updateProgress = () => {
      const duration = Number.isFinite(video.duration) ? video.duration : 0;
      const current = video.currentTime || 0;
      seek.max = String(duration);
      seek.value = String(current);
      seek.disabled = !duration || Boolean(mediaFailed || video.error);
      seek.setAttribute("aria-valuetext", `${formatTime(current)} из ${formatTime(duration)}`);
      root.querySelector("[data-fox-current]").textContent = formatTime(current);
      root.querySelector("[data-fox-duration]").textContent = formatTime(duration);
      root.querySelector("[data-fox-progress]").style.width = `${duration ? Math.min(100, current / duration * 100) : 0}%`;
      let buffered = 0;
      for (let i = 0; i < video.buffered.length; i += 1) {
        if (video.buffered.start(i) <= current && video.buffered.end(i) >= current) buffered = video.buffered.end(i);
      }
      root.querySelector("[data-fox-buffer]").style.width = `${duration ? Math.min(100, buffered / duration * 100) : 0}%`;
    };
    const updatePlayback = () => {
      const paused = video.paused || video.ended;
      const label = paused ? (video.ended ? "Смотреть заново" : "Воспроизвести видео") : "Приостановить видео";
      toggle.setAttribute("aria-label", label);
      toggle.title = `${label} (K)`;
      center.setAttribute("aria-label", video.ended ? "Смотреть заново" : video.currentTime > 0 ? "Продолжить просмотр" : "Воспроизвести видео");
      center.hidden = !paused || Boolean(mediaFailed || video.error) || waiting;
      root.querySelector("[data-fox-play-icon]").toggleAttribute("hidden", !paused);
      root.querySelector("[data-fox-pause-icon]").toggleAttribute("hidden", paused);
      loading.hidden = !waiting || Boolean(mediaFailed || video.error);
      root.setAttribute("aria-busy", String(waiting && !(mediaFailed || video.error)));
      if (center.hidden && document.activeElement === center) root.focus({ preventScroll: true });
      scheduleHide();
    };
    const updateVolume = () => {
      const muted = video.muted || video.volume === 0;
      mute.setAttribute("aria-pressed", String(muted));
      mute.setAttribute("aria-label", muted ? "Включить звук" : "Выключить звук");
      mute.title = `${muted ? "Включить" : "Выключить"} звук (M)`;
      volume.value = String(video.muted ? 0 : video.volume);
      volume.setAttribute("aria-valuetext", `${Math.round(Number(volume.value) * 100)}%`);
      root.querySelector("[data-fox-sound-icon]").toggleAttribute("hidden", muted);
      root.querySelector("[data-fox-muted-icon]").toggleAttribute("hidden", !muted);
    };
    const togglePlayback = async () => {
      if (mediaFailed || video.error) return;
      if (!video.paused || playPending) {
        video.pause();
        playPending = false;
        waiting = false;
        updatePlayback();
        return;
      }
      showMessage("");
      if (video.ended) video.currentTime = 0;
      playPending = true;
      try {
        await video.play();
      } catch (error) {
        if (error?.name !== "AbortError") showMessage("Не удалось начать просмотр. Нажмите воспроизведение ещё раз или обновите страницу.");
        waiting = false;
      } finally {
        playPending = false;
        updatePlayback();
      }
    };
    const toggleMute = () => {
      if (video.muted || video.volume === 0) {
        if (video.volume === 0) video.volume = previousVolume || 1;
        video.muted = false;
      } else {
        previousVolume = video.volume;
        video.muted = true;
      }
    };
    const toggleFullscreen = async () => {
      try {
        if (document.fullscreenElement === root) await document.exitFullscreen();
        else if (root.requestFullscreen && document.fullscreenEnabled) await root.requestFullscreen();
        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
      } catch {
        showMessage("Полноэкранный режим недоступен в этом браузере.");
        reveal();
      }
    };
    const seekTo = (position) => {
      if (!Number.isFinite(video.duration) || !video.duration || mediaFailed || video.error) return;
      video.currentTime = Math.max(0, Math.min(video.duration, position));
      updateProgress();
    };

    try {
      listen(center, "click", togglePlayback);
      listen(toggle, "click", togglePlayback);
      listen(video, "click", () => {
        root.focus({ preventScroll: true });
        togglePlayback();
      });
      listen(mute, "click", toggleMute);
      listen(volume, "input", () => {
        video.volume = Number(volume.value);
        video.muted = false;
        if (video.volume > 0) previousVolume = video.volume;
      });
      listen(seek, "input", () => seekTo(Number(seek.value)));
      listen(fullscreen, "click", toggleFullscreen);
      listen(document, "fullscreenchange", () => {
        fullscreen.setAttribute("aria-label", document.fullscreenElement === root ? "Выйти из полноэкранного режима" : "На весь экран");
        scheduleHide();
      });
      for (const event of ["timeupdate", "durationchange", "loadedmetadata", "progress", "seeked"]) listen(video, event, updateProgress);
      for (const event of ["play", "pause", "ended"]) listen(video, event, () => {
        if (video.paused) waiting = false;
        updatePlayback();
      });
      for (const event of ["waiting", "seeking"]) listen(video, event, () => {
        waiting = !video.paused;
        updatePlayback();
      });
      for (const event of ["playing", "canplay", "seeked"]) listen(video, event, () => {
        waiting = false;
        updatePlayback();
      });
      listen(video, "volumechange", updateVolume);
      const mediaError = () => {
        mediaFailed = true;
        waiting = false;
        showMessage("Не удалось воспроизвести видео. Проверьте соединение и обновите страницу.");
        updateProgress();
        updatePlayback();
      };
      listen(video, "error", mediaError);
      // A failed <source> may dispatch error without an error on <video>.
      for (const source of video.querySelectorAll("source")) listen(source, "error", mediaError);
      listen(root, "pointermove", scheduleHide);
      listen(root, "pointerdown", () => {
        keyboardFocus = false;
        scheduleHide();
      });
      listen(root, "focusin", (event) => {
        if (event.target.matches(":focus-visible")) keyboardFocus = true;
        reveal();
      });
      listen(root, "focusout", (event) => {
        if (!root.contains(event.relatedTarget)) {
          keyboardFocus = false;
          scheduleHide();
        }
      });
      listen(root, "keydown", (event) => {
        keyboardFocus = true;
        reveal();
        if (event.altKey || event.ctrlKey || event.metaKey || event.target.isContentEditable) return;
        if (event.target.matches("input, textarea, select")) return;
        if (event.key === " " && event.target.closest("button")) return;
        const key = event.key.toLowerCase();
        if (![" ", "k", "m", "f", "arrowleft", "arrowright", "arrowup", "arrowdown"].includes(key)) return;
        event.preventDefault();
        if (event.repeat && [" ", "k", "m", "f"].includes(key)) return;
        if (key === " " || key === "k") togglePlayback();
        else if (key === "m") toggleMute();
        else if (key === "f") toggleFullscreen();
        else if (key === "arrowleft") seekTo(video.currentTime - 5);
        else if (key === "arrowright") seekTo(video.currentTime + 5);
        else {
          video.volume = Math.max(0, Math.min(1, video.volume + (key === "arrowup" ? 0.05 : -0.05)));
          video.muted = false;
        }
      });
      listen(document, "visibilitychange", () => {
        if (document.hidden) video.pause();
      });
      fullscreen.hidden = !(document.fullscreenEnabled && root.requestFullscreen) && !video.webkitEnterFullscreen;
      updateProgress();
      updateVolume();
      if (mediaFailed || video.error) showMessage("Не удалось воспроизвести видео. Проверьте соединение и обновите страницу.");
      updatePlayback();
      controls.hidden = false;
      video.controls = false;
      root.classList.add("is-enhanced");
    } catch {
      listeners.abort();
      clearTimeout(hideTimer);
      controls.inert = false;
      root.removeAttribute("aria-busy");
      video.controls = true;
      controls.hidden = true;
      center.hidden = true;
      loading.hidden = true;
      root.classList.remove("is-enhanced", "is-controls-hidden");
    }
  }
})();
