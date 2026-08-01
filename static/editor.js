(() => {
    "use strict";

    const MAX_TEXT_BYTES = 1024 * 1024;
    const DRAFTS_KEY = "easytype.pendingDrafts.v2";
    const CLIENT_ID_KEY = "easytype.clientId.v1";
    const ACTIVE_BOARD_KEY = "easytype.activeBoard.v1";
    const WORK_MODE_KEY = "easytype.workMode.v1";
    const MARKDOWN_ASSIST_KEY = "easytype.markdownAssist.v1";
    const CLEAR_AFTER_PASTE_KEY = "easytype.clearAfterPaste.v1";
    const EDITOR_VISUAL_GAP_PX = 8;
    const DIRECT_FLUSH_DELAY_MS = 45;
    const HEARTBEAT_INTERVAL_MS = 25000;
    const HEARTBEAT_TIMEOUT_MS = 75000;
    const RESUME_PROBE_TIMEOUT_MS = 15000;
    const EMPTY_INPUT_SENTINEL = "\u200b";
    const textEncoder = new TextEncoder();

    const shell = document.querySelector(".editor-shell");
    const isLocal = shell.dataset.isLocal === "true";
    const input = document.getElementById("sharedText");
    const editorCard = input.closest(".editor-card");
    const editorToolbar = editorCard.querySelector(".editor-toolbar");
    const expandEditorButton = document.getElementById("expandEditorButton");
    const charCount = document.getElementById("charCount");
    const syncState = document.getElementById("syncState");
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
    const markdownAssistToggle = document.getElementById("markdownAssistToggle");
    const clearAfterPasteToggle = document.getElementById(
        "clearAfterPasteToggle",
    );
    const accessSettingsButton = document.getElementById("accessSettingsButton");
    const accessSettingsPanel = document.getElementById("accessSettingsPanel");
    const closeAccessSettingsButton = document.getElementById("closeAccessSettingsButton");
    const saveAccessModeButton = document.getElementById("saveAccessModeButton");
    const repairNetworkAccessButton = document.getElementById(
        "repairNetworkAccessButton",
    );
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
    let visualViewportBaselineHeight = Math.round(
        globalThis.visualViewport?.height ?? innerHeight,
    );

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
        connectionGeneration: 0,
        heartbeatTimer: null,
        heartbeatProbeTimer: null,
        lastServerMessageAt: 0,
        connectionStatus: "connecting",
        connectionLabel: "未连接",
        syncLabel: "等待连接",
        manuallyClosed: false,
        initialized: false,
        limitNoticeShown: false,
        editorImmersive: false,
        clearConfirmation: null,
        markdownAssistEnabled:
            localStorage.getItem(MARKDOWN_ASSIST_KEY) !== "false",
        clearAfterPasteEnabled:
            localStorage.getItem(CLEAR_AFTER_PASTE_KEY) !== "false",
        markdownKeydownHandled: false,
        sharedComposing: false,
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
        try {
            state.socket.send(JSON.stringify(message));
            return true;
        } catch (_error) {
            state.socket.close(4000, "Send failed");
            return false;
        }
    }

    function setConnection(status, label) {
        state.connectionStatus = status;
        state.connectionLabel = label;
        renderSyncSignal();
        renderDirectStatus();
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

    function renderSyncSignal() {
        let signal = "waiting";
        let label = state.syncLabel;
        if (state.connectionStatus !== "online") {
            signal = "error";
            label = state.connectionLabel || "未连接";
        } else if (label === "已同步") {
            signal = "synced";
        } else if (
            label.includes("失败") ||
            label.includes("冲突") ||
            label.includes("离线")
        ) {
            signal = "error";
        }
        syncState.dataset.state = signal;
        syncState.setAttribute("aria-label", label);
        syncState.title = label;
    }

    function setSync(label) {
        state.syncLabel = label;
        renderSyncSignal();
    }

    function showToast(message, isError = false) {
        toast.textContent = message;
        toast.setAttribute("aria-label", message);
        toast.className = isError ? "toast error" : "toast";
        toast.hidden = false;
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => {
            toast.hidden = true;
        }, 2800);
    }

    function showActionFeedback(
        button,
        message,
        {
            danger = false,
            duration = 1600,
            accessibleMessage = message,
            symbol = false,
        } = {},
    ) {
        const buttonRect = button.getBoundingClientRect();
        const feedbackCenter = Math.min(
            innerWidth - 58,
            Math.max(58, buttonRect.left + buttonRect.width / 2),
        );
        toast.replaceChildren();
        if (symbol) {
            const icon = document.createElementNS(
                "http://www.w3.org/2000/svg",
                "svg",
            );
            icon.classList.add("action-feedback-icon");
            icon.setAttribute("viewBox", "0 0 24 24");
            icon.setAttribute("aria-hidden", "true");
            const path = document.createElementNS(
                "http://www.w3.org/2000/svg",
                "path",
            );
            path.setAttribute(
                "d",
                "M18 6v3.5a5 5 0 0 1-5 5H5M9 10.5l-4 4 4 4",
            );
            icon.append(path);
            toast.append(icon);
        } else {
            toast.textContent = message;
        }
        toast.setAttribute("aria-label", accessibleMessage);
        toast.className = [
            "toast",
            "action-feedback",
            danger ? "danger" : "",
            symbol ? "symbol" : "",
        ].filter(Boolean).join(" ");
        toast.style.setProperty(
            "--easytype-action-feedback-left",
            `${Math.round(feedbackCenter)}px`,
        );
        toast.style.setProperty(
            "--easytype-action-feedback-top",
            `${Math.max(36, Math.round(buttonRect.top - 8))}px`,
        );
        toast.hidden = false;
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => {
            toast.hidden = true;
        }, duration);
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

    function ensureSharedCaretVisible() {
        if (document.activeElement !== input || input.disabled) {
            return;
        }
        requestAnimationFrame(() => {
            const cursor = input.selectionEnd ?? input.value.length;
            if (cursor >= input.value.length - 1) {
                input.scrollTop = Math.max(
                    0,
                    input.scrollHeight - input.clientHeight,
                );
            }
        });
    }

    function updateVisualViewport() {
        const viewport = globalThis.visualViewport;
        const inputFocused = document.activeElement === input;
        const toolbarHeight = Math.ceil(
            editorToolbar.getBoundingClientRect().height,
        );
        const inputStyle = getComputedStyle(input);
        const toolbarStyle = getComputedStyle(editorToolbar);
        const fontSize = Number.parseFloat(inputStyle.fontSize) || 16;
        const lineHeight = Number.parseFloat(inputStyle.lineHeight) || fontSize;
        const textEdgeInset = Math.max(0, (lineHeight - fontSize) / 2);
        const toolbarTopPadding =
            Number.parseFloat(toolbarStyle.paddingTop) || 0;
        const defaultBottomSpace = Math.max(
            0,
            Math.ceil(
                toolbarHeight -
                toolbarTopPadding -
                textEdgeInset +
                EDITOR_VISUAL_GAP_PX,
            ),
        );
        editorCard.style.setProperty(
            "--easytype-editor-toolbar-height",
            `${toolbarHeight}px`,
        );
        editorCard.style.setProperty(
            "--easytype-editor-bottom-space",
            `${defaultBottomSpace}px`,
        );
        if (!viewport || !matchMedia("(max-width: 700px)").matches) {
            document.body.classList.remove("keyboard-open");
            return;
        }
        if (!inputFocused) {
            visualViewportBaselineHeight = Math.max(
                visualViewportBaselineHeight,
                Math.round(viewport.height),
            );
        }
        const layoutInset = Math.max(
            0,
            innerHeight - viewport.height - viewport.offsetTop,
        );
        const heightLoss = Math.max(
            0,
            visualViewportBaselineHeight - viewport.height,
        );
        const keyboardOpen =
            inputFocused && Math.max(layoutInset, heightLoss) > 120;
        document.body.classList.toggle("keyboard-open", keyboardOpen);
        if (keyboardOpen) {
            const cardRect = editorCard.getBoundingClientRect();
            const preferredTop =
                viewport.offsetTop + viewport.height - toolbarHeight - 8;
            const cardMinimumTop = cardRect.top + 12;
            const cardMaximumTop = cardRect.bottom - toolbarHeight - 12;
            const toolbarTop = Math.max(
                cardMinimumTop,
                Math.min(preferredTop, cardMaximumTop),
            );
            const keyboardBottomSpace = Math.max(
                defaultBottomSpace,
                Math.ceil(
                    cardRect.bottom -
                    toolbarTop -
                    toolbarTopPadding -
                    textEdgeInset +
                    EDITOR_VISUAL_GAP_PX,
                ),
            );
            editorCard.style.setProperty(
                "--easytype-editor-bottom-space",
                `${keyboardBottomSpace}px`,
            );
            document.documentElement.style.setProperty(
                "--easytype-floating-toolbar-top",
                `${Math.round(toolbarTop)}px`,
            );
        } else {
            document.documentElement.style.removeProperty(
                "--easytype-floating-toolbar-top",
            );
        }
        ensureSharedCaretVisible();
    }

    function markdownLinePrefix(line) {
        let match = line.match(/^(\s*)([-+*])\s+\[([ xX])\]\s*(.*)$/);
        if (match) {
            return {
                empty: !match[4].trim(),
                prefix: `${match[1]}${match[2]} [ ] `,
            };
        }

        match = line.match(/^(\s*)([-+*])\s+(.*)$/);
        if (match) {
            return {
                empty: !match[3].trim(),
                prefix: `${match[1]}${match[2]} `,
            };
        }

        match = line.match(/^(\s*)(\d+)([.)])\s+(.*)$/);
        if (match) {
            return {
                empty: !match[4].trim(),
                prefix: `${match[1]}${Number(match[2]) + 1}${match[3]} `,
            };
        }

        match = line.match(/^(\s*(?:>\s*)+)(.*)$/);
        if (match) {
            return {
                empty: !match[2].trim(),
                prefix: match[1],
            };
        }
        return null;
    }

    function markdownLineBreakEdit() {
        if (
            !state.markdownAssistEnabled ||
            state.sharedComposing ||
            input.selectionStart !== input.selectionEnd
        ) {
            return null;
        }

        const text = sharedTextValue();
        const cursor = input.selectionStart;
        const lineStart = text.lastIndexOf("\n", cursor - 1) + 1;
        const nextLineBreak = text.indexOf("\n", cursor);
        const lineEnd = nextLineBreak === -1 ? text.length : nextLineBreak;
        const continuation = markdownLinePrefix(text.slice(lineStart, lineEnd));
        if (!continuation) {
            return null;
        }

        if (continuation.empty && cursor === lineEnd) {
            return {
                start: lineStart,
                end: lineEnd,
                text: "",
            };
        }

        return {
            start: cursor,
            end: cursor,
            text: `\n${continuation.prefix}`,
        };
    }

    function applyMarkdownLineBreak(edit) {
        input.setRangeText(edit.text, edit.start, edit.end, "end");
        input.dispatchEvent(new Event("input", { bubbles: true }));
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
            ensureSharedCaretVisible();
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
        if (mode === "direct" && state.editorImmersive) {
            setEditorImmersive(false);
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
            requestAnimationFrame(updateVisualViewport);
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
        if (!socketReady()) {
            directSurface.classList.add("is-error");
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

    function stopHeartbeat() {
        clearInterval(state.heartbeatTimer);
        clearTimeout(state.heartbeatProbeTimer);
        state.heartbeatTimer = null;
        state.heartbeatProbeTimer = null;
    }

    function noteServerMessage(socket) {
        if (state.socket !== socket) {
            return;
        }
        state.lastServerMessageAt = Date.now();
        clearTimeout(state.heartbeatProbeTimer);
        state.heartbeatProbeTimer = null;
    }

    function sendHeartbeat(socket, verifyResume = false) {
        if (
            state.socket !== socket ||
            socket.readyState !== WebSocket.OPEN
        ) {
            return;
        }
        try {
            socket.send('{"type":"ping"}');
        } catch (_error) {
            socket.close(4000, "Heartbeat send failed");
            return;
        }
        if (!verifyResume) {
            return;
        }
        const sentAt = Date.now();
        clearTimeout(state.heartbeatProbeTimer);
        state.heartbeatProbeTimer = setTimeout(() => {
            if (
                state.socket === socket &&
                socket.readyState === WebSocket.OPEN &&
                state.lastServerMessageAt < sentAt
            ) {
                socket.close(4000, "Heartbeat timeout");
            }
        }, RESUME_PROBE_TIMEOUT_MS);
    }

    function startHeartbeat(socket) {
        stopHeartbeat();
        state.lastServerMessageAt = Date.now();
        state.heartbeatTimer = setInterval(() => {
            if (
                document.hidden ||
                state.socket !== socket ||
                socket.readyState !== WebSocket.OPEN
            ) {
                return;
            }
            if (
                Date.now() - state.lastServerMessageAt >
                HEARTBEAT_TIMEOUT_MS
            ) {
                socket.close(4000, "Heartbeat timeout");
                return;
            }
            sendHeartbeat(socket);
        }, HEARTBEAT_INTERVAL_MS);
    }

    function connect() {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
        if (
            state.socket?.readyState === WebSocket.OPEN ||
            state.socket?.readyState === WebSocket.CONNECTING
        ) {
            return;
        }
        stopHeartbeat();
        const generation = state.connectionGeneration + 1;
        state.connectionGeneration = generation;
        setConnection("connecting", "未连接");
        const scheme = location.protocol === "https:" ? "wss" : "ws";
        const socket = new WebSocket(`${scheme}://${location.host}/ws`);
        state.socket = socket;

        socket.addEventListener("open", () => {
            if (
                state.socket !== socket ||
                state.connectionGeneration !== generation
            ) {
                socket.close(1000, "Superseded connection");
                return;
            }
            state.reconnectAttempt = 0;
            setConnection("online", "已连接");
            startHeartbeat(socket);
        });

        socket.addEventListener("message", (event) => {
            if (
                state.socket !== socket ||
                state.connectionGeneration !== generation
            ) {
                return;
            }
            noteServerMessage(socket);
            try {
                handleMessage(JSON.parse(event.data));
            } catch (_error) {
                showToast("收到无法识别的同步消息。", true);
            }
        });

        socket.addEventListener("close", (event) => {
            if (
                state.socket !== socket ||
                state.connectionGeneration !== generation
            ) {
                return;
            }
            stopHeartbeat();
            state.socket = null;
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
            return false;
        }
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error?.message ?? "插入到电脑当前窗口失败。");
        }
        return true;
    }

    function clearSharedBoard({ blur = true } = {}) {
        if (!sharedTextValue()) {
            return false;
        }
        if (blur) {
            input.blur();
        }
        input.value = "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        if (!blur) {
            ensureSharedCaretVisible();
        }
        return true;
    }

    function resetClearConfirmation() {
        if (state.clearConfirmation?.timer) {
            clearTimeout(state.clearConfirmation.timer);
        }
        state.clearConfirmation = null;
    }

    function requestClearSharedBoard() {
        if (!sharedTextValue()) {
            resetClearConfirmation();
            return;
        }
        const now = Date.now();
        const confirmation = state.clearConfirmation;
        if (
            confirmation?.boardId === state.activeBoardId &&
            confirmation.expiresAt > now
        ) {
            resetClearConfirmation();
            if (clearSharedBoard()) {
                showActionFeedback(clearButton, "已清空");
            }
            return;
        }

        resetClearConfirmation();
        const duration = 2600;
        const boardId = state.activeBoardId;
        const expiresAt = now + duration;
        const timer = setTimeout(() => {
            if (
                state.clearConfirmation?.boardId === boardId &&
                state.clearConfirmation.expiresAt === expiresAt
            ) {
                state.clearConfirmation = null;
            }
        }, duration);
        state.clearConfirmation = { boardId, expiresAt, timer };
        showActionFeedback(
            clearButton,
            "再次点击清空",
            { danger: true, duration },
        );
    }

    async function sendEnterToComputer() {
        const response = await fetch("/api/actions/enter", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        });
        if (response.status === 401) {
            location.assign("/pair");
            return false;
        }
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error?.message ?? "发送电脑回车失败。");
        }
        return true;
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

    function setEditorImmersive(open) {
        const nextOpen = Boolean(open);
        if (state.editorImmersive === nextOpen) {
            return;
        }
        const inputWasFocused = document.activeElement === input;
        state.editorImmersive = nextOpen;
        editorCard.classList.toggle("is-immersive", nextOpen);
        document.body.classList.toggle("editor-immersive", nextOpen);
        expandEditorButton.setAttribute("aria-pressed", String(nextOpen));
        expandEditorButton.setAttribute(
            "aria-label",
            nextOpen ? "退出全屏编辑" : "放大共享板",
        );
        if (nextOpen) {
            if (!aboutPanel.hidden) {
                setAboutOpen(false);
            }
            if (accessSettingsPanel && !accessSettingsPanel.hidden) {
                setAccessSettingsOpen(false);
            }
        }
        requestAnimationFrame(() => {
            updateVisualViewport();
            if (inputWasFocused) {
                focusElement(input);
                ensureSharedCaretVisible();
            }
        });
    }

    input.addEventListener("input", () => {
        const documentState = activeDocument();
        if (!documentState) {
            return;
        }
        resetClearConfirmation();
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
        ensureSharedCaretVisible();
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
    markdownAssistToggle.checked = state.markdownAssistEnabled;
    markdownAssistToggle.addEventListener("change", () => {
        state.markdownAssistEnabled = markdownAssistToggle.checked;
        localStorage.setItem(
            MARKDOWN_ASSIST_KEY,
            String(state.markdownAssistEnabled),
        );
        showToast(
            state.markdownAssistEnabled
                ? "已启用 Markdown 编辑辅助。"
                : "已关闭 Markdown 编辑辅助。",
        );
    });
    if (clearAfterPasteToggle) {
        clearAfterPasteToggle.checked = state.clearAfterPasteEnabled;
        clearAfterPasteToggle.addEventListener("change", () => {
            state.clearAfterPasteEnabled = clearAfterPasteToggle.checked;
            localStorage.setItem(
                CLEAR_AFTER_PASTE_KEY,
                String(state.clearAfterPasteEnabled),
            );
            showToast(
                state.clearAfterPasteEnabled
                    ? "插入成功后将自动清空当前共享板。"
                    : "已关闭插入后自动清空。",
            );
        });
    }
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }
        if (!aboutPanel.hidden) {
            setAboutOpen(false);
        } else if (state.editorImmersive) {
            setEditorImmersive(false);
        }
    });
    expandEditorButton.addEventListener("pointerdown", (event) => {
        event.preventDefault();
    });
    expandEditorButton.addEventListener("click", () => {
        setEditorImmersive(!state.editorImmersive);
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
        repairNetworkAccessButton.addEventListener("click", async () => {
            repairNetworkAccessButton.disabled = true;
            repairNetworkAccessButton.textContent = "等待 Windows 确认…";
            try {
                const response = await fetch(
                    "/api/admin/network-access/repair",
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: "{}",
                    },
                );
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(
                        payload.error?.message ?? "修复网络访问失败。",
                    );
                }
                showToast("网络访问权限已重新配置。");
            } catch (error) {
                showToast(error.message, true);
            } finally {
                repairNetworkAccessButton.disabled = false;
                repairNetworkAccessButton.textContent = "修复网络访问";
            }
        });
    }

    if (copyButton) {
        copyButton.addEventListener("click", async () => {
            try {
                await copyToCurrentDevice();
                showActionFeedback(copyButton, "已复制");
            } catch (error) {
                showToast(error.message, true);
            }
        });
    }

    if (remoteEnterButton) {
        remoteEnterButton.addEventListener("click", async () => {
            try {
                const sent = await sendEnterToComputer();
                if (!sent) {
                    return;
                }
                showActionFeedback(
                    remoteEnterButton,
                    "↵",
                    { accessibleMessage: "已发送回车", symbol: true },
                );
            } catch (error) {
                showToast(error.message, true);
            }
        });
    }

    if (pasteButton) {
        pasteButton.addEventListener("click", async () => {
            try {
                const inserted = await pasteIntoComputer();
                if (!inserted) {
                    return;
                }
                showActionFeedback(pasteButton, "已插入");
                if (state.clearAfterPasteEnabled) {
                    clearSharedBoard({ blur: false });
                }
            } catch (error) {
                showToast(error.message, true);
            }
        });
    }

    clearButton.addEventListener("click", requestClearSharedBoard);

    for (const button of [pasteButton, remoteEnterButton]) {
        button?.addEventListener("pointerdown", (event) => {
            if (
                document.activeElement === input &&
                document.body.classList.contains("keyboard-open")
            ) {
                event.preventDefault();
            }
        });
    }
    clearButton.addEventListener("pointerdown", (event) => {
        if (
            document.activeElement === input &&
            document.body.classList.contains("keyboard-open")
        ) {
            event.preventDefault();
        }
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
            ["insertLineBreak", "insertParagraph"].includes(event.inputType)
        ) {
            if (state.markdownKeydownHandled) {
                event.preventDefault();
                state.markdownKeydownHandled = false;
                return;
            }
            const edit = markdownLineBreakEdit();
            if (edit) {
                event.preventDefault();
                applyMarkdownLineBreak(edit);
                return;
            }
        }
        if (
            input.value === EMPTY_INPUT_SENTINEL &&
            event.inputType?.startsWith("insert")
        ) {
            input.value = "";
        }
    });
    input.addEventListener("compositionstart", () => {
        state.sharedComposing = true;
        if (input.value === EMPTY_INPUT_SENTINEL) {
            input.value = "";
        }
    });
    input.addEventListener("compositionend", () => {
        state.sharedComposing = false;
        keepEmptySharedInputEditable();
        ensureSharedCaretVisible();
    });
    input.addEventListener("keydown", (event) => {
        if (
            event.key !== "Enter" ||
            event.shiftKey ||
            event.ctrlKey ||
            event.altKey ||
            event.metaKey
        ) {
            return;
        }
        const edit = markdownLineBreakEdit();
        if (edit) {
            event.preventDefault();
            state.markdownKeydownHandled = true;
            applyMarkdownLineBreak(edit);
            setTimeout(() => {
                state.markdownKeydownHandled = false;
            }, 0);
        }
        setTimeout(ensureSharedCaretVisible, 0);
    });
    input.addEventListener("focus", () => {
        keepEmptySharedInputEditable();
        visualViewportBaselineHeight = Math.max(
            visualViewportBaselineHeight,
            Math.round(globalThis.visualViewport?.height ?? innerHeight),
        );
        requestAnimationFrame(updateVisualViewport);
    });
    input.addEventListener("blur", () => {
        if (input.value === EMPTY_INPUT_SENTINEL) {
            input.value = "";
        }
        setTimeout(updateVisualViewport, 0);
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
        stopHeartbeat();
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
    window.addEventListener("pageshow", () => {
        state.manuallyClosed = false;
        connect();
    });
    document.addEventListener("visibilitychange", () => {
        if (document.hidden || state.manuallyClosed) {
            clearTimeout(state.heartbeatProbeTimer);
            state.heartbeatProbeTimer = null;
            return;
        }
        if (socketReady()) {
            sendHeartbeat(state.socket, true);
            return;
        }
        connect();
    });

    updateVisualViewport();
    globalThis.visualViewport?.addEventListener(
        "resize",
        updateVisualViewport,
    );
    globalThis.visualViewport?.addEventListener(
        "scroll",
        updateVisualViewport,
    );
    window.addEventListener("resize", updateVisualViewport);
    window.addEventListener("scroll", updateVisualViewport, { passive: true });
    window.addEventListener("orientationchange", () => {
        setTimeout(() => {
            visualViewportBaselineHeight = Math.round(
                globalThis.visualViewport?.height ?? innerHeight,
            );
            updateVisualViewport();
        }, 250);
    });

    applyWorkMode(state.workMode);
    setConnection("connecting", "未连接");
    updateMeta();
    connect();
})();
