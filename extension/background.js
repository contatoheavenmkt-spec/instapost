// Insta Poster — background (service worker MV3)
// Funções:
// 1. Ler cookies do instagram.com via chrome.cookies API
// 2. Configurar proxy do Chrome via chrome.proxy.settings (pra IG ver IP do proxy)
// 3. Responder challenge HTTP de auth do proxy (webRequest.onAuthRequired)

const INSTA_DOMAINS = ["instagram.com", ".instagram.com", "www.instagram.com"];

const TARGET_COOKIE_NAMES = [
  "sessionid",
  "ds_user_id",
  "csrftoken",
  "mid",
  "ig_did",
  "rur",
  "datr",
  "shbid",
  "shbts",
  "ig_nrcb",
  "wd",
  "dpr",
];

// Estado do proxy ativo — guarda credenciais pra responder onAuthRequired
let _activeProxy = null;  // { host, port, username, password, account_username }

// =====================================================
// COOKIES
// =====================================================

async function getAllInstaCookies() {
  return new Promise((resolve, reject) => {
    chrome.cookies.getAll({ domain: "instagram.com" }, (cookies) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(cookies || []);
    });
  });
}

function pickCookies(allCookies) {
  const map = {};
  for (const c of allCookies) {
    if (!INSTA_DOMAINS.some(d => c.domain === d || c.domain.endsWith(".instagram.com"))) continue;
    if (map[c.name] && c.hostOnly) continue;
    map[c.name] = c.value;
  }
  const filtered = {};
  for (const name of TARGET_COOKIE_NAMES) {
    if (map[name]) filtered[name] = map[name];
  }
  for (const k of Object.keys(map)) {
    if (k.startsWith("ig_") || k.startsWith("fb")) {
      if (!filtered[k]) filtered[k] = map[k];
    }
  }
  return filtered;
}

// =====================================================
// PROXY
// =====================================================

// Listener de auth do proxy: quando Chrome solicita credenciais (HTTP 407),
// responde com user/pass. CRITICAL: service worker MV3 morre depois de ~30s
// inativo, perdendo _activeProxy em memória. Fallback é ler do storage.
chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {
    if (!details.isProxy) {
      callback({});
      return;
    }
    // Tenta cache em memória primeiro (rápido)
    if (_activeProxy && _activeProxy.username) {
      callback({
        authCredentials: {
          username: _activeProxy.username,
          password: _activeProxy.password || "",
        },
      });
      return;
    }
    // Service worker recém-acordou — busca em storage
    chrome.storage.local.get("active_proxy_full", (data) => {
      const creds = data && data.active_proxy_full;
      if (creds && creds.username) {
        _activeProxy = creds;  // restaura em memória
        callback({
          authCredentials: {
            username: creds.username,
            password: creds.password || "",
          },
        });
      } else {
        // Sem creds — deixa Chrome mostrar dialog (provavelmente erro)
        callback({});
      }
    });
  },
  { urls: ["<all_urls>"] },
  ["asyncBlocking"]
);

async function setProxy(proxyInfo, accountUsername) {
  // proxyInfo: { scheme, host, port, username, password }
  if (!proxyInfo || !proxyInfo.host) {
    throw new Error("proxy_info inválido");
  }
  _activeProxy = {
    ...proxyInfo,
    account_username: accountUsername,
  };
  // Persiste IMEDIATO em storage (service worker pode morrer a qualquer momento)
  await new Promise(r => chrome.storage.local.set({ active_proxy_full: { ..._activeProxy } }, r));

  const scheme = (proxyInfo.scheme || "http").toLowerCase();
  const chromeScheme = scheme === "socks5h" ? "socks5" : scheme;
  const port = parseInt(proxyInfo.port, 10);

  // PAC script: SÓ tráfego pra instagram/cdninstagram/fbcdn passa pelo proxy.
  // Tudo mais (painel instapost.shop, google, etc) vai DIRETO.
  // Isso resolve: 1) requests ao painel não recebem 407 (que travava login),
  // 2) tráfego de outras abas/sites continua normal enquanto proxy "ativo".
  const pacScript = `
    function FindProxyForURL(url, host) {
      var h = host.toLowerCase();
      if (h === "instagram.com" || h.indexOf(".instagram.com") !== -1 ||
          h === "cdninstagram.com" || h.indexOf(".cdninstagram.com") !== -1 ||
          h === "fbcdn.net" || h.indexOf(".fbcdn.net") !== -1) {
        return "${chromeScheme.toUpperCase()} ${proxyInfo.host}:${port}";
      }
      return "DIRECT";
    }
  `;

  return new Promise((resolve, reject) => {
    chrome.proxy.settings.set({
      value: {
        mode: "pac_script",
        pacScript: {
          data: pacScript,
          mandatory: false,
        },
      },
      scope: "regular",
    }, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        chrome.storage.local.set({
          active_proxy_meta: {
            account: accountUsername,
            host: proxyInfo.host,
            port: proxyInfo.port,
            at: Date.now(),
          },
        });
        resolve(true);
      }
    });
  });
}

