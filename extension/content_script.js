// Insta Poster — content_script (roda em main world em instagram.com)
//
// Spoofa navigator.* / screen.* / Intl / etc baseado no fingerprint da conta
// ativa (lido de chrome.storage.local via sessionStorage bridge).
//
// LIMITAÇÕES: Canvas / WebGL / AudioContext / fonts NÃO são spoofados — precisa
// patched Chromium (Dolphin Anty). Esses leak hardware real. Spoofar UA + screen
// + lang + timezone já reduz bem o clustering, mas não é 100%.
//
// Roda em document_start pra interceptar antes da página executar JS dela.

(function () {
  "use strict";

  // Bridge: extensão escreve o fingerprint em chrome.storage.local que aparece
  // pra content scripts via chrome.storage. Service worker da extensão escreve
  // o fingerprint da conta ativa toda vez que applyProxy() é chamado.
  // Aqui no MAIN world, NÃO temos acesso direto a chrome.* APIs.
  // Mas como esse script roda no MAIN world por config no manifest (world: MAIN),
  // ele precisa receber o fingerprint via outro mecanismo.
  //
  // Solução: a extensão injeta um <meta name="ip-fp" content="<base64 json>">
  // no head DOM, e este script lê de lá. Ou usa window.postMessage.
  //
  // Implementação aqui: lê window.__IP_FP que outro script (isolated world
  // companion) escreve via postMessage event listener.

  let activeFingerprint = null;

  // Captura o fingerprint que foi colocado em window.name pela extensão
  // ANTES da página carregar.
  // (Outra abordagem: o popup injeta um setupScript com chrome.scripting.executeScript
  // que escreve em window.__IP_FP antes da página carregar)
  try {
    const fromName = window.name;
    if (fromName && fromName.startsWith("__IP_FP:")) {
      const json = fromName.slice("__IP_FP:".length);
      activeFingerprint = JSON.parse(decodeURIComponent(json));
      // Limpa window.name pra não vazar pra outros sites
      window.name = "";
    }
  } catch (e) { /* silencioso */ }

  // Tenta também via storage event/postMessage como fallback
  window.addEventListener("message", function (event) {
    if (event.source !== window) return;
    if (event.data && event.data.type === "__IP_FP_SET" && event.data.payload) {
      activeFingerprint = event.data.payload;
      applyFingerprint();
    }
  });

  function applyFingerprint() {
    if (!activeFingerprint) return;
    const fp = activeFingerprint;

    try {
      // === navigator ===
      if (fp.user_agent) {
        Object.defineProperty(navigator, "userAgent", { value: fp.user_agent, configurable: true });
        Object.defineProperty(navigator, "appVersion", {
          value: fp.user_agent.replace(/^Mozilla\//, ""),
          configurable: true,
        });
      }
      if (fp.platform) {
        Object.defineProperty(navigator, "platform", { value: fp.platform, configurable: true });
      }
      if (fp.language) {
        Object.defineProperty(navigator, "language", { value: fp.language, configurable: true });
      }
      if (fp.languages && Array.isArray(fp.languages)) {
        Object.defineProperty(navigator, "languages", { value: Object.freeze(fp.languages.slice()), configurable: true });
      }
      if (fp.hardware_concurrency) {
        Object.defineProperty(navigator, "hardwareConcurrency", { value: fp.hardware_concurrency, configurable: true });
      }
      if (fp.device_memory_gb) {
        Object.defineProperty(navigator, "deviceMemory", { value: fp.device_memory_gb, configurable: true });
      }
      if (fp.vendor) {
        Object.defineProperty(navigator, "vendor", { value: fp.vendor, configurable: true });
      }

      // === userAgentData (Chrome 90+ Sec-CH-UA) ===
      // Não tenta spoofar — propriedade complexa, deixar como está
      // (declarativeNetRequest do background.js modifica headers Sec-CH-UA-* no nível HTTP)

      // === screen ===
      if (fp.screen) {
        const s = fp.screen;
        if (s.width) Object.defineProperty(screen, "width", { value: s.width, configurable: true });
        if (s.height) Object.defineProperty(screen, "height", { value: s.height, configurable: true });
        if (s.avail_width) Object.defineProperty(screen, "availWidth", { value: s.avail_width, configurable: true });
        if (s.avail_height) Object.defineProperty(screen, "availHeight", { value: s.avail_height, configurable: true });
        if (s.color_depth) Object.defineProperty(screen, "colorDepth", { value: s.color_depth, configurable: true });
        if (s.pixel_depth) Object.defineProperty(screen, "pixelDepth", { value: s.pixel_depth, configurable: true });
      }

      // === devicePixelRatio ===
      if (fp.device_pixel_ratio) {
        Object.defineProperty(window, "devicePixelRatio", { value: fp.device_pixel_ratio, configurable: true });
      }

      // === Timezone (Intl.DateTimeFormat) ===
      if (fp.timezone) {
        try {
          const OrigDTF = Intl.DateTimeFormat;
          const origResolved = OrigDTF.prototype.resolvedOptions;
          Intl.DateTimeFormat.prototype.resolvedOptions = function () {
            const r = origResolved.call(this);
            r.timeZone = fp.timezone;
            return r;
          };
        } catch (e) { /* não crítico */ }
      }
      if (typeof fp.timezone_offset_minutes === "number") {
        // Date.getTimezoneOffset retorna em minutos
        // Insta usa pra ver onde o user tá
        const origGetTzOffset = Date.prototype.getTimezoneOffset;
        Date.prototype.getTimezoneOffset = function () {
          return -fp.timezone_offset_minutes;  // sinal invertido (positivo se atrás de UTC)
        };
      }

      // Debug: deixa marcador (não imprime logs pra não chamar atenção)
      window.__ip_fp_applied = true;
    } catch (e) {
      // Falha de spoofing — não bloqueia carregamento da página
      console.warn("[InstaPoster ext] fingerprint apply falhou:", e.message);
    }
  }

  // Aplica imediato se já tem fingerprint (capturado de window.name)
  if (activeFingerprint) {
    applyFingerprint();
  }
})();
