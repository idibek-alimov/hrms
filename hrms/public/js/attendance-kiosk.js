/* No employee credentials or kiosk capability are stored in browser storage. */
(() => {
    const $ = (id) => document.getElementById(id);
    let lang = document.documentElement.lang, context, busy = false, retrying = false;
    const key = `attendance-kiosk-pending:${location.host}`;
    const t = (ru, tg) => lang === "tg" ? tg : ru;
    const pending = () => JSON.parse(localStorage.getItem(key) || "null");
    const status = (message, error = false) => { $("status").textContent = message; $("status").dataset.error = String(error); };
    function labels() {
        $("language").value = lang;
        for (const [id, ru, tg] of [
            ["title", "Приход и уход", "Омадан ва рафтан"], ["code-label", "Код подключения от руководителя", "Рамзи пайваст аз роҳбар"],
            ["employee-label", "Ваше имя", "Номи шумо"], ["in", "Пришёл", "Омадам"], ["out", "Ушёл", "Рафтам"],
            ["pair", "Подключить", "Пайваст кардан"], ["refresh", "Обновить", "Навсозӣ"], ["lookup", "Проверить результат", "Санҷидани натиҷа"],
            ["retry", "Повторить с PIN", "Такрор бо PIN"], ["pending-label", "Результат ещё не известен. Сначала проверьте его.", "Натиҷа ҳанӯз маълум нест. Аввал онро санҷед."],
            ["online-help", "Для отметки нужна связь с сервером.", "Барои сабт пайваст ба сервер лозим аст."]
        ]) $(id).textContent = t(ru, tg);
    }
    function controls() {
        document.querySelectorAll("button,input,select").forEach(el => el.disabled = busy);
        const p = pending();
        $("recovery").hidden = !p;
        $("in").disabled = busy || !context?.open || (!!p && !retrying);
        $("out").disabled = busy || !context?.open || (!!p && !retrying);
    }
    async function call(method, args = {}) {
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(`/api/method/hrms.hr.retail_attendance.api.${method}`, {
                method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
                headers: {"Content-Type":"application/json", "X-Retail-Attendance":"1", "X-Frappe-CSRF-Token":document.querySelector('meta[name="csrf-token"]').content},
                body: JSON.stringify(args)
            });
            if (!response.ok) { const e = new Error("rejected"); e.rejected = response.status >= 400 && response.status < 500; throw e; }
            const data = await response.json();
            if (!data.message || data.exc) throw Error("unknown");
            return data.message;
        } finally { clearTimeout(timer); }
    }
    async function refresh() {
        if (busy) return;
        busy = true; controls();
        try {
            context = await call("kiosk_context");
            $("pairing").hidden = true; $("attendance").hidden = false;
            $("shop").textContent = context.shop + (context.business_date ? ` · ${context.business_date}` : "");
            $("employee").replaceChildren(new Option(t("Выберите имя", "Номро интихоб кунед"), ""), ...context.employees.map(e => new Option(e.employee_name, e.name)));
            status(context.open ? "" : t("Рабочий день закрыт. Обратитесь к руководителю.", "Рӯзи корӣ баста аст. Ба роҳбар муроҷиат кунед."));
        } catch (e) {
            context = null; $("attendance").hidden = true; $("pairing").hidden = false;
            status(t("Нет связи или устройство не подключено. Проверьте связь; код подключения выдаёт руководитель.", "Пайваст нест ё дастгоҳ пайваст нашудааст. Пайвастро санҷед; рамзро роҳбар медиҳад."), true);
        } finally { busy = false; controls(); }
    }
    async function lookup() {
        if (busy) return;
        const p = pending(); if (!p) return;
        busy = true; controls();
        try {
            if (context && p.device !== context.device) throw Error("scope");
            const result = await call("kiosk_result", {request_id:p.request_id});
            if (result.found) { localStorage.removeItem(key); retrying = false; status(t("Сохранено: ", "Сабт шуд: ") + result.result.time); }
            else status(t("Сохранённая отметка не найдена. Можно повторить исходное действие с PIN.", "Сабти захирашуда ёфт нашуд. Амали пешинаро бо PIN такрор кардан мумкин аст."));
        } catch { status(t("Не удалось проверить результат. Повторите проверку при восстановлении связи.", "Натиҷа санҷида нашуд. Баъди барқарор шудани пайваст такрор кунед."), true); }
        finally { busy = false; controls(); }
    }
    $("attendance-form").addEventListener("submit", async e => {
        e.preventDefault(); if (busy || !context?.open) return;
        const original = pending(), action = e.submitter?.value;
        if (original && (!retrying || original.employee !== $("employee").value || original.action !== action || original.day !== context.day || original.device !== context.device)) return;
        const payload = original || {request_id:crypto.randomUUID(), employee:$("employee").value, action, day:context.day, device:context.device};
        // Durable intent precedes transmission; PIN remains in memory only.
        localStorage.setItem(key, JSON.stringify(payload));
        const pin = $("pin").value; $("pin").value = ""; $("employee").value = "";
        busy = true; retrying = false; controls();
        status(t("Сохраняем…", "Захира мешавад…"));
        try {
            const {device, ...args} = payload;
            const result = await call("kiosk_action", {...args, pin});
            localStorage.removeItem(key);
            status(t("Сохранено: ", "Сабт шуд: ") + result.time);
        } catch (e) {
            if (e.rejected) {
                localStorage.removeItem(key);
                status(t("Отметка отклонена. Проверьте PIN; при повторной ошибке обратитесь к руководителю. После нескольких попыток подождите пять минут.", "Сабт рад шуд. PIN-ро санҷед; агар хато такрор шавад, ба роҳбар муроҷиат кунед. Пас аз чанд кӯшиш панҷ дақиқа интизор шавед."), true);
            } else status(t("Результат неизвестен. Проверьте его перед повтором.", "Натиҷа номаълум аст. Пеш аз такрор онро санҷед."), true);
        } finally { busy = false; controls(); }
    });
    $("pair-form").addEventListener("submit", async e => {
        e.preventDefault(); if (busy) return;
        busy = true; controls(); const code = $("code").value; $("code").value = "";
        try { await call("pair_device", {pairing_code:code}); busy = false; await refresh(); }
        catch { status(t("Подключение не выполнено. Запросите новый код у руководителя.", "Пайваст нашуд. Аз роҳбар рамзи нав пурсед."), true); }
        finally { busy = false; controls(); }
    });
    $("lookup").onclick = lookup;
    $("retry").onclick = async () => {
        await lookup(); const p = pending(); if (!p || !context || p.device !== context.device || p.day !== context.day) return;
        retrying = true; $("employee").value = p.employee; controls();
        status(t("Введите PIN и повторите исходную кнопку: ", "PIN-ро ворид карда тугмаи пешинаро зер кунед: ") + (p.action === "IN" ? $("in").textContent : $("out").textContent)); $("pin").focus();
    };
    $("refresh").onclick = refresh;
    $("language").onchange = () => { lang = $("language").value; labels(); refresh(); };
    labels(); refresh().then(() => { if (pending()) lookup(); });
})();
