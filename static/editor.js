(() => {
    "use strict";

    const MAX_TEXT_BYTES = 1024 * 1024;
    const DRAFTS_KEY = "easytype.pendingDrafts.v2";
    const CLIENT_ID_KEY = "easytype.clientId.v1";
    const ACTIVE_BOARD_KEY = "easytype.activeBoard.v1";
    const WORK_MODE_KEY = "easytype.workMode.v1";
    const DIRECT_FLUSH_DELAY_MS = 45;
    const EMPTY_INPUT_SENTINEL = "\u200b";
    const textEncoder = new TextEncoder();

    const shell = document.querySelector(".editor-shell");
    const isLocal = shell.dataset.isLocal === "true";
    const input = document.getElementById("sharedText");
    const editorCard = input.closest(".editor-card");
    const charCount = document.getElementById("charCount");
    const syncState = document.getElementById("syncState");
    const connectionBadge = document.getElementById("connectionBadge");
    const connectionText = document.getElementById("connectionText");
    const conflictPanel = document.getElementById("conflictPanel");
    const useRemoteButton = document.getElementById("useRemoteButton");
    const keepLocalButton = document.getElementById("keepLocalButton");
    const copyButton = document.getElementById("copyButton");
    const remoteEnterButton = document.getElementById("remoteEnterButton");
    const pasteButton = document.getElementById("pasteButton");
    const clearButton = document.getElementById("clearButton");
    const toast = document.getElementById("toast");
    const aboutButton = document.getElementById("aboutButton");
    const aboutPanel = document.getElementById("aboutPanel");
    const aboutBackdrop = document.getElementById("aboutBackdrop");
    const closeAboutButton = document.getElementById("closeAboutButton");
    const accessSettingsButton = document.getElementById("accessSettingsButton");
    const accessSettingsPanel = document.getElementById("accessSettingsPanel");
    const closeAccessSettingsButton = document.getElementById("closeAccessSettingsButton");
    const saveAccessModeButton = document.getElementById("saveAccessModeButton");
    const deviceManagementLink = document.getElementById("deviceManagementLink");
    const trustedLanAccess = document.getElementById("trustedLanAccess");
    const boardModeButton = document.getElementById("boardModeButton");
    const directModeButton = document.getElementById("directModeButton");
    const boardWorkspace = document.getElementById("boardWorkspace");
    const directWorkspace = document.getElementById("directWorkspace");
    const boardTabs = document.getElementById("boardTabs");
    const addBoardButton = document.getElementById("addBoardButton");
    const renameBoardButton = document.getElementById("renameBoardButton");
    const renameBoardDialog = document.getElementById("renameBoardDialog");
    const renameBoardForm = document.getElementById("renameBoardForm");
    const renameBoardInput = document.getElementById("renameBoardInput");
    const cancelRenameBoardButton = document.getElementById(
        "cancelRenameBoardButton",
    );
    const directSurface = document.getElementById("directSurface");
    const directCapture = document.getElementById("directCapture");
    const directToggleButton = document.getElementById("directToggleButton");
    const directStatus = document.getElementById("directStatus");
    const directHint = document.getElementById("directHint");
    const directBackspaceButton = document.getElementById("directBackspaceButton");
    const directEnterButton = document.getElementById("directEnterButton");

    const state = {
        socket: null,
        boards: new Map(),
        documents: new Map(),
        activeBoardId: null,
        workMode: readWorkMode(),
        maxBoards: Number(shell.dataset.maxBoards) || 8,
        deletingBoardId: null,
        renamingBoardId: null,
        renameDialogBoardId: null,
        reconnectTimer: null,
        reconnectAttempt: 0,
        manuallyClosed: false,
        initialized: false,
        limitNoticeShown: false,
        clientId: getClientId(),
        direct: {
            serverStatus: { active: false },
            starting: false,
            active: false,
            sessionId: null,
            sequence: 0,
            acknowledgedText: "",
            pending: null,
            pendingKeys: [],
            composing: false,
            flushTimer: null,
        },
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

    function readWorkMode() {
        return localStorage.getItem(WORK_MODE_KEY) === "direct"
            ? "direct"
            : "board";
    }

    function socketReady() {
        return state.socket?.readyState === WebSocket.OPEN;
    }

    function send(message) {
        if (!socketReady()) {
            return false;
        }
        state.socket.send(JSON.stringify(message));
        return true;
    }

    function setConnection(status, label) {
        connectionBadge.className = `status-badge status-${status}`;
        connectionText.textContent = label;
        updateBoardControls(status === "online");
        if (pasteButton) {
            pasteButton.disabled = status !== "online";
        }
        if (remoteEnterButton) {
            remoteEnterButton.disabled = status !== "online";
        }
    }

    function updateBoardControls(online = socketReady()) {
        const boardCount = state.boards.size;
        addBoardButton.disabled =
            !online || boardCount >= state.maxBoards || Boolean(state.deletingBoardId);
        addBoardButton.title = boardCount >= state.maxBoards
            ? `最多只能创建 ${state.maxBoards} 个共享板`
            : "新建共享板";
        renameBoardButton.disabled =
            !online ||
            !state.activeBoardId ||
            Boolean(state.deletingBoardId) ||
            Boolean(state.renamingBoardId);
        for (const closeButton of boardTabs.querySelectorAll(".board-tab-close")) {
            closeButton.disabled =
                !online || boardCount <= 1 || Boolean(state.deletingBoardId);
            closeButton.title = boardCount <= 1
                ? "至少需要保留一个共享板"
                : "关闭并删除此共享板";
        }
    }

    function setSync(label) {
        syncState.textContent = label;
        syncState.title = label;
    }

    function showToast(message, isError = false) {
        toast.textContent = message;
        toast.className = isError ? "toast error" : "toast";
        toast.hidden = false;
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => {
            toast.hidden = true;
        }, 2800);
    }

    function readDrafts() {
        try {
            const parsed = JSON.parse(sessionStorage.getItem(DRAFTS_KEY) || "{}");
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch (_error) {
            return {};
        }
    }

    function saveDraft(boardId, text, baseRevision) {
        const drafts = readDrafts();
        drafts[boardId] = { text, baseRevision };
        sessionStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
    }

    function getDraft(boardId) {
        const draft = readDrafts()[boardId];
        if (
            draft &&
            typeof draft.text === "string" &&
            Number.isInteger(draft.baseRevision)
        ) {
            return draft;
        }
        return null;
    }

    function clearDraft(boardId) {
        const drafts = readDrafts();
        delete drafts[boardId];
        sessionStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
    }

    function ensureDocument(boardId) {
        if (!state.documents.has(boardId)) {
            state.documents.set(boardId, {
                initialized: false,
                serverText: "",
                localText: "",
                revision: 0,
                updatedAt: "",
                inFlight: null,
                conflict: null,
                flushTimer: null,
            });
        }
        return state.documents.get(boardId);
    }

    function updateBoardMetadata(document) {
        const previous = state.boards.get(document.id) || {};
        state.boards.set(document.id, {
            ...previous,
            id: document.id,
            number: document.number,
            name: document.name,
            revision: document.revision,
            updatedAt: document.updatedAt,
            unread: document.id === state.activeBoardId ? false : previous.unread,
        });
    }

    function renderBoardTabs() {
        boardTabs.replaceChildren();
        const boards = [...state.boards.values()].sort(
            (left, right) => left.number - right.number,
        );
        for (const board of boards) {
            const tabContainer = document.createElement("div");
            tabContainer.className = "board-tab-container";
            tabContainer.dataset.boardId = board.id;

            const selectButton = document.createElement("button");
            selectButton.type = "button";
            selectButton.className = "board-tab";
            selectButton.textContent = board.name;
            selectButton.title = board.name;
            selectButton.setAttribute(
                "aria-selected",
                String(board.id === state.activeBoardId),
            );
            selectButton.addEventListener("click", () => selectBoard(board.id));

            let cornerControl;
            if (board.unread) {
                cornerControl = document.createElement("span");
                cornerControl.className = "board-tab-corner board-tab-update";
                cornerControl.setAttribute("aria-label", `${board.name}有新内容`);
            } else {
                cornerControl = document.createElement("button");
                cornerControl.type = "button";
                cornerControl.className = "board-tab-corner board-tab-close";
                cornerControl.textContent = "×";
                cornerControl.setAttribute("aria-label", `关闭并删除${board.name}`);
                cornerControl.addEventListener(
                    "click",
                    () => requestDeleteBoard(board.id),
                );
            }

            tabContainer.append(selectButton, cornerControl);
            boardTabs.append(tabContainer);
        }
        updateBoardControls();
    }

    function requestRenameBoard(boardId) {
        const board = state.boards.get(boardId);
        if (!board || !socketReady()) {
            showToast("连接恢复后才能重命名共享板。", true);
            return;
        }
        state.renameDialogBoardId = boardId;
        renameBoardInput.value = board.name;
        renameBoardDialog.showModal();
        renameBoardInput.focus();
        renameBoardInput.select();
    }

    function submitBoardRename() {
        const boardId = state.renameDialogBoardId;
        const board = state.boards.get(boardId);
        const normalizedName = renameBoardInput.value.trim().replace(/\s+/g, " ");
        if (!normalizedName || [...normalizedName].length > 24) {
            showToast("名称不能为空，且最多为 24 个字符。", true);
            renameBoardInput.focus();
            return;
        }
        renameBoardDialog.close();
        state.renameDialogBoardId = null;
        if (!board || normalizedName === board.name) {
            return;
        }
        state.renamingBoardId = boardId;
        updateBoardControls();
        if (!send({
            type: "rename_board",
            boardId,
            name: normalizedName,
            clientId: state.clientId,
        })) {
            state.renamingBoardId = null;
            updateBoardControls();
            showToast("连接恢复后才能重命名共享板。", true);
        }
    }

    function closeRenameBoardDialog() {
        state.renameDialogBoardId = null;
        if (renameBoardDialog.open) {
            renameBoardDialog.close();
        }
    }

    function requestDeleteBoard(boardId) {
        if (state.boards.size <= 1) {
            showToast("至少需要保留一个共享板。", true);
            return;
        }
        const board = state.boards.get(boardId);
        if (
            !board ||
            !confirm(`确定删除“${board.name}”吗？其中的内容将无法恢复。`)
        ) {
            return;
        }
        state.deletingBoardId = board.id;
        updateBoardControls();
        if (!send({
            type: "delete_board",
            boardId: board.id,
            clientId: state.clientId,
        })) {
            state.deletingBoardId = null;
            updateBoardControls();
            showToast("连接恢复后才能删除共享板。", true);
        }
    }

    function activeDocument() {
        return state.activeBoardId
            ? ensureDocument(state.activeBoardId)
            : null;
    }

    function sharedTextValue() {
        return input.value.split(EMPTY_INPUT_SENTINEL).join("");
    }

    function keepEmptySharedInputEditable() {
        if (
            document.activeElement !== input ||
            input.disabled ||
            sharedTextValue()
        ) {
            return;
        }
        input.value = EMPTY_INPUT_SENTINEL;
        input.setSelectionRange(1, 1);
    }

    function setSharedTextValue(text) {
        input.value = text;
        keepEmptySharedInputEditable();
    }

    function updateMeta() {
        charCount.textContent = `${sharedTextValue().length} 字符`;
    }

    function updateSyncFromDocument(documentState) {
        if (!documentState) {
            setSync("等待共享板");
        } else if (documentState.conflict) {
            setSync("等待处理冲突");
        } else if (documentState.inFlight) {
            setSync("正在同步");
        } else if (documentState.localText !== documentState.serverText) {
            setSync(socketReady() ? "等待同步" : "离线修改待发送");
        } else {
            setSync("已同步");
        }
    }

    function renderActiveDocument({ preserveSelection = false } = {}) {
        const documentState = activeDocument();
        if (!documentState?.initialized) {
            input.value = "";
            input.disabled = true;
            setSync("正在读取共享板");
            updateMeta();
            conflictPanel.hidden = true;
            return;
        }

        const wasFocused = document.activeElement === input;
        const selectionStart = input.selectionStart;
        const selectionEnd = input.selectionEnd;
        input.disabled = false;
        setSharedTextValue(documentState.localText);
        if (preserveSelection && wasFocused) {
            focusElement(input);
            input.setSelectionRange(
                Math.min(selectionStart, input.value.length),
                Math.min(selectionEnd, input.value.length),
            );
        }
        updateMeta();
        conflictPanel.hidden = !documentState.conflict;
        updateSyncFromDocument(documentState);
    }

    function focusElement(element) {
        try {
            element.focus({ preventScroll: true });
        } catch (_error) {
            element.focus();
        }
    }

    function focusSharedInput({ moveCursorToEnd = true } = {}) {
        if (
            state.workMode !== "board" ||
            input.disabled ||
            !activeDocument()?.initialized
        ) {
            return false;
        }
        focusElement(input);
        keepEmptySharedInputEditable();
        if (moveCursorToEnd) {
            const cursor = input.value.length;
            input.setSelectionRange(cursor, cursor);
        }
        return document.activeElement === input;
    }

    function applyBoardSnapshot(document) {
        const documentState = ensureDocument(document.id);
        const existingDirty =
            documentState.initialized &&
            documentState.localText !== documentState.serverText;
        const draft = getDraft(document.id);

        if (
            existingDirty &&
            document.revision !== documentState.revision
        ) {
            documentState.conflict = {
                localText: documentState.localText,
                remote: document,
            };
            documentState.serverText = document.text;
            documentState.revision = document.revision;
            documentState.updatedAt = document.updatedAt;
        } else if (
            !documentState.initialized &&
            draft &&
            draft.text !== document.text
        ) {
            documentState.serverText = document.text;
            documentState.revision = document.revision;
            documentState.updatedAt = document.updatedAt;
            if (draft.baseRevision === document.revision) {
                documentState.localText = draft.text;
            } else {
                documentState.localText = draft.text;
                documentState.conflict = {
                    localText: draft.text,
                    remote: document,
                };
            }
        } else if (!existingDirty) {
            documentState.serverText = document.text;
            documentState.localText = document.text;
            documentState.revision = document.revision;
            documentState.updatedAt = document.updatedAt;
            documentState.conflict = null;
            clearDraft(document.id);
        }

        documentState.initialized = true;
        updateBoardMetadata(document);
        if (document.id === state.activeBoardId) {
            renderActiveDocument({ preserveSelection: true });
        }
        renderBoardTabs();
        if (
            !documentState.conflict &&
            documentState.localText !== documentState.serverText
        ) {
            scheduleFlush(document.id, 30);
        }
    }

    function selectBoard(boardId) {
        if (!state.boards.has(boardId)) {
            return;
        }
        if (boardId !== state.activeBoardId) {
            input.blur();
        }
        const current = activeDocument();
        if (current?.initialized) {
            current.localText = sharedTextValue();
            if (current.localText !== current.serverText) {
                saveDraft(
                    state.activeBoardId,
                    current.localText,
                    current.revision,
                );
                scheduleFlush(state.activeBoardId, 0);
            }
        }

        state.activeBoardId = boardId;
        localStorage.setItem(ACTIVE_BOARD_KEY, boardId);
        const board = state.boards.get(boardId);
        board.unread = false;
        renderBoardTabs();
        renderActiveDocument();
        send({ type: "select_board", boardId });
    }

    function scheduleFlush(boardId, delay = 200) {
        const documentState = ensureDocument(boardId);
        clearTimeout(documentState.flushTimer);
        documentState.flushTimer = setTimeout(() => flush(boardId), delay);
    }

    function flush(boardId) {
        const documentState = ensureDocument(boardId);
        clearTimeout(documentState.flushTimer);
        documentState.flushTimer = null;
        if (
            !documentState.initialized ||
            documentState.conflict ||
            documentState.inFlight
        ) {
            return;
        }
        if (!socketReady()) {
            if (boardId === state.activeBoardId) {
                setSync("离线修改待发送");
            }
            return;
        }
        if (documentState.localText === documentState.serverText) {
            clearDraft(boardId);
            if (boardId === state.activeBoardId) {
                setSync("已同步");
            }
            return;
        }
        if (
            textEncoder.encode(documentState.localText).byteLength >
            MAX_TEXT_BYTES
        ) {
            if (boardId === state.activeBoardId) {
                setSync("内容超过 1 MiB");
                showToast("内容已达到上限，暂未同步。", true);
            }
            return;
        }

        documentState.inFlight = {
            text: documentState.localText,
            baseRevision: documentState.revision,
        };
        saveDraft(
            boardId,
            documentState.inFlight.text,
            documentState.inFlight.baseRevision,
        );
        send({
            type: "update",
            clientId: state.clientId,
            boardId,
            baseRevision: documentState.inFlight.baseRevision,
            text: documentState.inFlight.text,
        });
        if (boardId === state.activeBoardId) {
            setSync("正在同步");
        }
    }

    function handleAcknowledgement(message) {
        const documentState = ensureDocument(message.boardId);
        if (!documentState.inFlight) {
            return;
        }
        documentState.serverText = documentState.inFlight.text;
        documentState.revision = message.revision;
        documentState.updatedAt = message.updatedAt;
        documentState.inFlight = null;
        const board = state.boards.get(message.boardId);
        if (board) {
            board.revision = message.revision;
            board.updatedAt = message.updatedAt;
        }
        if (documentState.localText === documentState.serverText) {
            clearDraft(message.boardId);
        } else {
            scheduleFlush(message.boardId, 30);
        }
        if (message.boardId === state.activeBoardId) {
            updateSyncFromDocument(documentState);
        }
        renderBoardTabs();
    }

    function handleRemoteUpdate(message) {
        const document = message.document;
        const documentState = ensureDocument(document.id);
        const hasLocalChange =
            documentState.inFlight ||
            documentState.localText !== documentState.serverText;

        if (hasLocalChange && document.text !== documentState.localText) {
            documentState.inFlight = null;
            documentState.conflict = {
                localText: documentState.localText,
                remote: document,
            };
            documentState.serverText = document.text;
            documentState.revision = document.revision;
            documentState.updatedAt = document.updatedAt;
        } else {
            documentState.serverText = document.text;
            documentState.localText = document.text;
            documentState.revision = document.revision;
            documentState.updatedAt = document.updatedAt;
            documentState.inFlight = null;
            documentState.conflict = null;
            clearDraft(document.id);
        }
        documentState.initialized = true;
        updateBoardMetadata(document);
        if (document.id === state.activeBoardId) {
            renderActiveDocument({ preserveSelection: true });
        }
        renderBoardTabs();
    }

    function handleBoardDeleted(message) {
        const boardId = message.boardId;
        const wasActive = boardId === state.activeBoardId;
        const deletedDocument = state.documents.get(boardId);
        if (deletedDocument) {
            clearTimeout(deletedDocument.flushTimer);
        }
        state.boards.delete(boardId);
        state.documents.delete(boardId);
        clearDraft(boardId);
        if (state.deletingBoardId === boardId) {
            state.deletingBoardId = null;
        }
        if (state.renameDialogBoardId === boardId) {
            closeRenameBoardDialog();
        }

        if (wasActive && message.fallback) {
            input.blur();
            state.activeBoardId = message.fallback.id;
            localStorage.setItem(ACTIVE_BOARD_KEY, message.fallback.id);
            applyBoardSnapshot(message.fallback);
            send({
                type: "select_board",
                boardId: message.fallback.id,
            });
        } else {
            renderBoardTabs();
        }
        updateBoardControls();
        if (message.sourceId === state.clientId) {
            showToast("当前共享板已删除。");
        }
    }

    function resolveWithRemote() {
        const documentState = activeDocument();
        if (!documentState?.conflict) {
            return;
        }
        const remote = documentState.conflict.remote;
        documentState.serverText = remote.text;
        documentState.localText = remote.text;
        documentState.revision = remote.revision;
        documentState.updatedAt = remote.updatedAt;
        documentState.conflict = null;
        documentState.inFlight = null;
        clearDraft(state.activeBoardId);
        renderActiveDocument();
        showToast("已采用最新内容。");
    }

    function resolveWithLocal() {
        const documentState = activeDocument();
        if (!documentState?.conflict) {
            return;
        }
        const localText = documentState.conflict.localText;
        const remote = documentState.conflict.remote;
        documentState.serverText = remote.text;
        documentState.localText = localText;
        documentState.revision = remote.revision;
        documentState.updatedAt = remote.updatedAt;
        documentState.conflict = null;
        documentState.inFlight = null;
        saveDraft(state.activeBoardId, localText, remote.revision);
        renderActiveDocument();
        scheduleFlush(state.activeBoardId, 0);
    }

    function notifyWhenLimitIsReached() {
        const byteLength = textEncoder.encode(sharedTextValue()).byteLength;
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

    function applyWorkMode(mode) {
        if (mode !== "board" && mode !== "direct") {
            mode = "board";
        }
        const leavingDirect =
            mode === "board" &&
            (state.direct.active || state.direct.starting);
        if (leavingDirect) {
            stopDirect();
        }
        state.workMode = mode;
        localStorage.setItem(WORK_MODE_KEY, mode);
        const boardMode = mode === "board";
        boardWorkspace.hidden = !boardMode;
        directWorkspace.hidden = boardMode;
        boardModeButton.setAttribute("aria-pressed", String(boardMode));
        directModeButton.setAttribute("aria-pressed", String(!boardMode));
        if (boardMode && state.activeBoardId) {
            renderActiveDocument();
        }
        renderDirectStatus();
    }

    function requestWorkMode(mode) {
        applyWorkMode(mode);
    }

    function graphemes(text) {
        if (globalThis.Intl?.Segmenter) {
            return [...new Intl.Segmenter(undefined, {
                granularity: "grapheme",
            }).segment(text)].map((item) => item.segment);
        }
        return [...text];
    }

    function commonPrefixLength(left, right) {
        const limit = Math.min(left.length, right.length);
        let index = 0;
        while (index < limit && left[index] === right[index]) {
            index += 1;
        }
        return index;
    }

    function renderDirectStatus() {
        directSurface.classList.remove("is-active", "is-waiting", "is-error");
        const activeDirectKeysEnabled =
            !isLocal &&
            state.direct.active &&
            !state.direct.pending &&
            state.direct.pendingKeys.length === 0 &&
            !state.direct.composing &&
            directCapture.value === state.direct.acknowledgedText;
        const idleDirectKeysEnabled =
            !isLocal &&
            !state.direct.active &&
            !state.direct.starting &&
            !state.direct.serverStatus.active &&
            socketReady();
        const directKeysEnabled =
            activeDirectKeysEnabled || idleDirectKeysEnabled;
        if (directBackspaceButton) {
            directBackspaceButton.disabled = !directKeysEnabled;
        }
        if (directEnterButton) {
            directEnterButton.disabled = !directKeysEnabled;
        }
        if (directToggleButton) {
            const toggled = state.direct.active || state.direct.starting;
            directToggleButton.setAttribute("aria-pressed", String(toggled));
            directToggleButton.setAttribute(
                "aria-label",
                toggled ? "停止直输" : "开启直输",
            );
        }
        if (isLocal) {
            if (state.direct.serverStatus.active) {
                directSurface.classList.add("is-active");
                directStatus.textContent =
                    `${state.direct.serverStatus.deviceName || "手机"} 正在直输`;
                directHint.textContent = "电脑当前窗口正在接收模拟输入";
            } else {
                directStatus.textContent = "请在手机上开启直输";
                directHint.textContent = "电脑会显示当前直输状态";
            }
            return;
        }
        if (state.direct.active) {
            directSurface.classList.add("is-active");
            directStatus.textContent = "正在直输";
            directHint.textContent = "再次点击波浪即可停止";
        } else if (state.direct.starting) {
            directSurface.classList.add("is-waiting");
            directStatus.textContent = "正在连接电脑窗口";
            directHint.textContent = "请保持电脑目标输入框获得焦点";
        } else if (state.direct.serverStatus.active) {
            directSurface.classList.add("is-waiting");
            directStatus.textContent =
                `${state.direct.serverStatus.deviceName || "另一台设备"} 正在直输`;
            directHint.textContent = "当前直输会话结束后可以重新开启";
        } else {
            directStatus.textContent = "点击波浪开启直输";
            directHint.textContent = "开启后可使用手机语音输入";
        }
    }

    function resetDirectState({ clearCapture = true } = {}) {
        clearTimeout(state.direct.flushTimer);
        state.direct.flushTimer = null;
        state.direct.starting = false;
        state.direct.active = false;
        state.direct.sessionId = null;
        state.direct.sequence = 0;
        state.direct.acknowledgedText = "";
        state.direct.pending = null;
        state.direct.pendingKeys = [];
        state.direct.composing = false;
        if (directCapture && clearCapture) {
            directCapture.value = "";
            directCapture.blur();
        }
        renderDirectStatus();
    }

    function startDirect() {
        if (
            isLocal ||
            state.workMode !== "direct" ||
            state.direct.starting ||
            state.direct.active
        ) {
            return;
        }
        if (!socketReady()) {
            showToast("连接恢复后才能开启直输。", true);
            directCapture.blur();
            return;
        }
        if (state.direct.serverStatus.active) {
            showToast("另一台设备正在使用直输。", true);
            directCapture.blur();
            return;
        }
        directCapture.value = "";
        state.direct.starting = true;
        state.direct.acknowledgedText = "";
        state.direct.pending = null;
        renderDirectStatus();
        send({ type: "direct_start" });
    }

    function stopDirect() {
        if (state.direct.active || state.direct.starting) {
            send({
                type: "direct_stop",
                sessionId: state.direct.sessionId,
            });
        }
        resetDirectState();
    }

    function scheduleDirectFlush(delay = DIRECT_FLUSH_DELAY_MS) {
        if (state.direct.flushTimer !== null) {
            return;
        }
        state.direct.flushTimer = setTimeout(flushDirect, delay);
    }

    function flushDirect() {
        clearTimeout(state.direct.flushTimer);
        state.direct.flushTimer = null;
        if (
            !state.direct.active ||
            state.direct.pending ||
            state.direct.pendingKeys.length > 0 ||
            state.direct.composing ||
            !socketReady()
        ) {
            return;
        }
        const desiredText = directCapture.value;
        const acknowledged = state.direct.acknowledgedText;
        if (desiredText === acknowledged) {
            return;
        }
        const acknowledgedParts = graphemes(acknowledged);
        const desiredParts = graphemes(desiredText);
        const prefixLength = commonPrefixLength(
            acknowledgedParts,
            desiredParts,
        );
        const removed = acknowledgedParts.slice(prefixLength);
        const inserted = desiredParts.slice(prefixLength).join("");
        const sequence = state.direct.sequence + 1;
        state.direct.sequence = sequence;
        state.direct.pending = {
            sequence,
            targetText: desiredText,
        };
        renderDirectStatus();
        send({
            type: "direct_input",
            sessionId: state.direct.sessionId,
            sequence,
            deleteCount: removed.length,
            text: inserted,
        });
    }

    function handleDirectAcknowledgement(message) {
        if (
            !state.direct.pending ||
            message.sessionId !== state.direct.sessionId ||
            message.sequence !== state.direct.pending.sequence
        ) {
            return;
        }
        state.direct.acknowledgedText = state.direct.pending.targetText;
        state.direct.pending = null;
        renderDirectStatus();
        if (
            state.direct.pendingKeys.length === 0 &&
            directCapture.value !== state.direct.acknowledgedText
        ) {
            scheduleDirectFlush(25);
        }
    }

    function applyDirectKeyToText(text, key) {
        if (key === "enter") {
            return "";
        }
        const parts = graphemes(text);
        parts.pop();
        return parts.join("");
    }

    async function sendDirectKeyWithoutSession(key) {
        const response = await fetch("/api/actions/key", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key }),
        });
        if (response.status === 401) {
            location.assign("/pair");
            return false;
        }
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error?.message ?? "发送电脑按键失败。");
        }
        return true;
    }

    async function triggerDirectKey(key) {
        if (!socketReady()) {
            showToast("连接恢复后才能发送电脑按键。", true);
            return;
        }
        if (!state.direct.active) {
            if (state.direct.starting || state.direct.serverStatus.active) {
                showToast("另一台设备正在使用直输。", true);
                return;
            }
            try {
                if (await sendDirectKeyWithoutSession(key)) {
                    showToast(key === "enter"
                        ? "已发送电脑回车。"
                        : "已发送电脑退格。");
                }
            } catch (error) {
                showToast(error.message, true);
            }
            return;
        }
        if (
            state.direct.pending ||
            state.direct.pendingKeys.length > 0 ||
            state.direct.composing ||
            directCapture.value !== state.direct.acknowledgedText
        ) {
            showToast("文字仍在发送，请稍后再按。", true);
            return;
        }
        const requestId = globalThis.crypto?.randomUUID?.() ??
            `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        state.direct.pendingKeys.push({ requestId, key });
        directCapture.value = applyDirectKeyToText(directCapture.value, key);
        renderDirectStatus();
        if (!send({
            type: "direct_key",
            sessionId: state.direct.sessionId,
            requestId,
            key,
        })) {
            state.direct.pendingKeys = [];
            resetDirectState();
            showToast("连接已断开，直输已停止。", true);
        }
        directCapture.focus({ preventScroll: true });
    }

    function handleDirectKeyAcknowledgement(message) {
        const pendingIndex = state.direct.pendingKeys.findIndex(
            (item) => item.requestId === message.requestId,
        );
        if (
            pendingIndex < 0 ||
            message.sessionId !== state.direct.sessionId
        ) {
            return;
        }
        const [pendingKey] = state.direct.pendingKeys.splice(pendingIndex, 1);
        state.direct.acknowledgedText = applyDirectKeyToText(
            state.direct.acknowledgedText,
            pendingKey.key,
        );
        renderDirectStatus();
        if (
            !state.direct.pending &&
            state.direct.pendingKeys.length === 0 &&
            directCapture.value !== state.direct.acknowledgedText
        ) {
            scheduleDirectFlush(25);
        }
    }

    function handleSocketError(message) {
        const directCodes = new Set([
            "direct_busy",
            "direct_inactive",
            "direct_input_failed",
            "direct_key_failed",
            "focus_changed",
            "invalid_direct_key",
            "remote_only",
            "sequence_gap",
            "target_unavailable",
        ]);
        if (directCodes.has(message.code)) {
            resetDirectState();
        }
        if (
            message.code === "board_not_found" ||
            message.code === "last_board_required" ||
            message.code === "invalid_board_name"
        ) {
            state.deletingBoardId = null;
            state.renamingBoardId = null;
            updateBoardControls();
        }
        showToast(message.message || "操作失败。", true);
    }

    function handleMessage(message) {
        if (message.type === "snapshot") {
            state.initialized = true;
            state.boards.clear();
            for (const board of message.boards || []) {
                state.boards.set(board.id, { ...board, unread: false });
            }
            state.direct.serverStatus = message.direct || { active: false };

            const savedBoard = localStorage.getItem(ACTIVE_BOARD_KEY);
            const preferredBoard = state.boards.has(savedBoard)
                ? savedBoard
                : message.document.id;
            state.activeBoardId = preferredBoard;
            applyBoardSnapshot(message.document);
            renderBoardTabs();
            if (preferredBoard !== message.document.id) {
                renderActiveDocument();
                send({ type: "select_board", boardId: preferredBoard });
            } else {
                renderActiveDocument();
            }
            for (const boardId of state.documents.keys()) {
                const documentState = ensureDocument(boardId);
                if (
                    documentState.initialized &&
                    documentState.localText !== documentState.serverText
                ) {
                    scheduleFlush(boardId, 30);
                }
            }
            renderDirectStatus();
            return;
        }
        if (message.type === "board_snapshot") {
            applyBoardSnapshot(message.document);
            return;
        }
        if (message.type === "ack") {
            handleAcknowledgement(message);
            return;
        }
        if (message.type === "update") {
            handleRemoteUpdate(message);
            return;
        }
        if (message.type === "conflict") {
            const documentState = ensureDocument(message.boardId);
            documentState.inFlight = null;
            documentState.conflict = {
                localText: documentState.localText,
                remote: message.document,
            };
            documentState.serverText = message.document.text;
            documentState.revision = message.document.revision;
            documentState.updatedAt = message.document.updatedAt;
            updateBoardMetadata(message.document);
            if (message.boardId === state.activeBoardId) {
                renderActiveDocument();
            }
            renderBoardTabs();
            return;
        }
        if (message.type === "board_updated") {
            const previous = state.boards.get(message.board.id) || {};
            state.boards.set(message.board.id, {
                ...previous,
                ...message.board,
                unread: message.board.id !== state.activeBoardId,
            });
            renderBoardTabs();
            return;
        }
        if (message.type === "board_created") {
            state.boards.set(message.board.id, {
                ...message.board,
                unread: message.sourceId !== state.clientId,
            });
            renderBoardTabs();
            if (message.sourceId === state.clientId) {
                selectBoard(message.board.id);
            }
            return;
        }
        if (message.type === "board_renamed") {
            const previous = state.boards.get(message.board.id);
            if (previous) {
                state.boards.set(message.board.id, {
                    ...previous,
                    ...message.board,
                });
            }
            if (state.renamingBoardId === message.board.id) {
                state.renamingBoardId = null;
            }
            renderBoardTabs();
            if (message.sourceId === state.clientId) {
                showToast("共享板已重命名。");
            }
            return;
        }
        if (message.type === "board_deleted") {
            handleBoardDeleted(message);
            return;
        }
        if (message.type === "direct_status") {
            state.direct.serverStatus = message.direct || { active: false };
            if (
                state.direct.active &&
                !state.direct.serverStatus.active
            ) {
                resetDirectState();
            }
            renderDirectStatus();
            return;
        }
        if (message.type === "direct_started") {
            state.direct.starting = false;
            state.direct.active = true;
            state.direct.sessionId = message.sessionId;
            state.direct.sequence = 0;
            state.direct.acknowledgedText = "";
            state.direct.pending = null;
            state.direct.serverStatus = { active: true };
            renderDirectStatus();
            if (directCapture.value) {
                scheduleDirectFlush(20);
            }
            return;
        }
        if (message.type === "direct_stopped") {
            state.direct.serverStatus = { active: false };
            resetDirectState();
            return;
        }
        if (message.type === "direct_ack") {
            handleDirectAcknowledgement(message);
            return;
        }
        if (message.type === "direct_key_ack") {
            handleDirectKeyAcknowledgement(message);
            return;
        }
        if (message.type === "error") {
            handleSocketError(message);
        }
    }

    function connect() {
        clearTimeout(state.reconnectTimer);
        setConnection("connecting", "未连接");
        const scheme = location.protocol === "https:" ? "wss" : "ws";
        const socket = new WebSocket(`${scheme}://${location.host}/ws`);
        state.socket = socket;

        socket.addEventListener("open", () => {
            state.reconnectAttempt = 0;
            setConnection("online", "已连接");
        });

        socket.addEventListener("message", (event) => {
            try {
                handleMessage(JSON.parse(event.data));
            } catch (_error) {
                showToast("收到无法识别的同步消息。", true);
            }
        });

        socket.addEventListener("close", (event) => {
            if (state.socket !== socket) {
                return;
            }
            setConnection("offline", "未连接");
            resetDirectState();
            for (const [boardId, documentState] of state.documents) {
                documentState.inFlight = null;
                if (
                    documentState.initialized &&
                    documentState.localText !== documentState.serverText
                ) {
                    saveDraft(
                        boardId,
                        documentState.localText,
                        documentState.revision,
                    );
                }
            }
            updateSyncFromDocument(activeDocument());
            if (event.code === 1008) {
                setTimeout(() => location.assign("/pair"), 500);
                return;
            }
            if (!state.manuallyClosed) {
                const delay = Math.min(
                    10000,
                    700 * (2 ** state.reconnectAttempt),
                );
                state.reconnectAttempt += 1;
                state.reconnectTimer = setTimeout(connect, delay);
            }
        });
    }

    function copyUsingSelection() {
        const hadFocus = document.activeElement === input;
        const selectionStart = input.selectionStart;
        const selectionEnd = input.selectionEnd;
        const selectionDirection = input.selectionDirection;
        input.focus({ preventScroll: true });
        input.select();
        const copied = document.execCommand("copy");
        input.setSelectionRange(
            selectionStart,
            selectionEnd,
            selectionDirection,
        );
        if (!hadFocus) {
            input.blur();
        }
        return copied;
    }

    async function copyToCurrentDevice() {
        if (globalThis.isSecureContext && navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(sharedTextValue());
            return;
        }
        try {
            if (copyUsingSelection()) {
                return;
            }
        } catch (_error) {
            // Show manual-copy guidance below.
        }
        throw new Error("当前浏览器未允许自动复制，请长按文本手动复制。");
    }

    async function pasteIntoComputer() {
        const response = await fetch("/api/actions/paste", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ boardId: state.activeBoardId }),
        });
        if (response.status === 401) {
            location.assign("/pair");
            return;
        }
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error?.message ?? "插入到电脑当前窗口失败。");
        }
    }

    async function sendEnterToComputer() {
        const response = await fetch("/api/actions/enter", {
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
            throw new Error(payload.error?.message ?? "发送电脑回车失败。");
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
        aboutBackdrop.hidden = !open;
        document.body.classList.toggle("about-open", open);
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
            setAboutOpen(false);
        }
    }

    input.addEventListener("input", () => {
        const documentState = activeDocument();
        if (!documentState) {
            return;
        }
        documentState.localText = sharedTextValue();
        if (input.value !== documentState.localText && documentState.localText) {
            const cursor = Math.max(
                0,
                (input.selectionStart ?? input.value.length) -
                    input.value
                        .slice(0, input.selectionStart ?? input.value.length)
                        .split(EMPTY_INPUT_SENTINEL).length +
                    1,
            );
            input.value = documentState.localText;
            input.setSelectionRange(cursor, cursor);
        }
        keepEmptySharedInputEditable();
        updateMeta();
        notifyWhenLimitIsReached();
        saveDraft(
            state.activeBoardId,
            documentState.localText,
            documentState.revision,
        );
        if (documentState.conflict) {
            documentState.conflict.localText = documentState.localText;
            setSync("冲突期间的本机修改已暂存");
            return;
        }
        updateSyncFromDocument(documentState);
        scheduleFlush(state.activeBoardId);
    });

    useRemoteButton.addEventListener("click", resolveWithRemote);
    keepLocalButton.addEventListener("click", resolveWithLocal);
    aboutButton.addEventListener("click", () => {
        setAboutOpen(aboutPanel.hidden);
    });
    closeAboutButton.addEventListener("click", () => setAboutOpen(false));
    aboutBackdrop.addEventListener("click", () => setAboutOpen(false));
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !aboutPanel.hidden) {
            setAboutOpen(false);
        }
    });
    boardModeButton.addEventListener("click", () => requestWorkMode("board"));
    directModeButton.addEventListener("click", () => requestWorkMode("direct"));
    addBoardButton.addEventListener("click", () => {
        if (state.boards.size >= state.maxBoards) {
            showToast(`最多只能创建 ${state.maxBoards} 个共享板。`, true);
            return;
        }
        if (!send({ type: "create_board", clientId: state.clientId })) {
            showToast("连接恢复后才能新建共享板。", true);
        }
    });
    renameBoardButton.addEventListener("click", () => {
        requestRenameBoard(state.activeBoardId);
    });
    renameBoardForm.addEventListener("submit", (event) => {
        event.preventDefault();
        submitBoardRename();
    });
    cancelRenameBoardButton.addEventListener("click", closeRenameBoardDialog);
    renameBoardDialog.addEventListener("cancel", () => {
        state.renameDialogBoardId = null;
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

    if (copyButton) {
        copyButton.addEventListener("click", async () => {
            try {
                await copyToCurrentDevice();
                showToast("已复制到当前设备剪贴板。");
            } catch (error) {
                showToast(error.message, true);
            }
        });
    }

    if (remoteEnterButton) {
        remoteEnterButton.addEventListener("click", async () => {
            try {
                await sendEnterToComputer();
                showToast("已发送电脑回车。");
            } catch (error) {
                showToast(error.message, true);
            }
        });
    }

    if (pasteButton) {
        pasteButton.addEventListener("click", async () => {
            try {
                await pasteIntoComputer();
                showToast("已插入到电脑当前窗口。");
            } catch (error) {
                showToast(error.message, true);
            }
        });
    }

    clearButton.addEventListener("click", () => {
        if (!sharedTextValue() || !confirm("确定清空当前共享板吗？")) {
            return;
        }
        input.blur();
        input.value = "";
        input.dispatchEvent(new Event("input"));
    });

    if (directCapture) {
        directCapture.addEventListener("input", () => {
            renderDirectStatus();
            if (state.direct.active && !state.direct.composing) {
                scheduleDirectFlush();
            }
        });
        directCapture.addEventListener("compositionstart", () => {
            state.direct.composing = true;
            renderDirectStatus();
        });
        directCapture.addEventListener("compositionend", () => {
            state.direct.composing = false;
            renderDirectStatus();
            if (state.direct.active) {
                scheduleDirectFlush(25);
            }
        });
    }

    if (directToggleButton) {
        directToggleButton.addEventListener("pointerdown", (event) => {
            event.preventDefault();
        });
        directToggleButton.addEventListener("click", () => {
            if (state.direct.active || state.direct.starting) {
                stopDirect();
                return;
            }
            focusElement(directCapture);
            startDirect();
        });
    }

    input.addEventListener("beforeinput", (event) => {
        if (
            input.value === EMPTY_INPUT_SENTINEL &&
            event.inputType?.startsWith("insert")
        ) {
            input.value = "";
        }
    });
    input.addEventListener("compositionstart", () => {
        if (input.value === EMPTY_INPUT_SENTINEL) {
            input.value = "";
        }
    });
    input.addEventListener("compositionend", keepEmptySharedInputEditable);
    input.addEventListener("focus", keepEmptySharedInputEditable);
    input.addEventListener("blur", () => {
        if (input.value === EMPTY_INPUT_SENTINEL) {
            input.value = "";
        }
    });
    editorCard.addEventListener("click", (event) => {
        if (event.target === editorCard) {
            focusSharedInput();
        }
    });

    for (const [button, key] of [
        [directBackspaceButton, "backspace"],
        [directEnterButton, "enter"],
    ]) {
        if (!button) {
            continue;
        }
        button.addEventListener("pointerdown", (event) => {
            event.preventDefault();
        });
        button.addEventListener("click", () => {
            void triggerDirectKey(key);
        });
    }

    window.addEventListener("pagehide", () => {
        state.manuallyClosed = true;
        for (const [boardId, documentState] of state.documents) {
            if (
                documentState.initialized &&
                documentState.localText !== documentState.serverText
            ) {
                saveDraft(
                    boardId,
                    documentState.localText,
                    documentState.revision,
                );
            }
        }
        state.socket?.close();
    });

    applyWorkMode(state.workMode);
    setConnection("connecting", "未连接");
    updateMeta();
    connect();
})();
