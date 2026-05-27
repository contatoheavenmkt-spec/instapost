// Insta Poster — background (service worker MV3)
// Função única: ler cookies do instagram.com via chrome.cookies API.
// Popup NÃO consegue ler diretamente — precisa de service worker com permissão.

const INSTA_DOMAINS = ["instagram.com", ".instagram.com", "www.instagram.com"];

// Cookies essenciais que a API do Insta usa (ordem importa? não, mas seguimos formato instagrapi)
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

async function getAllInstaCookies() {
  // chrome.cookies.getAll com domain ".instagram.com" pega todos
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

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "getInstaCookies") {
    (async () => {
      try {
        const all = await getAllInstaCookies();
        const map = {};
        for (const c of all) {
          // Mantém só cookies do .instagram.com (não de subdomínios de marketing)
          if (!INSTA_DOMAINS.some(d => c.domain === d || c.domain.endsWith(".instagram.com"))) continue;
          // Prioriza HostOnly se houver duplicata
          if (map[c.name] && c.hostOnly) continue;
          map[c.name] = c.value;
        }
        // Filtra apenas cookies relevantes (incluir extras conhecidos não atrapalha)
        const filtered = {};
        for (const name of TARGET_COOKIE_NAMES) {
          if (map[name]) filtered[name] = map[name];
        }
        // Inclui qualquer outro cookie começando com "ig_" ou "fb" pra robustez
        for (const k of Object.keys(map)) {
          if (k.startsWith("ig_") || k.startsWith("fb")) {
            if (!filtered[k]) filtered[k] = map[k];
          }
        }
        sendResponse({ ok: true, cookies: filtered, raw_count: all.length });
      } catch (e) {
        sendResponse({ ok: false, error: e.message });
      }
    })();
    return true; // async response
  }
});
