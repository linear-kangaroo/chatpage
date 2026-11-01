(() => {
  "use strict";

  const TOKEN_KEY = "chatpage_token";
  const BACKEND_KEY = "chatpage_backend";
  const MAX_CONTEXT_MESSAGES = 20;
  const HEALTH_INTERVAL_MS = 30000;

  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("btn-send");
  const clearBtn = document.getElementById("btn-clear");
  const settingsBtn = document.getElementById("btn-settings");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const tokenDialog = document.getElementById("token-dialog");
  const tokenForm = document.getElementById("token-form");
  const tokenInput = document.getElementById("token-input");
  const backendInput = document.getElementById("backend-input");
  const tokenError = document.getElementById("token-error");
  const btnTokenCancel = document.getElementById("btn-token-cancel");

  let token = localStorage.getItem(TOKEN_KEY) || "";
  let backend = localStorage.getItem(BACKEND_KEY) || ""; // 留空 = 与页面同源

  function apiUrl(path) {
    return (backend ? backend.replace(/\/+$/, "") : "") + path;
  }
  let history = []; // [{ role, content }]
  let controller = null; // 进行中请求的 AbortController
  let pendingEl = null; // "正在思考"占位节点

  class ApiError extends Error {
    constructor(status, detail) {
      super(`HTTP ${status}`);
      this.name = "ApiError";
      this.status = status;
      this.detail = detail;
    }
  }

  function setStatus(text, state) {
    statusText.textContent = text;
    statusEl.dataset.state = state;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function appendMessage(role, content) {
    const wrapper = document.createElement("div");
    wrapper.className = `message ${role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = content; // 仅用 textContent,避免 XSS
    wrapper.appendChild(bubble);
    messagesEl.appendChild(wrapper);
    scrollToBottom();
    return wrapper;
  }

  function renderAll() {
    messagesEl.replaceChildren();
    for (const message of history) {
      appendMessage(message.role, message.content);
    }
  }

  function stopPending(notice) {
    const wasVisible = Boolean(pendingEl && pendingEl.isConnected);
    if (pendingEl) {
      pendingEl.remove();
      pendingEl = null;
    }
    if (notice && wasVisible) {
      appendMessage("error", notice);
    }
  }

  function setLoading(loading) {
    sendBtn.textContent = loading ? "停止" : "发送";
    sendBtn.classList.toggle("btn-danger", loading);
    inputEl.disabled = loading;
  }

  function friendlyError(error) {
    const map = {
      400: "请求不合法",
      401: "访问令牌无效,请重新设置",
      413: "消息过长",
      429: "请求过于频繁,请稍后再试",
      502: "上游服务暂时不可用,请稍后再试",
      504: "上游响应超时,请稍后再试",
    };
    return error.detail || map[error.status] || `请求失败(HTTP ${error.status})`;
  }

  function openTokenDialog(message) {
    backendInput.value = backend;
    tokenInput.value = token;
    tokenError.hidden = !message;
    tokenError.textContent = message || "";
    tokenDialog.showModal();
    tokenInput.focus();
  }

  async function send() {
    const text = inputEl.value.trim();
    if (!text || controller) return;
    if (!token) {
      openTokenDialog("请先设置访问令牌");
      return;
    }

    inputEl.value = "";
    autoResize();
    const userMsg = { role: "user", content: text };
    history.push(userMsg);
    appendMessage("user", text);

    pendingEl = appendMessage("pending", "");
    setLoading(true);
    controller = new AbortController();

    let content = "";
    try {
      const response = await fetch(apiUrl("/api/chat/stream"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          messages: history.slice(-MAX_CONTEXT_MESSAGES),
        }),
        signal: controller.signal,
      });

      let detail = "";
      if (!response.ok) {
        try {
          const data = await response.json();
          detail = data.detail || "";
        } catch {
          // 响应体不是 JSON 时忽略
        }
        throw new ApiError(response.status, detail);
      }
      if (!response.body) {
        throw new ApiError(0, "浏览器不支持流式响应");
      }

      // 解析 SSE:fetch + ReadableStream(EventSource 不支持 POST 与自定义头)
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let bubbleEl = null;
      let upstreamError = "";
      let finished = false;

      const ensureBubble = () => {
        if (!bubbleEl) {
          stopPending("");
          bubbleEl = appendMessage("assistant", "").querySelector(".bubble");
        }
        return bubbleEl;
      };

      outer: while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          for (const line of block.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (payload === "[DONE]") {
              finished = true;
              break outer;
            }
            let parsed;
            try {
              parsed = JSON.parse(payload);
            } catch {
              continue;
            }
            if (parsed.error) {
              upstreamError = parsed.error;
              break outer;
            }
            if (typeof parsed.delta === "string" && parsed.delta) {
              content += parsed.delta;
              ensureBubble().textContent = content;
            }
          }
        }
      }

      if (upstreamError) {
        if (content) {
          // 已有部分内容:保留并追加错误提示
          bubbleEl.textContent = content;
          history.push({ role: "assistant", content });
          appendMessage("error", upstreamError);
        } else {
          stopPending(upstreamError);
          if (history[history.length - 1] === userMsg) history.pop();
        }
      } else if (content) {
        // 正常结束(finished 或流自然关闭)
        bubbleEl.textContent = content;
        history.push({ role: "assistant", content });
      } else {
        stopPending(finished ? "没有收到回复,请重试" : "连接中断,请重试");
        if (history[history.length - 1] === userMsg) history.pop();
      }
    } catch (error) {
      if (error.name === "AbortError") {
        stopPending("已停止生成");
        if (content) {
          history.push({ role: "assistant", content });
        } else if (history[history.length - 1] === userMsg) {
          history.pop();
        }
      } else if (error instanceof ApiError) {
        const message = friendlyError(error);
        stopPending(message);
        if (history[history.length - 1] === userMsg) history.pop();
        if (error.status === 401) {
          openTokenDialog(message);
        }
      } else {
        const message = "无法连接后端服务,请检查设置中的后端地址和令牌";
        stopPending(message);
        if (history[history.length - 1] === userMsg) history.pop();
        openTokenDialog(message);
      }
    } finally {
      controller = null;
      setLoading(false);
    }
  }

  async function checkHealth() {
    setStatus("连接中…", "pending");
    try {
      const response = await fetch(apiUrl("/health"), { cache: "no-store" });
      if (response.ok) {
        setStatus("已连接", "ok");
        return;
      }
    } catch {
      // 落到未连接
    }
    setStatus("未连接", "error");
  }

  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = `${Math.min(inputEl.scrollHeight, 160)}px`;
  }

  inputEl.addEventListener("input", autoResize);

  inputEl.addEventListener("keydown", (event) => {
    // isComposing 避免中文输入法选词时的 Enter 误发送
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });

  sendBtn.addEventListener("click", () => {
    if (controller) {
      controller.abort();
    } else {
      send();
    }
  });

  clearBtn.addEventListener("click", () => {
    if (controller) controller.abort();
    history = [];
    renderAll();
  });

  settingsBtn.addEventListener("click", () => openTokenDialog(""));

  tokenForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = tokenInput.value.trim();
    if (!value) {
      tokenError.hidden = false;
      tokenError.textContent = "令牌不能为空";
      return;
    }
    backend = backendInput.value.trim();
    localStorage.setItem(BACKEND_KEY, backend);
    token = value;
    localStorage.setItem(TOKEN_KEY, token);
    tokenDialog.close();
    checkHealth();
  });

  btnTokenCancel.addEventListener("click", () => tokenDialog.close());

  if (!token) {
    openTokenDialog("");
  }
  checkHealth();
  setInterval(checkHealth, HEALTH_INTERVAL_MS);
})();
