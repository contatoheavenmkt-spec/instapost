// Insta Poster — popup logic com proxy automático

const STORAGE_KEYS = ["panel_url", "token", "last_workspace", "last_account"];
let _data = null;  // cache da última fetch de extension-info

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
  _data = data;
  const wsSel = document.getElementById("sel-workspace");
  const accSel = document.getElementById("sel-account");
  const wsList = data.workspaces || [];
  const accList = data.accounts || [];

  wsSel.innerHTML = wsList.length
    ? wsList.map(w => `<option value="${w.slug}">${w.name}</option>`).join("")
    : `<option value="default">default</option>`;

  const cfg = await getConfig();
  if (cfg.last_workspace && wsList.some(w => w.slug === cfg.last_workspace)) {
    wsSel.value = cfg.last_workspace;
  }

  function renderAccountsForWorkspace() {
    const ws = wsSel.value;
    const filtered = accList.filter(a => a.workspace_slug === ws);
    if (!filtered.length) {
      accSel.innerHTML = `<option value="">— nenhuma conta nesse workspace —</option>`;
      document.getElementById("btn-apply-proxy").disabled = true;
      document.getElementById("account-proxy-hint").textContent = "—";
      return;
    }
    accSel.innerHTML = filtered.map(a => {
      const proxyLabel = a.proxy ? `${a.proxy.host}:${a.proxy.port}` : "sem proxy";
      return `<option value="${a.username}" data-proxy='${JSON.stringify(a.proxy || null)}'>@${a.username} — ${proxyLabel}</option>`;
    }).join("");
    if (cfg.last_account && filtered.some(a => a.username === cfg.last_account)) {
      accSel.value = cfg.last_account;
    }
    document.getElementById("btn-apply-proxy").disabled = false;
    updateAccountHint();
  }

  function updateAccountHint() {
    const ws = wsSel.value;
    const acc = accList.find(a => a.workspace_slug === ws && a.username === accSel.value);
    const hint = document.getElementById("account-proxy-hint");
    if (acc && acc.proxy) {
      hint.innerHTML = `Proxy: <code style="font-family: monospace; font-size: 10.5px">${acc.proxy.host}:${acc.proxy.port}</code>`;
    } else if (acc) {
      hint.innerHTML = `<span style="color: var(--warning)">⚠ Conta sem proxy configurado — vai usar seu IP direto</span>`;
    } else {
      hint.textContent = "—";
    }
  }

  wsSel.onchange = renderAccountsForWorkspace;
  accSel.onchange = updateAccountHint;
  renderAccountsForWorkspace();
  document.getElementById("header-sub").textContent = `Logado: ${data.user_email}`;
}

function getSelectedAccount() {
  const ws = document.getElementById("sel-workspace").value;
  const username = document.getElementById("sel-account").value;
  if (!_data || !username) return null;
  return _data.accounts.find(a => a.workspace_slug === ws && a.username === username) || null;
}

async function applyProxyAndOpen() {
  const acc = getSelectedAccount();
  if (!acc) { msg("Selecione uma conta", "error"); return; }
  if (!acc.proxy) {
    const ok = confirm("Essa conta não tem proxy configurado. Vai abrir Instagram usando seu IP direto.\n\nIsso pode causar mismatch quando a VPS for postar. Continuar mesmo assim?");
    if (!ok) return;
  }

  const btn = document.getElementById("btn-apply-proxy");
  const label = document.getElementById("btn-apply-proxy-label");
  btn.disabled = true;
  label.innerHTML = '<span class="spinner"></span> Aplicando proxy…';
  clearMsg();

  try {
    if (acc.proxy) {
      // Pede ao background pra setar proxy
      const r = await chrome.runtime.sendMessage({
        action: "applyProxy",
        proxy: acc.proxy,
        account_username: acc.username,
      });
      if (!r || !r.ok) throw new Error(r?.error || "Falha aplicando proxy");
    }
    // Abre nova aba no Instagram
    chrome.tabs.create({ url: "https://www.instagram.com/accounts/login/" });
    await refreshProxyBanner();
    document.getElementById("btn-capture").disabled = false;
    document.getElementById("btn-clear-proxy").disabled = false;
    document.getElementById("capture-hint").textContent = "Loga no Insta na aba que abriu, depois volta aqui";
    msg(acc.proxy ? `✓ Proxy ${acc.proxy.host}:${acc.proxy.port} ativo — loga na aba que abriu` : "✓ Aba do Insta aberta (sem proxy)", "success");
  } catch (e) {
    msg("Erro: " + e.message, "error");
  } finally {
    btn.disabled = false;
    label.textContent = "1) Aplicar proxy + abrir Insta";
  }
}

async function captureCookies() {
  const acc = getSelectedAccount();
  if (!acc) return;

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
      throw new Error("Não tem sessionid nos cookies — vc tá logado no instagram.com nessa conta?");
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

    msg(`✓ Cookies de @${result.username} salvos (${result.cookies_count} cookies)`, "success");
    await setConfig({ last_workspace: acc.workspace_slug, last_account: acc.username });

    // Limpa proxy automaticamente depois de salvar
    setTimeout(async () => {
      try {
        await chrome.runtime.sendMessage({ action: "clearProxy" });
        await refreshProxyBanner();
        document.getElementById("capture-hint").textContent = "Proxy limpo. Pronto pra próxima conta.";
      } catch {}
    }, 1500);
  } catch (e) {
    msg("Erro: " + e.message, "error");
  } finally {
    btn.disabled = false;
    label.textContent = "2) Salvar cookies";
  }
}

async function manualClearProxy() {
  try {
    await chrome.runtime.sendMessage({ action: "clearProxy" });
    await refreshProxyBanner();
    document.getElementById("btn-capture").disabled = true;
    document.getElementById("btn-clear-proxy").disabled = true;
    document.getElementById("capture-hint").textContent = "Proxy limpo — clica em (1) pra começar de novo";
    msg("Proxy removido", "info");
  } catch (e) {
    msg("Erro limpando proxy: " + e.message, "error");
  }
}

async function refreshProxyBanner() {
  const status = await chrome.runtime.sendMessage({ action: "getProxyStatus" });
  const banner = document.getElementById("proxy-active-banner");
  const info = document.getElementById("proxy-active-info");
  if (status && status.active && status.meta) {
    banner.classList.remove("hidden");
    info.textContent = `@${status.meta.account} via ${status.meta.host}:${status.meta.port}`;
    document.getElementById("btn-clear-proxy").disabled = false;
    document.getElementById("btn-capture").disabled = false;
  } else {
    banner.classList.add("hidden");
    document.getElementById("btn-clear-proxy").disabled = true;
    document.getElementById("btn-capture").disabled = true;
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
      await refreshProxyBanner();
      setTimeout(clearMsg, 2000);
    } catch (e) {
      msg("Falha: " + e.message + " — verifica token e URL", "error");
    }
  };

  document.getElementById("btn-apply-proxy").onclick = applyProxyAndOpen;
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