async function clearProxy() {
  _activeProxy = null;
  return new Promise((resolve, reject) => {
    chrome.proxy.settings.clear({ scope: "regular" }, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        chrome.storage.local.remove("active_proxy_meta");
        resolve(true);
      }
    });
  });
}

async function getProxyStatus() {
  return new Promise((resolve) => {
    chrome.proxy.settings.get({}, (config) => {
      chrome.storage.local.get("active_proxy_meta", (data) => {
        resolve({
          active: config && config.value && config.value.mode === "fixed_servers",
          mode: config?.value?.mode,
          meta: data.active_proxy_meta || null,
        });
      });
    });
  });
}

// Restaura _activeProxy em memória se o service worker foi reiniciado
chrome.storage.local.get("active_proxy_full", (data) => {
  if (data.active_proxy_full) {
    _activeProxy = data.active_proxy_full;
  }
});

// =====================================================
// MESSAGE ROUTER
// =====================================================

// =====================================================
// COOKIE INJECTION (download cookies da VPS e injeta no Chrome)
// =====================================================

async function clearInstaCookies() {
  return new Promise((resolve) => {
    chrome.cookies.getAll({ domain: "instagram.com" }, async (cookies) => {
      if (!cookies || !cookies.length) { resolve(0); return; }
      await Promise.all(cookies.map(c => new Promise(r => {
        const url = `https://${(c.domain || "").replace(/^\./, "")}${c.path || "/"}`;
        chrome.cookies.remove({ url, name: c.name, storeId: c.storeId }, () => r());
      })));
      resolve(cookies.length);
    });
  });
}

async function injectCookies(cookieList) {
  // Limpa cookies antigos do IG antes pra evitar conflito
  const removed = await clearInstaCookies();
  const results = { ok: 0, failed: 0, errors: [], cleared: removed };
  for (const c of cookieList) {
    if (!c.name || !c.value) continue;
    try {
      await new Promise((resolve, reject) => {
        const cookieDef = {
          url: "https://www.instagram.com",
          name: c.name,
          value: String(c.value),
          domain: c.domain || ".instagram.com",
          path: c.path || "/",
          secure: c.secure !== false,
          httpOnly: c.httpOnly === true,
          sameSite: (c.sameSite || "lax").toLowerCase(),
        };
        if (c.expirationDate) cookieDef.expirationDate = c.expirationDate;
        chrome.cookies.set(cookieDef, (cookie) => {
          if (chrome.runtime.lastError) {
            reject(new Error(`${c.name}: ${chrome.runtime.lastError.message}`));
          } else if (!cookie) {
            reject(new Error(`${c.name}: set returned null`));
          } else {
            resolve(cookie);
          }
        });
      });
      results.ok += 1;
    } catch (e) {
      results.failed += 1;
      results.errors.push(e.message);
    }
  }
  return results;
}

// =====================================================
// FINGERPRINT SPOOFING (UA via declarativeNetRequest)
// =====================================================
//
// Override Sec-CH-UA + User-Agent headers no nível HTTP via declarativeNetRequest.
// Complementa o content_script.js que faz override no JS (navigator.userAgent etc).
//
// declarativeNetRequest tem ID estável que substituímos sempre — apenas 1 rule
// ativa pra IG por vez. Quando proxy é limpo, rule também é removido.

const DNR_RULE_ID = 17001;  // ID fixo pra essa rule

