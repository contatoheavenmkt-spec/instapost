// Insta Poster — popup logic v2: single-click flow

const STORAGE_KEYS = ["panel_url", "token", "last_workspace"];
let _data = null;  // cache da última fetch
let _activeAccount = null;  // username com proxy aplicado agora

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

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
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
  _data = data;

  const wsSel = document.getElementById("sel-workspace");
  const wsList = data.workspaces || [];
  const wsWrap = document.getElementById("ws-selector-wrap");

  // Auto-hide workspace dropdown se só tem 1
  if (wsList.length <= 1) {
    wsWrap.classList.add("hidden");
    wsSel.innerHTML = wsList.length
      ? `<option value="${wsList[0].slug}">${wsList[0].name}</option>`
      : `<option value="default">default</option>`;
  } else {
    wsWrap.classList.remove("hidden");
    wsSel.innerHTML = wsList.map(w => `<option value="${w.slug}">${escapeHtml(w.name)}</option>`).join("");
    const cfg = await getConfig();
    if (cfg.last_workspace && wsList.some(w => w.slug === cfg.last_workspace)) {
      wsSel.value = cfg.last_workspace;
    }
  }

  document.getElementById("header-sub").textContent = data.user_email || "—";
  renderAccountList();
}

function getSearchTerm() {
  return (document.getElementById("search-account").value || "").toLowerCase().trim();
}

function renderAccountList() {
  const ws = document.getElementById("sel-workspace").value;
  const search = getSearchTerm();
  const accList = (_data?.accounts || []).filter(a => a.workspace_slug === ws);
  const filtered = search
    ? accList.filter(a => a.username.toLowerCase().includes(search))
    : accList;

  const wrap = document.getElementById("account-list");
  if (!filtered.length) {
    wrap.innerHTML = `<div class="account-list-empty">
      ${accList.length === 0
        ? "Nenhuma conta cadastrada nesse workspace.<br><br>Adicione contas no painel primeiro."
        : `Nenhuma conta encontrada com "${escapeHtml(search)}"`
      }
    </div>`;
    return;
  }

  wrap.innerHTML = filtered.map(a => {
    const initial = (a.username[0] || "?").toUpperCase();
    const proxyText = a.proxy
      ? `via ${a.proxy.host}:${a.proxy.port}`
      : `<span style="color: var(--warning)">sem proxy</span>`;
    return `
      <div class="account-row" data-username="${escapeHtml(a.username)}">
        <div class="account-avatar">${escapeHtml(initial)}</div>
        <div class="account-info">
          <div class="account-name">@${escapeHtml(a.username)}</div>
          <div class="account-meta">${proxyText}</div>
        </div>
        <div class="account-action">Abrir →</div>
      </div>
    `;
  }).join("");

  // Click handlers
  wrap.querySelectorAll(".account-row").forEach(row => {
    row.addEventListener("click", () => onAccountClick(row.dataset.username));
  });
}

async function onAccountClick(username) {
  const ws = document.getElementById("sel-workspace").value;
  const acc = _data.accounts.find(a => a.workspace_slug === ws && a.username === username);
  if (!acc) return;

  // Se já tem proxy ativo pra OUTRA conta, pergunta se quer trocar
  if (_activeAccount && _activeAccount !== username) {
    const ok = confirm(`Proxy de @${_activeAccount} ainda tá ativo. Trocar pra @${username}?\n(Vai limpar o atual e aplicar o novo)`);
    if (!ok) return;
    try { await chrome.runtime.sendMessage({ action: "clearProxy" }); } catch {}
    _activeAccount = null;
  }

  // Marca o card como "carregando"
  const row = document.querySelector(`.account-row[data-username="${CSS.escape(username)}"]`);
  if (row) {
    const action = row.querySelector(".account-action");
    if (action) action.innerHTML = '<span class="spinner"></span>';
  }

  clearMsg();
  try {
    if (acc.proxy) {
      const r = await chrome.runtime.sendMessage({
        action: "applyProxy",
        proxy: acc.proxy,
        account_username: acc.username,
      });
      if (!r || !r.ok) throw new Error(r?.error || "Falha aplicando proxy");
    } else {
      // Sem proxy — avisa e segue
      const ok = confirm(`@${username} não tem proxy configurado no painel.\n\nIG vai ver seu IP direto, pode dar mismatch quando VPS for postar.\n\nContinuar mesmo assim?`);
      if (!ok) {
        if (row) row.querySelector(".account-action").textContent = "Abrir →";
        return;
      }
    }

    _activeAccount = username;
    await setConfig({ last_workspace: ws });

    // Abre Instagram numa aba nova
    chrome.tabs.create({ url: "https://www.instagram.com/accounts/login/" });

    await refreshProxyBanner();
    msg(`✓ Proxy de @${username} ativo — loga no Insta na aba que abriu`, "success");
  } catch (e) {
    msg("Erro: " + e.message, "error");
  } finally {
    if (row) {
      const action = row.querySelector(".account-action");
      if (action) action.textContent = "Abrir →";
    }
  }
}

