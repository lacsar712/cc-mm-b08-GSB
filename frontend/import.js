const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
const role = localStorage.getItem("methane_role") || "";

const deniedBox = document.querySelector("#denied");
const panel = document.querySelector("#panel");
const textArea = document.querySelector("#text");
const msg = document.querySelector("#msg");
const confirmBtn = document.querySelector("#confirm");
const previewTable = document.querySelector("#previewTable");
const previewRows = document.querySelector("#previewRows");
const historyBox = document.querySelector("#history");

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
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showDenied() {
  document.querySelector("#who").textContent = role ? `当前：查看（${role}）` : "未登录";
  document.querySelector("#out").hidden = false;
  deniedBox.hidden = false;
}

let previewOk = false;

async function loadHistory() {
  const batches = await api("/api/imports");
  historyBox.innerHTML = batches
    .map(
      (b) =>
        `<tr><td>${b.id}</td><td class="${b.status === "success" ? "ok" : "bad"}">${
          b.status === "success" ? "成功" : "失败"
        }</td><td>${b.total_rows}</td><td>${b.imported_count}</td><td>${b.error || ""}</td><td>${
          b.created_by
        }</td><td>${b.created_at}</td></tr>`,
    )
    .join("");
}

document.querySelector("#preview").onclick = async () => {
  msg.textContent = "";
  previewTable.hidden = true;
  confirmBtn.disabled = true;
  previewOk = false;
  try {
    const res = await api("/api/imports/preview", {
      method: "POST",
      body: JSON.stringify({ text: textArea.value }),
    });
    previewRows.innerHTML = res.rows
      .map((r) => {
        if (r.error) {
          return `<tr><td>${r.line_no}</td><td>${r.site || "（空）"}</td><td>${
            r.ch4_pct ?? ""
          }</td><td class="bad">错误</td><td class="bad">${r.error}</td></tr>`;
        }
        return `<tr><td>${r.line_no}</td><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${
          r.level === "报警" ? "alarm" : "ok"
        }">${r.level}</td><td>${r.note}</td></tr>`;
      })
      .join("");
    previewTable.hidden = false;
    previewOk = res.ok;
    confirmBtn.disabled = !previewOk;
    msg.textContent = `试算完成：共 ${res.total} 行，报警 ${res.alarms} 行。试算不写库、不推送。${
      previewOk ? "核对无误后点「确认导入」。" : "存在错误行，无法确认。"
    }`;
  } catch (err) {
    msg.textContent = err.message;
  }
};

document.querySelector("#confirm").onclick = async () => {
  if (!previewOk) return;
  msg.textContent = "正在入库…";
  try {
    const res = await api("/api/imports/confirm", {
      method: "POST",
      body: JSON.stringify({ text: textArea.value }),
    });
    msg.textContent = `已整批入库 ${res.imported} 行，其中报警 ${res.alarms} 行已推送。`;
    confirmBtn.disabled = true;
    previewOk = false;
    await loadHistory();
  } catch (err) {
    msg.textContent = err.message;
    confirmBtn.disabled = true;
    previewOk = false;
    await loadHistory().catch(() => {});
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.href = "/";
};

// 旁观账号不能打开导入页：前端直接拦截，服务端接口同样拒绝
if (!token) {
  location.href = "/";
} else if (role !== "writer") {
  showDenied();
} else {
  document.querySelector("#who").textContent = "检查员";
  document.querySelector("#out").hidden = false;
  panel.hidden = false;
  loadHistory().catch((err) => {
    if (/未登录|无效令牌|401/.test(err.message)) location.href = "/";
    msg.textContent = err.message;
  });
}