async function setUARule(userAgent) {
  if (!userAgent || !chrome.declarativeNetRequest) return;
  try {
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: [DNR_RULE_ID],
      addRules: [{
        id: DNR_RULE_ID,
        priority: 1,
        action: {
          type: "modifyHeaders",
          requestHeaders: [
            { header: "User-Agent", operation: "set", value: userAgent },
          ],
        },
        condition: {
          urlFilter: "instagram.com",
          resourceTypes: [
            "main_frame", "sub_frame", "stylesheet", "script", "image",
            "font", "object", "xmlhttprequest", "ping", "csp_report", "media",
            "websocket", "other",
          ],
        },
      }],
    });
  } catch (e) {
    console.warn("[bg] setUARule falhou:", e.message);
  }
}

async function clearUARule() {
  if (!chrome.declarativeNetRequest) return;
  try {
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: [DNR_RULE_ID],
    });
  } catch (e) {
    console.warn("[bg] clearUARule falhou:", e.message);
  }
}

// =====================================================
// FINGERPRINT TAB — abre IG com window.name = fingerprint
// =====================================================

async function openInstaWithFingerprint(url, fingerprint) {
  // Cria a aba normal primeiro
  const tab = await new Promise((resolve, reject) => {
    chrome.tabs.create({ url, active: true }, (t) => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve(t);
    });
  });
  // Injeta o fingerprint via chrome.scripting (roda ANTES dos scripts da página
  // por causa do run_at: "document_start" no content_script.js do manifest).
  // Aqui usamos uma estratégia complementar: injetar via window.postMessage
  // assim que a aba estiver pronta.
  if (fingerprint) {
    try {
      // Injecta um script que coloca window.name = "__IP_FP:<json>" ANTES da
      // página carregar. content_script.js no MAIN world em document_start vai
      // ler isso. Como tabs.create já navegou, a página pode ter começado a
      // carregar — injetamos no MAIN world via scripting.executeScript no
      // document_start estágio.
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: false },
        world: "MAIN",
        injectImmediately: true,
        func: (fp) => {
          window.__IP_FP_PAYLOAD = fp;
          window.postMessage({ type: "__IP_FP_SET", payload: fp }, "*");
        },
        args: [fingerprint],
      });
    } catch (e) {
      console.warn("[bg] inject fingerprint via executeScript falhou:", e.message);
    }
  }
  return tab;
}

// =====================================================
// MESSAGE ROUTER
// =====================================================

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  (async () => {
    try {
      if (request.action === "getInstaCookies") {
        const all = await getAllInstaCookies();
        const filtered = pickCookies(all);
        sendResponse({ ok: true, cookies: filtered, raw_count: all.length });
        return;
      }
      if (request.action === "applyProxy") {
        await setProxy(request.proxy, request.account_username);
        chrome.storage.local.set({ active_proxy_full: { ..._activeProxy } });
        sendResponse({ ok: true });
        return;
      }
      if (request.action === "clearProxy") {
        await clearProxy();
        chrome.storage.local.remove("active_proxy_full");
        sendResponse({ ok: true });
        return;
      }
      if (request.action === "getProxyStatus") {
        const status = await getProxyStatus();
        sendResponse({ ok: true, ...status });
        return;
      }
      if (request.action === "injectCookies") {
        const r = await injectCookies(request.cookies || []);
        sendResponse({ ok: true, ...r });
        return;
      }
      if (request.action === "clearInstaCookies") {
        const n = await clearInstaCookies();
        sendResponse({ ok: true, cleared: n });
        return;
      }
      if (request.action === "setUARule") {
        await setUARule(request.user_agent);
        sendResponse({ ok: true });
        return;
      }
      if (request.action === "clearUARule") {
        await clearUARule();
        sendResponse({ ok: true });
        return;
      }
      if (request.action === "openInstaWithFingerprint") {
        const tab = await openInstaWithFingerprint(
          request.url || "https://www.instagram.com/",
          request.fingerprint || null,
        );
        sendResponse({ ok: true, tab_id: tab.id });
        return;
      }
      sendResponse({ ok: false, error: "unknown action" });
    } catch (e) {
      sendResponse({ ok: false, error: e.message });
    }
  })();
  return true;
});