async function captureCookies() {
  if (!_activeAccount) {
    msg("Nenhuma conta ativa — clica em uma conta primeiro", "error");
    return;
  }
  const ws = document.getElementById("sel-workspace").value;
  const acc = _data.accounts.find(a => a.workspace_slug === ws && a.username === _activeAccount);
  if (!acc) {
    msg("Conta não encontrada na lista atual", "error");
    return;
  }

  const btn = document.getElementById("btn-capture");
  const label = document.getElementById("btn-capture-label");
  btn.disabled = true;
  label.innerHTML = '<span class="spinner"></span> Lendo cookies…';
  clearMsg();

  try {
    const resp = await chrome.runtime.sendMessage({ action: "getInstaCookies" });
    if (!resp || !resp.ok) {
      throw new Error(resp?.error || "Falha lendo cookies");
    }
    const cookies = resp.cookies || {};
    if (!cookies.sessionid || !cookies.ds_user_id) {
      throw new Error("Sem sessionid nos cookies — vc tá logado no instagram.com nessa aba?");
    }

    label.innerHTML = '<span class="spinner"></span> Enviando ao painel…';
    const result = await apiCall("/api/sessions/upload", {
      method: "POST",
      body: JSON.stringify({
        workspace_slug: acc.workspace_slug,
        username: acc.username,
        cookies,
        user_agent: navigator.userAgent,
      }),
    });

    msg(`✅ Cookies de @${result.username} salvos na VPS (${result.cookies_count} cookies). VPS já pode postar.`, "success");

    // Limpa proxy automaticamente
    setTimeout(async () => {
      try {
        await chrome.runtime.sendMessage({ action: "clearProxy" });
        _activeAccount = null;
        await refreshProxyBanner();
      } catch {}
    }, 1500);
  } catch (e) {
    msg("Erro: " + e.message, "error");
  } finally {
    btn.disabled = false;
    label.textContent = "💾 Salvar cookies dessa conta";
  }
}

async function manualClearProxy() {
  try {
    await chrome.runtime.sendMessage({ action: "clearProxy" });
    _activeAccount = null;
    await refreshProxyBanner();
    msg("Proxy removido", "info");
  } catch (e) {
    msg("Erro: " + e.message, "error");
  }
}

async function refreshProxyBanner() {
  const status = await chrome.runtime.sendMessage({ action: "getProxyStatus" });
  const banner = document.getElementById("proxy-active-banner");
  if (status && status.active && status.meta) {
    banner.classList.remove("hidden");
    document.getElementById("active-account-name").textContent = status.meta.account;
    document.getElementById("proxy-active-info").textContent = `${status.meta.host}:${status.meta.port}`;
    _activeAccount = status.meta.account;
  } else {
    banner.classList.add("hidden");
    _activeAccount = null;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await getConfig();

  // Setup config
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
      await refreshProxyBanner();
      setTimeout(clearMsg, 2000);
    } catch (e) {
      msg("Falha: " + e.message + " — verifica token e URL", "error");
    }
  };

  // Main actions
  document.getElementById("btn-capture").onclick = captureCookies;
  document.getElementById("btn-clear-proxy").onclick = manualClearProxy;
  document.getElementById("btn-refresh").onclick = async () => {
    msg("Recarregando…", "info");
    try { await loadAccountsList(); clearMsg(); } catch (e) { msg(e.message, "error"); }
  };
  document.getElementById("btn-settings").onclick = () => {
    document.getElementById("cfg-panel-url").value = cfg.panel_url || "https://instapost.shop";
    document.getElementById("cfg-token").value = "";
    show("view-setup");
    clearMsg();
  };
  document.getElementById("link-help").onclick = (e) => {
    e.preventDefault();
    show("view-help");
  };
  document.getElementById("btn-close-help").onclick = () => {
    show("view-main");
  };

  // Workspace change → re-render list
  document.getElementById("sel-workspace").onchange = () => {
    renderAccountList();
  };

  // Search debounced
  let _searchTimer = null;
  document.getElementById("search-account").oninput = () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(renderAccountList, 120);
  };

  // Init
  if (cfg.panel_url && cfg.token) {
    show("view-main");
    try {
      await loadAccountsList();
      await refreshProxyBanner();
    } catch (e) {
      msg("Token inválido ou painel inacessível: " + e.message, "error");
      show("view-setup");
      document.getElementById("cfg-panel-url").value = cfg.panel_url;
    }
  } else {
    show("view-setup");
  }
});
