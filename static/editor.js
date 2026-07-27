(() => {
    "use strict";

    const MAX_TEXT_BYTES = 1024 * 1024;
    const DRAFT_KEY = "easytype.pendingDraft.v1";
    const CLIENT_ID_KEY = "easytype.clientId.v1";
    const textEncoder = new TextEncoder();

    const input = document.getElementById("sharedText");
    const charCount = document.getElementById("charCount");
    const syncState = document.getElementById("syncState");
    const connectionBadge = document.getElementById("connectionBadge");
    const connectionText = document.getElementById("connectionText");
    const conflictPanel = document.getElementById("conflictPanel");
    const useRemoteButton = document.getElementById("useRemoteButton");
    const keepLocalButton = document.getElementById("keepLocalButton");
    const copyButton = document.getElementById("copyButton");
    const pasteButton = document.getElementById("pasteButton");
    const clearButton = document.getElementById("clearButton");
    const toast = document.getElementById("toast");
    const aboutButton = document.getElementById("aboutButton");
    const aboutPanel = document.getElementById("aboutPanel");
    const closeAboutButton = document.getElementById("closeAboutButton");
    const accessSettingsButton = document.getElementById("accessSettingsButton");
    const accessSettingsPanel = document.getElementById("accessSettingsPanel");
    const closeAccessSettingsButton = document.getElementById("closeAccessSettingsButton");
    const saveAccessModeButton = document.getElementById("saveAccessModeButton");
    const deviceManagementLink = document.getElementById("deviceManagementLink");
    const trustedLanAccess = document.getElementById("trustedLanAccess");

    const state = {
        socket: null,
        revision: 0,
        serverText: "",
        inFlight: null,
        conflict: null,
        flushTimer: null,
        reconnectTimer: null,
        reconnectAttempt: 0,
        manuallyClosed: false,
        initialized: false,
        limitNoticeShown: false,
        clientId: getClientId(),
    };

    function getClientId() {
        let existing = sessionStorage.getItem(CLIENT_ID_KEY);
        if (existing) {
            return existing;
        }
        existing = globalThis.crypto?.randomUUID?.() ??
            `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        sessionStorage.setItem(CLIENT_ID_KEY, existing);
        return existing;
    }

    function setConnection(status, label) {
        connectionBadge.className = `status-badge status-${status}`;
        connectionText.textContent = label;
        const available = status === "online";
        copyButton.disabled = !available;
        pasteButton.disabled = !available;
    }

    function setSync(label) {
        syncState.textContent = label;
        syncState.title = label;
    }

    function updateMeta() {
        charCount.textContent = `${input.value.length} 字符`;
    }

    function notifyWhenLimitIsReached() {
        const byteLength = textEncoder.encode(input.value).byteLength;
        if (byteLength < MAX_TEXT_BYTES) {
            state.limitNoticeShown = false;
            return;
        }
        if (state.limitNoticeShown) {
            return;
        }
        showToast(
            byteLength > MAX_TEXT_BYTES
                ? "内容已超过上限，请删减后再同步。"
                : "内容已达到上限，无法继续增加。",
            true,
        );
        state.limitNoticeShown = true;
    }

    function showToast(message, isError = false) {
        toast.textContent = message;
        toast.className = isError ? "toast error" : "toast";
        toast.hidden = false;
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => {
            toast.hidden = true;
        }, 2600);
    }

    function currentDraft() {
        try {
            const raw = sessionStorage.getItem(DRAFT_KEY);
            if (!raw) {
                return null;
            }
            const parsed = JSON.parse(raw);
            if (
                typeof parsed.text === "string" &&
                Number.isInteger(parsed.baseRevision)
            ) {
                return parsed;
            }
        } catch (_error) {
            // Ignore an invalid session draft.
        }
        return null;
    }

    function saveDraft(text = input.value, baseRevision = state.revision) {
        sessionStorage.setItem(
            DRAFT_KEY,
            JSON.stringify({ text, baseRevision }),
        );
    }

    function clearDraft() {
        sessionStorage.removeItem(DRAFT_KEY);
    }

    function scheduleFlush(delay = 200) {
        clearTimeout(state.flushTimer);
        state.flushTimer = setTimeout(flush, delay);
    }

    function flush() {
        clearTimeout(state.flushTimer);
        state.flushTimer = null;
        if (state.conflict || state.inFlight) {
            return;
        }
        if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
            setSync("离线修改待发送");
            return;
        }
        if (input.value === state.serverText) {
            clearDraft();
            setSync("已同步");
            return;
        }
        if (textEncoder.encode(input.value).byteLength > MAX_TEXT_BYTES) {
            setSync("内容超过 1 MiB");
            showToast("内容超过 1 MiB，暂未同步。", true);
            return;
        }

        state.inFlight = {
            text: input.value,
            baseRevision: state.revision,
        };
        saveDraft(state.inFlight.text, state.inFlight.baseRevision);
        state.socket.send(JSON.stringify({
            type: "update",
            clientId: state.clientId,
            baseRevision: state.inFlight.baseRevision,
            text: state.inFlight.text,
        }));
        setSync("正在同步");
    }

    function commonPrefixLength(left, right) {
        const limit = Math.min(left.length, right.length);
        let index = 0;
        while (index < limit && left[index] === right[index]) {
            index += 1;
        }
        return index;
    }

    function commonSuffixLength(left, right, prefixLength) {
        const limit = Math.min(left.length, right.length) - prefixLength;
        let count = 0;
        while (
            count < limit &&
            left[left.length - 1 - count] === right[right.length - 1 - count]
        ) {
            count += 1;
        }
        return count;
    }

    function applyRemoteText(nextText) {
        const previousText = input.value;
        const wasFocused = document.activeElement === input;
        const selectionStart = input.selectionStart;
        const selectionEnd = input.selectionEnd;
        const prefix = commonPrefixLength(previousText, nextText);
        const suffix = commonSuffixLength(previousText, nextText, prefix);

        input.value = nextText;
        if (wasFocused) {
            const oldChangedEnd = previousText.length - suffix;
            const newChangedEnd = nextText.length - suffix;
            const mapPosition = (position) => {
                if (position <= prefix) {
                    return position;
                }
                if (position >= oldChangedEnd) {
                    return Math.max(0, position + nextText.length - previousText.length);
                }
                return newChangedEnd;
            };
            input.setSelectionRange(
                mapPosition(selectionStart),
                mapPosition(selectionEnd),
            );
        }
        updateMeta();
    }

    function acceptSnapshot(documentState) {
        state.revision = documentState.revision;
        state.serverText = documentState.text;
        state.inFlight = null;
        updateMeta();

        const draft = currentDraft();
        if (!draft || draft.text === documentState.text) {
            applyRemoteText(documentState.text);
            clearDraft();
            setSync("已同步");
        } else if (draft.baseRevision === documentState.revision) {
            input.value = draft.text;
            updateMeta();
            setSync("正在发送离线修改");
            scheduleFlush(0);
        } else {
            enterConflict(documentState, draft.text);
        }
        state.initialized = true;
    }

    function receiveUpdate(documentState) {
        if (documentState.revision <= state.revision) {
            return;
        }
        if (
            state.conflict ||
            state.inFlight ||
            input.value !== state.serverText
        ) {
            enterConflict(documentState, input.value);
            return;
        }
        state.revision = documentState.revision;
        state.serverText = documentState.text;
        applyRemoteText(documentState.text);
        clearDraft();
        setSync("已同步");
    }

    function acknowledge(message) {
        if (!state.inFlight) {
            state.revision = Math.max(state.revision, message.revision);
            updateMeta();
            return;
        }
        state.revision = message.revision;
        state.serverText = state.inFlight.text;
        state.inFlight = null;
        updateMeta();

        if (input.value === state.serverText) {
            clearDraft();
            setSync("已同步");
        } else {
            saveDraft(input.value, state.revision);
            scheduleFlush(0);
        }
    }

    function enterConflict(documentState, localText) {
        state.inFlight = null;
        state.conflict = {
            remote: documentState,
            localText,
        };
        saveDraft(localText, state.revision);
        conflictPanel.hidden = false;
        setSync("同步冲突，等待选择");
    }

    function resolveWithRemote() {
        if (!state.conflict) {
            return;
        }
        const remote = state.conflict.remote;
        state.conflict = null;
        state.revision = remote.revision;
        state.serverText = remote.text;
        applyRemoteText(remote.text);
        clearDraft();
        conflictPanel.hidden = true;
        setSync("已采用最新内容");
    }

    function resolveWithLocal() {
        if (!state.conflict) {
            return;
        }
        const { remote, localText } = state.conflict;
        state.conflict = null;
        state.revision = remote.revision;
        state.serverText = remote.text;
        input.value = localText;
        updateMeta();
        saveDraft(localText, state.revision);
        conflictPanel.hidden = true;
        setSync("正在用本机内容覆盖");
        scheduleFlush(0);
    }

    function handleSocketMessage(event) {
        let message;
        try {
            message = JSON.parse(event.data);
        } catch (_error) {
            showToast("收到无法解析的服务器消息。", true);
            return;
        }
        switch (message.type) {
            case "snapshot":
                acceptSnapshot(message.document);
                break;
            case "update":
                receiveUpdate(message.document);
                break;
            case "ack":
                acknowledge(message);
                break;
            case "conflict":
                enterConflict(
                    message.document,
                    state.inFlight?.text ?? input.value,
                );
                break;
            case "error":
                state.inFlight = null;
                setSync("同步失败");
                showToast(message.error?.message ?? "同步失败。", true);
                break;
            case "pong":
                break;
            default:
                showToast("收到未知服务器消息。", true);
        }
    }

    function connect() {
        clearTimeout(state.reconnectTimer);
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        const socket = new WebSocket(`${protocol}//${location.host}/ws`);
        state.socket = socket;
        setConnection("connecting", "未连接");

        socket.addEventListener("open", () => {
            state.reconnectAttempt = 0;
            setConnection("online", "已连接");
        });
        socket.addEventListener("message", handleSocketMessage);
        socket.addEventListener("close", (event) => {
            if (state.socket !== socket) {
                return;
            }
            if (state.inFlight) {
                saveDraft(state.inFlight.text, state.inFlight.baseRevision);
                state.inFlight = null;
            }
            setConnection("offline", "未连接");
            setSync(input.value === state.serverText ? "等待重连" : "离线修改待发送");
            if (state.manuallyClosed) {
                return;
            }
            if (event.code === 1008) {
                location.assign("/pair");
                return;
            }
            const delay = Math.min(5000, 500 * (2 ** state.reconnectAttempt));
            state.reconnectAttempt += 1;
            state.reconnectTimer = setTimeout(connect, delay);
        });
        socket.addEventListener("error", () => {
            socket.close();
        });
    }

    async function postAction(action) {
        const response = await fetch(`/api/actions/${action}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        });
        if (response.status === 401) {
            location.assign("/pair");
            return;
        }
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error?.message ?? "操作失败。");
        }
    }

    function updateAccessModeUi(mode) {
        if (!accessSettingsButton) {
            return;
        }
        const pairingMode = mode === "pairing";
        accessSettingsButton.textContent =
            `访问设置 · ${pairingMode ? "配对模式" : "信任模式"}`;
        deviceManagementLink.hidden = !pairingMode;
        trustedLanAccess.hidden = pairingMode;
        const selected = document.querySelector(
            `input[name="accessMode"][value="${mode}"]`,
        );
        if (selected) {
            selected.checked = true;
        }
    }

    function setAboutOpen(open) {
        aboutPanel.hidden = !open;
        aboutButton.setAttribute("aria-expanded", String(open));
        if (open && accessSettingsPanel) {
            accessSettingsPanel.hidden = true;
            accessSettingsButton.setAttribute("aria-expanded", "false");
        }
    }

    function setAccessSettingsOpen(open) {
        accessSettingsPanel.hidden = !open;
        accessSettingsButton.setAttribute("aria-expanded", String(open));
        if (open) {
            aboutPanel.hidden = true;
            aboutButton.setAttribute("aria-expanded", "false");
        }
    }

    input.addEventListener("input", () => {
        updateMeta();
        notifyWhenLimitIsReached();
        saveDraft(input.value, state.revision);
        if (state.conflict) {
            state.conflict.localText = input.value;
            setSync("冲突期间的本机修改已暂存");
            return;
        }
        setSync(state.socket?.readyState === WebSocket.OPEN ? "等待同步" : "离线修改待发送");
        scheduleFlush();
    });

    useRemoteButton.addEventListener("click", resolveWithRemote);
    keepLocalButton.addEventListener("click", resolveWithLocal);
    aboutButton.addEventListener("click", () => {
        setAboutOpen(aboutPanel.hidden);
    });
    closeAboutButton.addEventListener("click", () => {
        setAboutOpen(false);
    });

    if (accessSettingsButton) {
        accessSettingsButton.addEventListener("click", () => {
            setAccessSettingsOpen(accessSettingsPanel.hidden);
        });
        closeAccessSettingsButton.addEventListener("click", () => {
            setAccessSettingsOpen(false);
        });
        saveAccessModeButton.addEventListener("click", async () => {
            const selected = document.querySelector(
                'input[name="accessMode"]:checked',
            );
            if (!selected) {
                return;
            }
            if (
                selected.value === "trusted_lan" &&
                !confirm(
                    "信任模式允许同一家庭局域网内的设备直接访问。确定切换吗？",
                )
            ) {
                return;
            }
            saveAccessModeButton.disabled = true;
            try {
                const response = await fetch("/api/admin/access-mode", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ mode: selected.value }),
                });
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(payload.error?.message ?? "切换访问模式失败。");
                }
                updateAccessModeUi(payload.mode);
                setAccessSettingsOpen(false);
                showToast(
                    payload.mode === "pairing"
                        ? "已切换到配对模式。"
                        : "已切换到信任模式。",
                );
            } catch (error) {
                showToast(error.message, true);
            } finally {
                saveAccessModeButton.disabled = false;
            }
        });
    }

    copyButton.addEventListener("click", async () => {
        try {
            await postAction("copy");
            showToast("已复制到电脑剪贴板。");
        } catch (error) {
            showToast(error.message, true);
        }
    });

    pasteButton.addEventListener("click", async () => {
        try {
            await postAction("paste");
            showToast("已插入到电脑当前窗口。");
        } catch (error) {
            showToast(error.message, true);
        }
    });

    clearButton.addEventListener("click", () => {
        if (!input.value || !confirm("确定清空所有设备上的共享文本吗？")) {
            return;
        }
        input.value = "";
        input.dispatchEvent(new Event("input"));
        input.focus();
    });

    window.addEventListener("pagehide", () => {
        state.manuallyClosed = true;
        if (input.value !== state.serverText) {
            saveDraft(input.value, state.revision);
        }
        state.socket?.close();
    });

    setConnection("connecting", "未连接");
    updateMeta();
    connect();
})();
