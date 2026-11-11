"use strict";

(() => {
  const storageKey = "foxtube.player.v1";
  const rates = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  const formatTime = (time) => {
    const value = Number.isFinite(time) ? Math.max(0, Math.floor(time)) : 0;
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const seconds = String(value % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
  };
  const readPreferences = () => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey));
      if (!value || typeof value !== "object") return {};
      return { volume: value.volume, muted: value.muted, rate: value.rate,
        loop: value.loop, theater: value.theater, captions: value.captions === true,
        captionLanguage: typeof value.captionLanguage === "string" ? value.captionLanguage.slice(0, 35) : "" };
    } catch { return {}; }
  };

  const instances = new WeakMap();
  const initialize = (container = document) => {
  for (const root of container.querySelectorAll("[data-fox-player]")) {
    if (instances.has(root)) continue;
    const get = (name) => root.querySelector(`[data-fox-${name}]`);
    const video = root.querySelector("video");
    const controls = get("controls");
    const center = get("center");
    const toggle = get("toggle");
    const mute = get("mute");
    const volume = get("volume");
    const seek = get("seek");
    const fullscreen = get("fullscreen");
    const loading = get("loading");
    const message = get("message");
    const settings = get("settings");
    const panel = get("settings-panel");
    const speed = get("speed");
    const quality = get("quality");
    const captions = get("captions");
    const captionTrack = get("caption-track");
    const captionFile = get("caption-file");
    const loop = get("loop");
    const theater = get("theater");
    const pip = get("pip");
    const feedback = get("feedback");
    const seekPreview = get("seek-preview");
    if (!(video instanceof HTMLVideoElement) || [controls, center, toggle, mute, volume, seek,
      fullscreen, loading, message, settings, panel, speed, quality, captions, captionTrack,
      captionFile, loop, theater, pip, feedback, seekPreview].some((element) => !element)) continue;

    const listeners = new AbortController();
    const listen = (element, event, handler) => element.addEventListener(event, handler, { signal: listeners.signal });
    const preferences = root.dataset.preserveState === "true"
      ? { volume: video.volume, muted: video.muted, rate: video.playbackRate, loop: video.loop }
      : readPreferences();
    let hideTimer, feedbackTimer, clickTimer, touchTimer;
    let keyboardFocus = false;
    let toolbarHover = false;
    let scrubbing = false;
    let waiting = false;
    let previousVolume = 1;
    let playPending = false;
    let playAttempt = 0;
    let touchStart;
    let previousTap;
    let ignoreMouseUntil = 0;
    let localTrack;
    let pendingLocalTrack;
    let captionAttempt = 0;
    let selectedCaption;
    let pendingQuality;
    let captionResize;
    const captionLines = new WeakMap();
    const observedTracks = new WeakSet();
    // A newly pinned preview may still be awaiting resource selection. Its
    // initial NETWORK_NO_SOURCE is transient; actual failures emit error.
    const awaitingPreview = root.dataset.preserveState === "true" && !video.currentSrc;
    let mediaFailed = Boolean(video.error) ||
      (!awaitingPreview && video.networkState === HTMLMediaElement.NETWORK_NO_SOURCE);
    const variants = Array.from(video.querySelectorAll("source")).filter((source) => {
      try { return source.src && new URL(source.src).origin === location.origin; }
      catch { return false; }
    });
    const savePreferences = () => {
      Object.assign(preferences, { volume: video.volume, muted: video.muted,
        rate: pendingQuality?.rate ?? video.playbackRate, loop: video.loop,
        theater: root.classList.contains("is-theater") });
      try { localStorage.setItem(storageKey, JSON.stringify(preferences)); } catch { /* Storage may be disabled. */ }
    };
    const showMessage = (text) => {
      message.textContent = text;
      message.hidden = !text;
    };
    const positionCaptions = () => {
      for (const track of video.textTracks) {
        if (track.mode !== "showing") continue;
        for (const cue of track.activeCues || []) {
          if (!captionLines.has(cue)) captionLines.set(cue, cue.line);
          if (captionLines.get(cue) === "auto") {
            const lineHeight = Math.max(10, video.clientHeight * 0.05 * 1.2);
            const clearance = Math.max(4, Math.ceil((controls.offsetHeight + 12) / lineHeight));
            cue.line = root.classList.contains("is-controls-hidden") ? -1 : -clearance;
          }
        }
      }
    };
    const notify = (text) => {
      clearTimeout(feedbackTimer);
      feedback.textContent = text;
      feedback.hidden = false;
      feedbackTimer = setTimeout(() => { feedback.hidden = true; }, 1100);
    };
    const reveal = () => {
      clearTimeout(hideTimer);
      root.classList.remove("is-controls-hidden");
      controls.inert = false;
      positionCaptions();
    };
    const keepControls = () => video.paused || video.ended || mediaFailed || video.error ||
      keyboardFocus || toolbarHover || scrubbing || !panel.hidden || !message.hidden;
    const scheduleHide = () => {
      reveal();
      if (keepControls()) return;
      hideTimer = setTimeout(() => {
        if (keepControls()) return;
        // Pointer focus must not remain inside an inert subtree.
        if (controls.contains(document.activeElement)) root.focus({ preventScroll: true });
        root.classList.add("is-controls-hidden");
        controls.inert = true;
        seekPreview.hidden = true;
        positionCaptions();
      }, 2500);
    };
    const closeSettings = (restoreFocus = false) => {
      panel.hidden = true;
      settings.setAttribute("aria-expanded", "false");
      if (restoreFocus) settings.focus({ preventScroll: true });
      scheduleHide();
    };
    const updateProgress = () => {
      const duration = Number.isFinite(video.duration) ? video.duration : 0;
      const current = video.currentTime || 0;
      seek.max = String(duration);
      if (!scrubbing) seek.value = String(current);
      seek.disabled = !duration || Boolean(mediaFailed || video.error);
      seek.setAttribute("aria-valuetext", `${formatTime(current)} из ${formatTime(duration)}`);
      get("current").textContent = formatTime(current);
      get("duration").textContent = formatTime(duration);
      get("progress").style.width = `${duration ? Math.min(100, current / duration * 100) : 0}%`;
      let buffered = 0;
      for (let i = 0; i < video.buffered.length; i += 1) {
        if (video.buffered.start(i) <= current && video.buffered.end(i) >= current) buffered = video.buffered.end(i);
      }
      get("buffer").style.width = `${duration ? Math.min(100, buffered / duration * 100) : 0}%`;
    };
    const updatePlayback = () => {
      const paused = pendingQuality ? !pendingQuality.resume : video.paused || video.ended;
      const label = paused ? (video.ended ? "Смотреть заново" : "Воспроизвести видео") : "Приостановить видео";
      toggle.setAttribute("aria-label", label);
      toggle.title = `${label} (K)`;
      center.setAttribute("aria-label", video.ended ? "Смотреть заново" : video.currentTime > 0 ? "Продолжить просмотр" : "Воспроизвести видео");
      center.hidden = !paused || Boolean(mediaFailed || video.error) || waiting;
      get("play-icon").toggleAttribute("hidden", !paused);
      get("pause-icon").toggleAttribute("hidden", paused);
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
      get("sound-icon").toggleAttribute("hidden", muted);
      get("muted-icon").toggleAttribute("hidden", !muted);
      get("unmute").hidden = !muted;
      if (video.volume > 0) previousVolume = video.volume;
    };
    const play = async () => {
      const attempt = ++playAttempt;
      playPending = true;
      showMessage("");
      try { await video.play(); }
      catch (error) {
        if (attempt === playAttempt && error?.name !== "AbortError") {
          if (error?.name === "NotAllowedError" && !video.muted) {
            video.muted = true;
            try { await video.play(); }
            catch { if (attempt === playAttempt) showMessage("Не удалось начать просмотр. Нажмите воспроизведение, чтобы продолжить."); }
          } else showMessage("Не удалось начать просмотр. Нажмите воспроизведение ещё раз или обновите страницу.");
        }
      } finally {
        if (attempt === playAttempt) {
          playPending = false;
          waiting = false;
          updatePlayback();
        }
      }
    };
    const togglePlayback = () => {
      if (mediaFailed || video.error) return;
      if (pendingQuality) {
        pendingQuality.resume = !pendingQuality.resume;
        waiting = pendingQuality.resume;
        updatePlayback();
        return;
      }
      if (!video.paused || playPending) {
        playAttempt += 1;
        video.pause();
        playPending = false;
        waiting = false;
        updatePlayback();
      } else {
        if (video.ended) video.currentTime = 0;
        play();
      }
    };
    const toggleMute = () => {
      if (video.muted || video.volume === 0) {
        if (video.volume === 0) video.volume = previousVolume || 1;
        video.muted = false;
      } else video.muted = true;
      notify(video.muted ? "Без звука" : `Громкость ${Math.round(video.volume * 100)}%`);
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
    const seekTo = (position, announce = false) => {
      if (!Number.isFinite(video.duration) || !video.duration || mediaFailed || video.error) return;
      video.currentTime = Math.max(0, Math.min(video.duration, position));
      updateProgress();
      if (announce) notify(formatTime(video.currentTime));
    };
    const setSpeed = (rate) => {
      if (!rates.includes(rate)) return;
      video.defaultPlaybackRate = rate;
      video.playbackRate = rate;
      notify(`Скорость ${rate}×`);
    };
    const changeSpeed = (step) => setSpeed(rates[Math.max(0, Math.min(rates.length - 1, rates.indexOf(video.playbackRate) + step))]);
    const toggleTheater = () => {
      root.classList.toggle("is-theater");
      root.closest(".watch-page")?.classList.toggle("is-theater", root.classList.contains("is-theater"));
      theater.setAttribute("aria-pressed", String(root.classList.contains("is-theater")));
      savePreferences();
    };
    const togglePip = async () => {
      if (pip.hidden) return;
      try {
        if (document.pictureInPictureElement === video) await document.exitPictureInPicture();
        else await video.requestPictureInPicture();
      } catch { notify("Картинка в картинке недоступна"); }
    };
    const subtitleTracks = () => Array.from(video.textTracks).filter((track) =>
      ["captions", "subtitles"].includes(track.kind) && track !== pendingLocalTrack?.element.track);
    const updateCaptions = () => {
      const tracks = subtitleTracks();
      const active = tracks.findIndex((track) => track.mode === "showing");
      for (const track of tracks) {
        if (!observedTracks.has(track)) {
          observedTracks.add(track);
          track.addEventListener("cuechange", positionCaptions);
        }
      }
      captionTrack.replaceChildren(new Option(tracks.length ? "Выключены" : "Нет дорожек", "off"),
        ...tracks.map((track, index) => new Option(track.label || track.language || `Дорожка ${index + 1}`, String(index))));
      captionTrack.value = active < 0 ? "off" : String(active);
      captionTrack.disabled = captions.disabled = !tracks.length;
      captions.setAttribute("aria-pressed", String(active >= 0));
      captions.setAttribute("aria-label", active >= 0 ? "Выключить субтитры" : "Включить субтитры");
      if (active >= 0) selectedCaption = tracks[active];
      positionCaptions();
    };
    const selectCaption = (track) => {
      for (const item of subtitleTracks()) item.mode = item === track ? "showing" : "disabled";
      preferences.captions = Boolean(track);
      if (track && track !== localTrack?.element.track) preferences.captionLanguage = track.language;
      savePreferences();
      updateCaptions();
    };
    const toggleCaptions = () => {
      const tracks = subtitleTracks();
      if (!tracks.length) { notify("Откройте файл .vtt в настройках"); return; }
      selectCaption(tracks.some((track) => track.mode === "showing") ? undefined : tracks.includes(selectedCaption) ? selectedCaption : tracks[0]);
    };
    const disposeTrack = (track) => {
      if (!track) return;
      track.controller.abort();
      track.element.track.removeEventListener("cuechange", positionCaptions);
      track.element.remove();
      URL.revokeObjectURL(track.url);
    };
    const loadCaptions = async () => {
      const file = captionFile.files?.[0];
      captionFile.value = "";
      if (!file) return;
      const attempt = ++captionAttempt;
      disposeTrack(pendingLocalTrack);
      pendingLocalTrack = undefined;
      if (file.size > 1024 * 1024 || !/\.vtt$/i.test(file.name)) {
        notify("Нужен файл .vtt до 1 МиБ");
        return;
      }
      try {
        const text = await file.text();
        if (attempt !== captionAttempt) return;
        if (!/^\uFEFF?WEBVTT(?:[ \t][^\r\n]*)?(?:\r?\n|$)/.test(text)) {
          notify("Не удалось прочитать WebVTT");
          return;
        }
        const element = document.createElement("track");
        element.kind = "subtitles";
        element.label = "С устройства";
        const url = URL.createObjectURL(new Blob([text], { type: "text/vtt" }));
        const candidate = { element, url, controller: new AbortController() };
        pendingLocalTrack = candidate;
        const failed = () => {
          if (pendingLocalTrack !== candidate) return;
          pendingLocalTrack = undefined;
          disposeTrack(candidate);
          updateCaptions();
          notify("В файле нет читаемых субтитров");
        };
        element.addEventListener("load", () => {
          if (pendingLocalTrack !== candidate) return;
          if (!element.track.cues?.length) { failed(); return; }
          disposeTrack(localTrack);
          localTrack = candidate;
          pendingLocalTrack = undefined;
          candidate.controller.abort();
          selectCaption(element.track);
          notify("Субтитры включены");
        }, { signal: candidate.controller.signal });
        element.addEventListener("error", failed, { signal: candidate.controller.signal });
        element.src = url;
        video.append(element);
        // A disabled track is not fetched; hidden loads cues without displaying it yet.
        element.track.mode = "hidden";
      } catch { notify("Не удалось открыть субтитры"); }
    };
    const updateQuality = () => {
      if (variants.length > 1) return;
      quality.replaceChildren(new Option(video.videoHeight ? `${video.videoHeight}p · единственный вариант` : "Исходное · единственный вариант", "0"));
      quality.disabled = true;
    };
    const switchQuality = () => {
      const source = variants[Number(quality.value)];
      if (!source || variants.length < 2) return;
      const resume = pendingQuality?.resume ?? (!video.paused || playPending);
      const position = pendingQuality?.position ?? video.currentTime;
      pendingQuality = { resume, position, rate: pendingQuality?.rate ?? video.playbackRate };
      playAttempt += 1;
      playPending = false;
      mediaFailed = false;
      waiting = resume;
      showMessage("");
      video.src = source.src;
      video.load();
      updatePlayback();
    };

    try {
      // iOS can expose volume while leaving it controlled only by hardware.
      const initialVolume = video.volume;
      const probeVolume = initialVolume === 0.5 ? 0.6 : 0.5;
      video.volume = probeVolume;
      volume.hidden = Math.abs(video.volume - probeVolume) > 0.01;
      video.volume = initialVolume;
      if (typeof preferences.volume === "number" && Number.isFinite(preferences.volume)) video.volume = Math.min(1, Math.max(0, preferences.volume));
      video.muted = preferences.muted === true;
      if (rates.includes(preferences.rate)) {
        video.defaultPlaybackRate = preferences.rate;
        video.playbackRate = preferences.rate;
      }
      video.loop = preferences.loop === true;
      loop.checked = video.loop;
      speed.value = String(video.playbackRate);
      if (preferences.theater === true) toggleTheater();
      if (variants.length > 1) {
        quality.replaceChildren(...variants.map((source, index) => new Option(source.dataset.quality || `Вариант ${index + 1}`, String(index))));
        quality.disabled = false;
      }
      listen(root, "foxtube:player-mode", () => {
        clearTimeout(clickTimer); clearTimeout(touchTimer);
        previousTap = undefined;
        closeSettings(); updateProgress(); updatePlayback();
      });
      listen(get("unmute"), "click", () => {
        if (video.volume === 0) video.volume = previousVolume || 1;
        video.muted = false;
        if (video.paused) play();
      });
      listen(center, "click", togglePlayback);
      listen(toggle, "click", togglePlayback);
      // Cancel on the second press: its mouseup can arrive after the single-click
      // delay when the user holds the button during a double click.
      listen(video, "mousedown", (event) => {
        if (event.detail > 1) clearTimeout(clickTimer);
      });
      listen(video, "click", (event) => {
        if (performance.now() < ignoreMouseUntil) return;
        root.focus({ preventScroll: true });
        clearTimeout(clickTimer);
        if (event.detail < 2) clickTimer = setTimeout(togglePlayback, 500);
      });
      listen(video, "dblclick", (event) => {
        if (performance.now() < ignoreMouseUntil) return;
        event.preventDefault();
        clearTimeout(clickTimer);
        toggleFullscreen();
      });
      listen(video, "pointerdown", (event) => {
        if (event.pointerType === "touch") {
          ignoreMouseUntil = performance.now() + 900;
          touchStart = { x: event.clientX, y: event.clientY, hidden: root.classList.contains("is-controls-hidden") };
        }
      });
      listen(video, "pointercancel", () => { touchStart = undefined; });
      listen(video, "pointerup", (event) => {
        if (event.pointerType !== "touch" || !touchStart) return;
        const start = touchStart;
        touchStart = undefined;
        if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > 18) return;
        const bounds = video.getBoundingClientRect();
        const ratio = (event.clientX - bounds.left) / bounds.width;
        const side = ratio < 0.35 ? -1 : ratio > 0.65 ? 1 : 0;
        const now = performance.now();
        clearTimeout(touchTimer);
        if (side && previousTap?.side === side && now - previousTap.time < 320) {
          seekTo(video.currentTime + side * 10, true);
          previousTap = undefined;
        } else {
          previousTap = { side, time: now };
          if (!start.hidden) touchTimer = setTimeout(togglePlayback, 320);
        }
        scheduleHide();
      });
      listen(mute, "click", toggleMute);
      listen(volume, "input", () => { video.volume = Number(volume.value); video.muted = false; });
      listen(seek, "input", () => seekTo(Number(seek.value)));
      listen(seek, "pointerdown", () => { scrubbing = true; reveal(); });
      const finishScrub = () => { if (scrubbing) { scrubbing = false; updateProgress(); scheduleHide(); } };
      listen(window, "pointerup", finishScrub);
      listen(window, "pointercancel", finishScrub);
      listen(seek, "pointermove", (event) => {
        if (!Number.isFinite(video.duration)) return;
        const bounds = seek.getBoundingClientRect();
        const fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
        seekPreview.textContent = formatTime(fraction * video.duration);
        seekPreview.style.left = `${fraction * 100}%`;
        seekPreview.hidden = false;
      });
      listen(seek, "pointerleave", () => { seekPreview.hidden = true; });
      listen(fullscreen, "click", toggleFullscreen);
      listen(document, "fullscreenchange", () => {
        const active = document.fullscreenElement === root;
        fullscreen.setAttribute("aria-label", active ? "Выйти из полноэкранного режима" : "На весь экран");
        fullscreen.title = `${active ? "Выйти из полноэкранного режима" : "На весь экран"} (F)`;
        scheduleHide();
      });
      listen(settings, "click", () => {
        if (!panel.hidden) { closeSettings(); return; }
        panel.hidden = false;
        settings.setAttribute("aria-expanded", "true");
        reveal();
      });
      listen(document, "pointerdown", (event) => { if (!panel.hidden && !panel.contains(event.target) && !settings.contains(event.target)) closeSettings(); });
      listen(speed, "change", () => setSpeed(Number(speed.value)));
      listen(loop, "change", () => { video.loop = loop.checked; savePreferences(); });
      listen(theater, "click", toggleTheater);
      listen(pip, "click", togglePip);
      listen(captions, "click", toggleCaptions);
      listen(captionTrack, "change", () => selectCaption(subtitleTracks()[Number.parseInt(captionTrack.value, 10)]));
      listen(captionFile, "change", loadCaptions);
      listen(quality, "change", switchQuality);
      listen(video.textTracks, "addtrack", () => {
        const tracks = subtitleTracks();
        if (preferences.captions && !tracks.some((track) => track.mode === "showing")) {
          const track = tracks.find((item) => item.language === preferences.captionLanguage) || tracks[0];
          if (track) track.mode = "showing";
        }
        updateCaptions();
      });
      listen(video.textTracks, "removetrack", updateCaptions);
      listen(video.textTracks, "change", updateCaptions);
      for (const event of ["enterpictureinpicture", "leavepictureinpicture"]) listen(video, event, () => {
        const active = document.pictureInPictureElement === video;
        pip.setAttribute("aria-pressed", String(active));
        pip.textContent = active ? "Вернуть из картинки в картинке (I)" : "Картинка в картинке (I)";
      });
      for (const event of ["timeupdate", "durationchange", "progress", "seeked"]) listen(video, event, updateProgress);
      listen(video, "loadedmetadata", () => {
        if (mediaFailed) showMessage("");
        mediaFailed = false;
        updateQuality();
        updateProgress();
        const rate = pendingQuality?.rate ?? preferences.rate;
        video.defaultPlaybackRate = rates.includes(rate) ? rate : 1;
        video.playbackRate = video.defaultPlaybackRate;
        if (pendingQuality) {
          const state = pendingQuality;
          pendingQuality = undefined;
          seekTo(state.position);
          if (state.resume) play();
        }
      });
      for (const event of ["play", "pause", "ended"]) listen(video, event, () => {
        if (video.paused) waiting = false;
        updatePlayback();
      });
      listen(root, "foxtube:play-request", play);
      for (const event of ["waiting", "seeking"]) listen(video, event, () => { waiting = !video.paused; updatePlayback(); });
      for (const event of ["playing", "canplay", "seeked"]) listen(video, event, () => { waiting = false; updatePlayback(); });
      listen(video, "volumechange", () => { updateVolume(); savePreferences(); });
      listen(video, "ratechange", () => { speed.value = String(video.playbackRate); savePreferences(); });
      const mediaError = () => {
        mediaFailed = true;
        pendingQuality = undefined;
        waiting = false;
        showMessage("Не удалось воспроизвести видео. Проверьте соединение и обновите страницу.");
        updateProgress();
        updatePlayback();
      };
      listen(video, "error", mediaError);
      for (const source of video.querySelectorAll("source")) listen(source, "error", () => { if (!video.getAttribute("src")) mediaError(); });
      listen(root, "pointermove", scheduleHide);
      listen(controls, "pointerenter", (event) => { if (event.pointerType !== "touch") { toolbarHover = true; reveal(); } });
      listen(controls, "pointerleave", () => { toolbarHover = false; scheduleHide(); });
      listen(root, "pointerdown", () => { keyboardFocus = false; scheduleHide(); });
      listen(root, "focusin", (event) => {
        if (event.target.matches(":focus-visible")) keyboardFocus = true;
        reveal();
      });
      listen(root, "focusout", (event) => {
        if (!root.contains(event.relatedTarget)) { keyboardFocus = false; closeSettings(); }
        else if (!panel.hidden && !panel.contains(event.relatedTarget) && event.relatedTarget !== settings) closeSettings();
      });
      listen(root, "keydown", (event) => {
        if (event.key === "Escape" && !panel.hidden) {
          event.preventDefault(); event.stopPropagation(); closeSettings(true); return;
        }
        keyboardFocus = true;
        reveal();
        if (event.altKey || event.ctrlKey || event.metaKey || event.target.isContentEditable || event.target.closest("input, textarea, select, summary")) return;
        if ((event.key === " " || event.key === "Enter") && event.target.closest("button")) return;
        const key = event.key.toLowerCase();
        const known = [" ", "k", "j", "l", "m", "f", "c", "t", "i", "arrowleft", "arrowright", "arrowup", "arrowdown", "home", "end", ",", ".", "<", ">"];
        if (!known.includes(key) && !/^[0-9]$/.test(key)) return;
        event.preventDefault();
        if (event.repeat && [" ", "k", "m", "f", "c", "t", "i"].includes(key)) return;
        if (key === " " || key === "k") togglePlayback();
        else if (key === "m") toggleMute();
        else if (key === "f") toggleFullscreen();
        else if (key === "c") toggleCaptions();
        else if (key === "t") toggleTheater();
        else if (key === "i") togglePip();
        else if (key === "arrowleft" || key === "j") seekTo(video.currentTime - (key === "j" ? 10 : 5), true);
        else if (key === "arrowright" || key === "l") seekTo(video.currentTime + (key === "l" ? 10 : 5), true);
        else if (key === "home") seekTo(0, true);
        else if (key === "end") seekTo(video.duration, true);
        else if (/^[0-9]$/.test(key)) seekTo(video.duration * Number(key) / 10, true);
        else if (key === "<" || key === ">" || (event.shiftKey && [",", "."].includes(key))) changeSpeed(key === ">" || key === "." ? 1 : -1);
        else if (key === "," || key === ".") { if (video.paused) seekTo(video.currentTime + (key === "." ? 1 : -1) / 30); }
        else {
          if (volume.hidden) { notify("Используйте кнопки громкости устройства"); return; }
          video.volume = Math.max(0, Math.min(1, video.volume + (key === "arrowup" ? 0.05 : -0.05)));
          video.muted = false;
          notify(`Громкость ${Math.round(video.volume * 100)}%`);
        }
      });
      listen(window, "blur", () => { clearTimeout(clickTimer); clearTimeout(touchTimer); previousTap = undefined; finishScrub(); });
      listen(document, "visibilitychange", () => {
        if (document.hidden) {
          clearTimeout(clickTimer); clearTimeout(touchTimer); clearTimeout(hideTimer);
        } else scheduleHide();
      });
      listen(window, "pagehide", (event) => {
        clearTimeout(clickTimer); clearTimeout(touchTimer); clearTimeout(hideTimer); clearTimeout(feedbackTimer);
        if (!event.persisted) { captionAttempt += 1; disposeTrack(localTrack); disposeTrack(pendingLocalTrack); }
      });
      listen(window, "pageshow", scheduleHide);
      fullscreen.hidden = !(document.fullscreenEnabled && root.requestFullscreen) && !video.webkitEnterFullscreen;
      pip.hidden = !(document.pictureInPictureEnabled && video.requestPictureInPicture);
      updateQuality();
      updateCaptions();
      if (preferences.captions) {
        const tracks = subtitleTracks();
        const track = tracks.find((item) => item.language === preferences.captionLanguage) || tracks[0];
        if (track) selectCaption(track);
      }
      updateProgress();
      updateVolume();
      if (mediaFailed || video.error) showMessage("Не удалось воспроизвести видео. Проверьте соединение и обновите страницу.");
      updatePlayback();
      controls.hidden = false;
      video.controls = false;
      root.classList.add("is-enhanced");
      captionResize = new ResizeObserver(positionCaptions);
      captionResize.observe(video);
      instances.set(root, () => {
        captionAttempt += 1; playAttempt += 1;
        listeners.abort();
        captionResize?.disconnect();
        for (const track of video.textTracks) track.removeEventListener("cuechange", positionCaptions);
        for (const timer of [hideTimer, feedbackTimer, clickTimer, touchTimer]) clearTimeout(timer);
        disposeTrack(localTrack); disposeTrack(pendingLocalTrack);
        instances.delete(root);
      });
    } catch {
      listeners.abort();
      captionResize?.disconnect();
      for (const track of video.textTracks) track.removeEventListener("cuechange", positionCaptions);
      for (const timer of [hideTimer, feedbackTimer, clickTimer, touchTimer]) clearTimeout(timer);
      disposeTrack(localTrack);
      disposeTrack(pendingLocalTrack);
      controls.inert = false;
      root.removeAttribute("aria-busy");
      video.controls = true;
      controls.hidden = panel.hidden = feedback.hidden = center.hidden = loading.hidden = true;
      root.classList.remove("is-enhanced", "is-controls-hidden");
    }
  }
  };
  window.FoxPlayer = { initialize, dispose: (root) => instances.get(root)?.() };
  initialize();
  document.addEventListener("foxtube:page-ready", () => initialize());
})();
