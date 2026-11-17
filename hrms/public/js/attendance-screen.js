/* Manager-opened attendance page. PINs/passwords never enter browser storage. */
(() => {
	const $ = (id) => document.getElementById(id);
	let lang = document.documentElement.lang,
		ctx,
		busy = false,
		retrying = false,
		refreshing = false,
		clockBase,
		clockReceived;
	const t = (ru, tg) => (lang === "tg" ? tg : ru);
	const key = () => `attendance-screen-pending:${location.host}:${ctx?.scope || "unknown"}`;
	function pending() {
		try {
			return JSON.parse(localStorage.getItem(key()) || "null");
		} catch {
			return null;
		}
	}
	const status = (message, error = false) => {
		$("status").textContent = message;
		$("status").dataset.error = String(error);
	};
	const time = (value) => (value ? value.slice(11, 16) : "—");
	function labels() {
		$("language").value = lang;
		document.documentElement.lang = lang;
		for (const [id, ru, tg] of [
			["title", "Приход и уход", "Омадан ва рафтан"],
			["employee-label", "Ваше имя", "Номи шумо"],
			["in", "Пришёл", "Омадам"],
			["out", "Ушёл", "Рафтам"],
			["refresh", "Обновить", "Навсозӣ"],
			["lookup", "Проверить результат", "Санҷидани натиҷа"],
			["retry", "Повторить с PIN", "Такрор бо PIN"],
			[
				"pending-label",
				"Результат ещё не известен. Сначала проверьте его.",
				"Натиҷа ҳанӯз маълум нест. Аввал онро санҷед.",
			],
			[
				"online-help",
				"Для отметки нужна связь с сервером. Закрытие этого окна не закрывает рабочий день.",
				"Барои сабт пайваст ба сервер лозим аст. Бастани ин равзана рӯзи кориро намебандад.",
			],
			["board-title", "Сегодня", "Имрӯз"],
			["management", "Вернуться к управлению", "Бозгашт ба идоракунӣ"],
			["unlock-title", "Вернуться к управлению", "Бозгашт ба идоракунӣ"],
			["password-label", "Пароль руководителя", "Рамзи роҳбар"],
			["cancel-unlock", "Отмена", "Бекор"],
			["confirm-unlock", "Продолжить", "Идома"],
			["entry-link", "Открыть посещаемость", "Кушодани ҳозиршавӣ"],
		])
			$(id).textContent = t(ru, tg);
		$("board-head").replaceChildren();
		const tr = document.createElement("tr");
		for (const label of [
			t("Сотрудник", "Корманд"),
			t("График", "Ҷадвал"),
			t("Приход", "Омадан"),
			t("Уход", "Рафтан"),
			t("Сейчас", "Ҳозир"),
			t("Штраф", "Ҷарима"),
		]) {
			const th = document.createElement("th");
			th.textContent = label;
			tr.append(th);
		}
		$("board-head").append(tr);
	}
	const state = (value) =>
		({
			"At work": t("На работе", "Дар кор"),
			Overtime: t("Работает сверх графика", "Берун аз ҷадвал кор мекунад"),
			"Checked out": t("Ушёл", "Рафт"),
			Expected: t("Ожидается", "Интизор"),
			Late: t("Ещё не пришёл", "Ҳанӯз наомад"),
			"Not arrived": t("Не пришёл", "Наомад"),
			"Not recorded": t("Нет отметки", "Сабт нест"),
			"Day off": t("Выходной", "Истироҳат"),
			"Approved leave": t("Отсутствие согласовано", "Набудан тасдиқ шудааст"),
			Holiday: t("Выходной", "Истироҳат"),
			Unconfigured: t("График не задан", "Ҷадвал таъин нашудааст"),
			Review: t("График требует проверки", "Ҷадвалро санҷед"),
		}[value] || "—");
	function fine(value) {
		if (!value) return "—";
		if (["Cancelled", "Suppressed", "Compensated"].includes(value.status))
			return t("Отменён", "Бекор шуд");
		return (
			new Intl.NumberFormat(lang === "tg" ? "tg-TJ" : "ru-RU", {
				maximumFractionDigits: 2,
			}).format(Number(value.amount)) +
			" " +
			ctx.currency
		);
	}
	function draw() {
		const selected = $("employee").value;
		$("employee").replaceChildren(
			new Option(t("Выберите имя", "Номро интихоб кунед"), ""),
			...ctx.employees.map((e) => new Option(e.employee_name, e.name))
		);
		$("employee").value = ctx.employees.some((e) => e.name === selected) ? selected : "";
		$("shop").textContent = ctx.shop + (ctx.business_date ? ` · ${ctx.business_date}` : "");
		$("board-rows").replaceChildren();
		for (const e of ctx.employees) {
			const tr = document.createElement("tr"),
				actual = e.actual[0];
			const values = [
				e.employee_name,
				e.starts_at ? `${time(e.starts_at)}–${time(e.ends_at)}` : state(e.state),
				time(actual?.in_time),
				time(actual?.out_time),
				state(e.status),
				fine(e.fine),
			];
			values.forEach((v, i) => {
				const td = document.createElement("td");
				td.textContent = v;
				if (i === 5 && e.fine?.status === "Applied") td.className = "fine";
				if (i === 3 && actual?.automatic) {
					const s = document.createElement("small");
					s.textContent = t("При закрытии дня", "Ҳангоми бастани рӯз");
					td.append(s);
				}
				tr.append(td);
			});
			$("board-rows").append(tr);
		}
		if (!ctx.employees.length) {
			const tr = document.createElement("tr"),
				td = document.createElement("td");
			td.colSpan = 6;
			td.textContent = t(
				"Сотрудники ещё не добавлены в график.",
				"Кормандон ҳанӯз ба ҷадвал илова нашудаанд."
			);
			tr.append(td);
			$("board-rows").append(tr);
		}
		$("attendance").hidden = false;
		$("board").hidden = false;
		$("entry").hidden = true;
	}
	function controls() {
		document.querySelectorAll("button,input,select").forEach((el) => (el.disabled = busy));
		const p = pending();
		$("recovery").hidden = !p;
		$("in").disabled = $("out").disabled = busy || !ctx?.open || (!!p && !retrying);
	}
	async function call(method, args = {}) {
		const controller = new AbortController(),
			timer = setTimeout(() => controller.abort(), 20000);
		try {
			const response = await fetch(
				`/api/method/hrms.hr.retail_attendance.screen.${method}`,
				{
					method: "POST",
					credentials: "same-origin",
					cache: "no-store",
					signal: controller.signal,
					headers: {
						"Content-Type": "application/json",
						Accept: "application/json",
						"X-Retail-Attendance": "1",
						"X-Frappe-CSRF-Token": document.querySelector('meta[name="csrf-token"]')
							.content,
					},
					body: JSON.stringify(args),
				}
			);
			if (!response.ok) {
				const e = Error("rejected");
				e.status = response.status;
				e.rejected = [400, 401, 403, 404, 417, 422, 429].includes(response.status);
				throw e;
			}
			const data = await response.json();
			if (!data.message || data.exc) throw Error("unknown");
			return data.message;
		} finally {
			clearTimeout(timer);
		}
	}
	async function refresh(quiet = false) {
		if (busy || refreshing) return;
		refreshing = true;
		try {
			ctx = await call("context");
			clockBase = Date.parse(ctx.server_time.replace(" ", "T") + "Z");
			clockReceived = Date.now();
			draw();
			clock();
			if (!ctx.open)
				status(
					t(
						"Рабочий день закрыт. Руководитель должен открыть день в посещаемости.",
						"Рӯзи корӣ баста аст. Роҳбар бояд рӯзро дар ҳозиршавӣ кушояд."
					)
				);
			else if (!quiet) status("");
		} catch (e) {
			if (e.status === 403 || e.status === 401) {
				ctx = null;
				$("attendance").hidden = $("board").hidden = true;
				$("entry").hidden = false;
				status(
					t(
						"Откройте экран отметки из посещаемости под учётной записью руководителя.",
						"Экрани сабтро аз ҳозиршавӣ бо ҳисоби роҳбар кушоед."
					),
					true
				);
			} else {
				if (ctx) ctx.open = false;
				status(
					t(
						"Нет связи с сервером. Проверьте подключение и нажмите «Обновить».",
						"Пайваст ба сервер нест. Пайвастро санҷида «Навсозӣ»-ро зер кунед."
					),
					true
				);
			}
		} finally {
			refreshing = false;
			controls();
		}
	}
	async function lookup() {
		if (busy) return false;
		const p = pending();
		if (!p) return false;
		busy = true;
		controls();
		try {
			const saved = await call("result", { request_id: p.request_id });
			if (saved.found) {
				localStorage.removeItem(key());
				retrying = false;
				status(t("Сохранено: ", "Сабт шуд: ") + time(saved.result.time));
				return true;
			}
			status(
				t(
					"Отметка не найдена. Можно повторить исходное действие с PIN.",
					"Сабт ёфт нашуд. Амали пешинаро бо PIN такрор кардан мумкин аст."
				)
			);
			return true;
		} catch {
			status(
				t(
					"Результат не проверен. Восстановите связь и повторите проверку.",
					"Натиҷа санҷида нашуд. Пайвастро барқарор карда санҷед."
				),
				true
			);
			return false;
		} finally {
			busy = false;
			controls();
		}
	}
	$("attendance-form").onsubmit = async (e) => {
		e.preventDefault();
		if (busy || !ctx?.open) return;
		const old = pending(),
			action = e.submitter?.value;
		if (
			old &&
			(!retrying ||
				old.employee !== $("employee").value ||
				old.action !== action ||
				old.day !== ctx.day)
		)
			return;
		const payload = old || {
			request_id: crypto.randomUUID(),
			employee: $("employee").value,
			action,
			day: ctx.day,
		};
		try {
			localStorage.setItem(key(), JSON.stringify(payload));
		} catch {
			status(
				t(
					"Не удалось сохранить запрос. Обратитесь к руководителю.",
					"Дархост захира нашуд. Ба роҳбар муроҷиат кунед."
				),
				true
			);
			return;
		}
		const pin = $("pin").value;
		$("pin").value = "";
		$("employee").value = "";
		busy = true;
		retrying = false;
		controls();
		status(t("Сохраняем…", "Захира мешавад…"));
		try {
			const saved = await call("punch", { ...payload, pin });
			localStorage.removeItem(key());
			status(
				(action === "IN"
					? t("Приход сохранён — ", "Омадан сабт шуд — ")
					: t("Уход сохранён — ", "Рафтан сабт шуд — ")) + time(saved.time)
			);
		} catch (e) {
			if (e.rejected) {
				localStorage.removeItem(key());
				status(
					t(
						"Отметка не сохранена. Проверьте PIN и состояние рабочего дня. После нескольких неверных попыток подождите пять минут.",
						"Сабт захира нашуд. PIN ва ҳолати рӯзи кориро санҷед. Пас аз чанд кӯшиши нодуруст панҷ дақиқа интизор шавед."
					),
					true
				);
			} else
				status(
					t(
						"Результат неизвестен. Проверьте его перед повтором.",
						"Натиҷа номаълум аст. Пеш аз такрор онро санҷед."
					),
					true
				);
		} finally {
			busy = false;
			controls();
			await refresh(true);
			$("employee").focus();
		}
	};
	$("lookup").onclick = async () => {
		await lookup();
		await refresh(true);
	};
	$("retry").onclick = async () => {
		if (!(await lookup())) return;
		const p = pending();
		if (!p || !ctx || p.day !== ctx.day) return;
		retrying = true;
		$("employee").value = p.employee;
		controls();
		status(
			t("Введите PIN и повторите кнопку: ", "PIN-ро ворид карда тугмаро такрор кунед: ") +
				(p.action === "IN" ? $("in").textContent : $("out").textContent)
		);
		$("pin").focus();
	};
	$("management").onclick = () => {
		$("unlock-error").textContent = "";
		$("manager-password").value = "";
		$("unlock-dialog").showModal();
	};
	$("cancel-unlock").onclick = () => {
		$("manager-password").value = "";
		$("unlock-dialog").close();
	};
	$("unlock-dialog").addEventListener("close", () => {
		$("manager-password").value = "";
	});
	$("unlock-form").onsubmit = async (e) => {
		e.preventDefault();
		if (busy) return;
		busy = true;
		controls();
		const password = $("manager-password").value;
		$("manager-password").value = "";
		try {
			const saved = await call("unlock", { password });
			try {
				localStorage.removeItem("retail-attendance-screen");
			} catch {}
			location.replace(saved.url);
		} catch {
			$("unlock-error").textContent = t(
				"Не удалось вернуться к управлению. Проверьте пароль и связь. После нескольких попыток подождите пять минут.",
				"Бозгашт ба идоракунӣ иҷро нашуд. Рамз ва пайвастро санҷед. Пас аз чанд кӯшиш панҷ дақиқа интизор шавед."
			);
		} finally {
			busy = false;
			controls();
		}
	};
	$("refresh").onclick = () => refresh();
	$("language").onchange = () => {
		lang = $("language").value;
		labels();
		if (ctx) draw();
	};
	function clock() {
		$("clock").textContent = clockBase
			? new Date(clockBase + Date.now() - clockReceived).toLocaleTimeString("ru-RU", {
					hour: "2-digit",
					minute: "2-digit",
					timeZone: "UTC",
			  })
			: "—";
	}
	labels();
	clock();
	setInterval(clock, 1000);
	setInterval(() => refresh(true), 15000);
	refresh().then(() => {
		if (pending()) lookup();
	});
	window.addEventListener("pageshow", (e) => {
		if (e.persisted) location.reload();
	});
})();
