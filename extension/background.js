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

// Listener de auth do proxy: quando Chrome solicita credenciais do proxy
// (HTTP 407), responde com user/pass armazenados em _activeProxy.
chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {
    if (details.isProxy && _activeProxy && _activeProxy.username) {
      callback({
        authCredentials: {
          username: _activeProxy.username,
          password: _activeProxy.password || "",
        },
      });
      return;
    }
    // Não é proxy ou não temos credenciais — não responde nada
    callback({});
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
  const scheme = (proxyInfo.scheme || "http").toLowerCase();
  // chrome.proxy aceita: 'http', 'https', 'quic', 'socks4', 'socks5'
  const chromeScheme = scheme === "socks5h" ? "socks5" : scheme;
  return new Promise((resolve, reject) => {
    chrome.proxy.settings.set({
      value: {
        mode: "fixed_servers",
        rules: {
          singleProxy: {
            scheme: chromeScheme,
            host: proxyInfo.host,
            port: parseInt(proxyInfo.port, 10),
          },
          // Bypass localhost — evita roubar nossa request ao painel
          bypassList: ["localhost", "127.0.0.1", "<local>"],
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
        // Persiste tb pra sobreviver restart do service worker
        chrome.storage.local.set({
          active_proxy_full: { ..._activeProxy },
        });
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
      sendResponse({ ok: false, error: "unknown action" });
    } catch (e) {
      sendResponse({ ok: false, error: e.message });
    }
  })();
  return true; // async
});
