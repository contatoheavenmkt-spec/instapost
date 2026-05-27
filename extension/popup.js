// Insta Poster — popup logic
// Lê/grava config em chrome.storage, chama background pra ops com cookies.

const STORAGE_KEYS = ["panel_url", "token", "last_workspace", "last_account"];

async function getConfig() {
  return new Promise(resolve => chrome.storage.local.get(STORAGE_KEYS, resolve));
}
async function setConfig(updates) {
  return new Promise(resolve => chrome.storage.local.set(updates, resolve));
}

function show(viewId) {
  document.querySelectorAll("[id^='view-']").forEach(el => el.classList.add("hidden"));
  document.getElementById(viewId).classList.remove("hidden");
}

function msg(text, kind = "info") {
  const m = document.getElementById("msg");
  m.className = `msg msg-${kind}`;
  m.textContent = text;
  m.classList.remove("hidden");
}

function clearMsg() {
  document.getElementById("msg").classList.add("hidden");
}

async function apiCall(path, opts = {}) {
  const { panel_url, token } = await getConfig();
  if (!panel_url || !token) throw new Error("Configuração faltando");
  const url = panel_url.replace(/\/$/, "") + path;
  const res = await fetch(url, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
      ...(opts.headers || {}),
    },
  });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const err = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
    throw new Error(err);
  }
  return data;
}

async function loadAccountsList() {
  const data = await apiCall("/api/sessions/extension-info");
  const wsSel = document.getElementById("sel-workspace");
  const accSel = document.getElementById("sel-account");
  const wsList = data.workspaces || [];
  const accList = data.accounts || [];

  // Workspaces dropdown
  wsSel.innerHTML = wsList.map(w => `<option value="${w.slug}">${w.name}</option>`).join("");
  if (!wsList.length) {
    wsSel.innerHTML = `<option value="default">default</option>`;
  }

  // Restaura último workspace usado
  const cfg = await getConfig();
  if (cfg.last_workspace && wsList.some(w => w.slug === cfg.last_workspace)) {
    wsSel.value = cfg.last_workspace;
  }

  function renderAccountsForWorkspace() {
    const ws = wsSel.value;
    const filtered = accList.filter(a => a.workspace_slug === ws);
    if (!filtered.length) {
      accSel.innerHTML = `<option value="">— nenhuma conta nesse workspace —</option>`;
      document.getElementById("btn-capture").disabled = true;
      return;
    }
    accSel.innerHTML = filtered.map(a => `<option value="${a.username}">@${a.username}</option>`).join("");
    if (cfg.last_account && filtered.some(a => a.username === cfg.last_account)) {
      accSel.value = cfg.last_account;
    }
    document.getElementById("btn-capture").disabled = false;
  }

  wsSel.onchange = renderAccountsForWorkspace;
  renderAccountsForWorkspace();
  document.getElementById("header-sub").textContent = `Logado: ${data.user_email}`;
}

async function captureAndUpload() {
  const wsSel = document.getElementById("sel-workspace");
  const accSel = document.getElementById("sel-account");
  const ws = wsSel.value;
  const username = accSel.value;
  if (!username) {
    msg("Selecione uma conta primeiro", "error");
    return;
  }

  const btn = document.getElementById("btn-capture");
  const label = document.getElementById("btn-capture-label");
  btn.disabled = true;
  label.innerHTML = '<span class="spinner"></span> Capturando cookies…';
  clearMsg();

  try {
    // Pede ao background pra ler cookies do instagram.com
    const resp = await chrome.runtime.sendMessage({ action: "getInstaCookies" });
    if (!resp || !resp.ok) {
      throw new Error(resp?.error || "Falha lendo cookies do Chrome");
    }
    const cookies = resp.cookies || {};
    if (!cookies.sessionid || !cookies.ds_user_id) {
      throw new Error("Você não está logado no instagram.com neste browser. Abra instagram.com, loga, e tente de novo.");
    }

    label.innerHTML = '<span class="spinner"></span> Enviando ao painel…';

    const result = await apiCall("/api/sessions/upload", {
      method: "POST",
      body: JSON.stringify({
        workspace_slug: ws,
        username,
        cookies,
        user_agent: navigator.userAgent,
      }),
    });

    msg(`✓ Cookies de @${result.username} salvos no workspace "${result.workspace_slug}" (${result.cookies_count} cookies)`, "success");
    await setConfig({ last_workspace: ws, last_account: username });
  } catch (e) {
    msg("Erro: " + e.message, "error");
  } finally {
    btn.disabled = false;
    label.textContent = "Salvar cookies";
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await getConfig();

  document.getElementById("btn-save-config").onclick = async () => {
    const url = document.getElementById("cfg-panel-url").value.trim().replace(/\/$/, "");
    const token = document.getElementById("cfg-token").value.trim();
    if (!url || !token) { msg("Preencha URL e token", "error"); return; }
    await setConfig({ panel_url: url, token });
    msg("Testando conexão…", "info");
    try {
      await loadAccountsList();
      clearMsg();
      msg("✓ Conectado!", "success");
      show("view-main");
      setTimeout(clearMsg, 2000);
    } catch (e) {
      msg("Falha: " + e.message + " — verifica token e URL", "error");
    }
  };

  document.getElementById("btn-capture").onclick = captureAndUpload;
  document.getElementById("btn-refresh").onclick = async () => {
    msg("Recarregando…", "info");
    try { await loadAccountsList(); clearMsg(); } catch (e) { msg(e.message, "error"); }
  };
  document.getElementById("btn-settings").onclick = () => {
    document.getElementById("cfg-panel-url").value = cfg.panel_url || "https://instapost.shop";
    document.getElementById("cfg-token").value = ""; // não revela; user reentra
    show("view-setup");
    clearMsg();
  };

  // View inicial: se já tem config, vai pra main; senão pede setup
  if (cfg.panel_url && cfg.token) {
    show("view-main");
    try {
      await loadAccountsList();
    } catch (e) {
      msg("Token inválido ou painel inacessível: " + e.message, "error");
      show("view-setup");
      document.getElementById("cfg-panel-url").value = cfg.panel_url;
    }
  } else {
    show("view-setup");
  }
});
