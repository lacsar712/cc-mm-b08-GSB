const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let previewRows = [];

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const viewMain = document.querySelector("#viewMain");
const viewImport = document.querySelector("#viewImport");
const navImport = document.querySelector("#navImport");
const navList = document.querySelector("#navList");
const importText = document.querySelector("#importText");
const btnPreview = document.querySelector("#btnPreview");
const btnConfirm = document.querySelector("#btnConfirm");
const importMsg = document.querySelector("#importMsg");
const previewTable = document.querySelector("#previewTable");
const previewRowsEl = document.querySelector("#previewRows");

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(
      Array.isArray(detail)
        ? detail.map((d) => d.msg).join("；") || "请求失败"
        : detail || "请求失败",
    );
  }
  return data;
}

function showImportGuard() {
  // 旁观账号不能打开导入页：直接路由拦回总表，接口侧也有 403 兜底
  if (role !== "writer") {
    live.textContent = "旁观账号无权使用批量导入";
    location.hash = "#main";
    return false;
  }
  return true;
}

function route() {
  const onImport = location.hash === "#import";
  if (onImport && !showImportGuard()) return;
  viewMain.hidden = onImport;
  viewImport.hidden = !onImport;
  navImport.hidden = role !== "writer" || onImport;
  navList.hidden = !onImport;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  connect();
  load();
  route();
}

async function load() {
  paint(await api("/api/readings"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
  };
}

function paintPreview() {
  previewRowsEl.innerHTML = previewRows
    .map((r) =>
      r.error
        ? `<tr><td>${r.line}</td><td colspan="3" class="alarm">${r.error}</td></tr>`
        : `<tr><td>${r.line}</td><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
  previewTable.hidden = false;
}

btnPreview.onclick = async () => {
  importMsg.textContent = "";
  try {
    const data = await api("/api/readings/import/preview", {
      method: "POST",
      body: JSON.stringify({ text: importText.value }),
    });
    previewRows = data.rows;
    paintPreview();
    const bad = previewRows.some((r) => r.error);
    btnConfirm.disabled = previewRows.length === 0 || bad;
    importMsg.textContent = `试算完成，共 ${previewRows.length} 行，状态仅供预览，尚未入库。`;
  } catch (err) {
    importMsg.textContent = err.message;
  }
};

btnConfirm.onclick = async () => {
  importMsg.textContent = "";
  btnConfirm.disabled = true;
  try {
    const data = await api("/api/readings/import/confirm", {
      method: "POST",
      body: JSON.stringify({ text: importText.value }),
    });
    importMsg.textContent = `已入库 ${data.imported} 行，报警记录已推送。`;
    previewRows = [];
    previewTable.hidden = true;
    importText.value = "";
    location.hash = "#main";
    load();
  } catch (err) {
    importMsg.textContent = `导入失败，整批未入库：${err.message}`;
    btnPreview.click();
  }
};

// 试算结果一旦改动文本即作废，避免“看到的状态”和实际提交不一致
importText.addEventListener("input", () => {
  btnConfirm.disabled = true;
});

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

window.addEventListener("hashchange", route);

if (token) showApp();
