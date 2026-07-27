(() => {
    "use strict";

    const newPairingButton = document.getElementById("newPairingButton");
    const pairingEmpty = document.getElementById("pairingEmpty");
    const pairingCard = document.getElementById("pairingCard");
    const pairingQr = document.getElementById("pairingQr");
    const pairingUrl = document.getElementById("pairingUrl");
    const pairingExpiry = document.getElementById("pairingExpiry");
    const deviceList = document.getElementById("deviceList");
    const toast = document.getElementById("toast");

    function showToast(message, isError = false) {
        toast.textContent = message;
        toast.className = isError ? "toast error" : "toast";
        toast.hidden = false;
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => {
            toast.hidden = true;
        }, 2800);
    }

    async function requestJson(url, options = {}) {
        const response = await fetch(url, options);
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error?.message ?? "操作失败。");
        }
        return payload;
    }

    function formatDate(value) {
        const parsed = new Date(value);
        if (Number.isNaN(parsed.valueOf())) {
            return value;
        }
        return new Intl.DateTimeFormat("zh-CN", {
            dateStyle: "medium",
            timeStyle: "short",
        }).format(parsed);
    }

    async function loadDevices() {
        try {
            const payload = await requestJson("/api/admin/devices");
            renderDevices(payload.devices);
        } catch (error) {
            deviceList.textContent = "";
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.textContent = error.message;
            deviceList.append(empty);
        }
    }

    function renderDevices(devices) {
        deviceList.textContent = "";
        if (!devices.length) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.textContent = "还没有已授权设备。";
            deviceList.append(empty);
            return;
        }

        for (const device of devices) {
            const row = document.createElement("div");
            row.className = "device-item";

            const detail = document.createElement("div");
            const name = document.createElement("strong");
            name.textContent = device.name;
            const dates = document.createElement("span");
            dates.textContent = `最后使用：${formatDate(device.lastSeen)}`;
            detail.append(name, dates);

            const revoke = document.createElement("button");
            revoke.className = "button secondary";
            revoke.type = "button";
            revoke.textContent = "撤销授权";
            revoke.addEventListener("click", async () => {
                if (!confirm(`确定撤销“${device.name}”吗？`)) {
                    return;
                }
                revoke.disabled = true;
                try {
                    await requestJson(`/api/admin/devices/${encodeURIComponent(device.id)}`, {
                        method: "DELETE",
                        headers: { "Content-Type": "application/json" },
                        body: "{}",
                    });
                    showToast("设备授权已撤销。");
                    await loadDevices();
                } catch (error) {
                    revoke.disabled = false;
                    showToast(error.message, true);
                }
            });

            row.append(detail, revoke);
            deviceList.append(row);
        }
    }

    newPairingButton.addEventListener("click", async () => {
        newPairingButton.disabled = true;
        try {
            const payload = await requestJson("/api/admin/pairing-codes", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}",
            });
            pairingQr.src = `${payload.qrUrl}&v=${Date.now()}`;
            pairingUrl.textContent = payload.pairUrl;
            pairingExpiry.textContent = `有效期至 ${formatDate(payload.expiresAt)}，仅可使用一次。`;
            pairingEmpty.hidden = true;
            pairingCard.hidden = false;
        } catch (error) {
            showToast(error.message, true);
        } finally {
            newPairingButton.disabled = false;
        }
    });

    loadDevices();
})();
