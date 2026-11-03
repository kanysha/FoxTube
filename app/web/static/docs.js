"use strict";

window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  displayRequestDuration: true,
  withCredentials: true,
  requestInterceptor: async (request) => {
    if (!["POST", "PUT", "PATCH", "DELETE"].includes((request.method || "GET").toUpperCase())) {
      return request;
    }
    try {
      const response = await fetch("/api/v1/auth/session", {
        credentials: "same-origin",
        mode: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("session_unavailable");
      const session = await response.json();
      if (typeof session.csrf_token !== "string" || !session.csrf_token) {
        throw new Error("session_unavailable");
      }
      request.headers = request.headers || {};
      request.headers["X-CSRF-Token"] = session.csrf_token;
      return request;
    } catch {
      throw new Error("Не удалось подтвердить сессию. Обновите страницу /docs и повторите запрос.");
    }
  },
  validatorUrl: null,
  presets: [SwaggerUIBundle.presets.apis],
  layout: "BaseLayout",
});
