// Интерфейс: вход, «Разбор», «Кандидаты», «Вакансии», разбор кандидата, пакет для заказчика, загрузка. Данные — из /api, без сборки.

const $ = (s) => document.querySelector(s);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ic = (n, c = "") => `<svg class="i ${c}"><use href="#i-${n}"/></svg>`;
const num = (x) => (x == null ? "–" : Math.round(x));
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const day = (iso) => new Date(iso).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
const stamp = (iso) =>
  new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

const VERDICT = {
  fit: ["подходит", "v-ok", "t-ok", "check", "c-full"],
  partial: ["частично", "v-mid", "t-mid", "half", "c-part"],
  no_fit: ["не подходит", "v-bad", "t-bad", "x", ""],
  no_data: ["нет данных", "v-bad", "t-bad", "help", ""],
};
const PS = {
  new: ["новый", "p-new"],
  review: ["на рассмотрении", "p-rev"],
  cust: ["у заказчика", "p-inv"],
  alt: ["предложена другая вакансия", "p-new"],
  invited: ["приглашён", "p-inv"],
  reserve: ["в резерве", "p-res"],
  rejected: ["отказ", "p-rej"],
  cust_rejected: ["отказ заказчика", "p-rej"],
};
const FILTERS = [
  ["all", "Все"], ["new", "Новые"], ["review", "На рассмотрении"], ["cust", "У заказчика"],
  ["alt", "Другая вакансия"], ["invited", "Приглашены"], ["reserve", "Резерв"], ["rejected", "Отказ"], ["cust_rejected", "Отказ заказчика"],
];
const ACT = [null, ["Заказчику", "send", "отправить на рассмотрение"], ["Другая вакансия", "arrow", "предложить кандидату"], ["В резерв", "archive", "вернуться при новой вакансии"], ["Отказ", "x", "с причинами по делу"]];
const DEC2ST = { 1: "cust", 2: "alt", 3: "reserve", 4: "rejected" };
const BK = { need: "Нужно ваше решение", fit: "Явно подходят", nofit: "Явно не подходят", done: "Разобрано" };
const ST = { full: ["полное", "check"], partial: ["частично", "half"], no: ["нет", "x"], unknown: ["не указано", "help"] };
const SECTION = {
  must: "Обязательные", nice: "Желательные", inferred: "Из обязанностей",
  profstandard: "Профстандарт", condition: "Условия · в балл не входят",
};
const LETTER = { invite: "Пригласить", offer_other: "Другая вакансия", reserve: "В резерв", reject: "Отказ" };
const ANSWERS = [["full", "Да"], ["partial", "Частично"], ["no", "Нет"]];
const FROM_CANDIDATE = "со слов кандидата";
const FLAG = {
  llm_error: "ошибка модели", quote_not_found: "цитата не найдена", no_without_negation: "«нет» без отрицания в тексте",
  check_relevance: "проверьте уместность", self_claim: "самооценка кандидата", from_letter: "из письма",
};
const POLL_MS = 1500;

const st = { me: null, bk: "need", ti: 0, packNames: false, pii: false, flt: "all", q: "", tab: "req", from: null, batch: sessionStorage.getItem("rvm-batch") };
let view = {}; // данные текущего экрана
let poll = null;

const mgr = () => st.me?.role === "manager";

async function api(path, opt = {}) {
  const r = await fetch(path, { ...opt, headers: { "X-Requested-With": "fetch", ...opt.headers } });
  if (r.status === 401 && path !== "/api/login") showLogin();
  if (!r.ok) {
    let msg = "";
    try {
      const d = (await r.json()).detail;
      msg = typeof d === "string" ? d : "";
    } catch {
      /* тело не JSON */
    }
    throw new Error(msg || `сервер ответил ${r.status}`);
  }
  return opt.raw ? r : r.status === 204 ? null : r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });

const stateBox = (kind, text) =>
  `<div class="card state ${kind === "err" ? "err" : ""}">${ic(kind === "err" ? "alert" : kind === "empty" ? "inbox" : "clock")}${esc(text)}</div>`;
const failed = (e) => stateBox("err", "Не удалось загрузить данные: " + e.message + ". Обновите страницу."); // stateBox экранирует
const names = {}; // ФИО из ответов API — только при включённом переключателе
// ФИО руководителю API отдаёт, только если пакет ушёл с «ФИО в документе»
const nameText = (id) => ((st.pii || mgr()) && names[id]) || `Кандидат ${id}`;
const cname = (id) => esc(nameText(id)); // для разметки; в textContent — nameText
const remember = (list) => list.forEach((c) => c.name && (names[c.id] = c.name));
const plural = (n, one, few, many) =>
  n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;
const piiTog = () =>
  `<label class="switch">ФИО и пол <button class="tog" role="switch" aria-checked="${st.pii}" data-pii aria-label="Показать ФИО и пол"></button></label>`;

/* ---------- роутинг ---------- */

function route() {
  const p = location.hash.replace(/^#\/?/, "").split("/");
  if (p[0] === "candidates" && p[2] === "vacancy") return { name: "cand", id: +p[1], vid: p[3] ? decodeURIComponent(p[3]) : null };
  if (p[0] === "candidates" && p[1]) return { name: "base", id: +p[1] };
  if (p[0] === "vacancies") return { name: "vacancies", vid: p[1] ? decodeURIComponent(p[1]) : null };
  if (p[0] === "triage") return { name: "triage" };
  return { name: "base", id: null };
}

let prevHash = "";
window.addEventListener("hashchange", () => {
  const r = route();
  // «‹ назад» в разборе ведёт туда, откуда пришли; переключение вакансий не в счёт
  if (r.name === "cand" && !prevHash.startsWith(`#/candidates/${r.id}/vacancy`)) st.from = prevHash || null;
  prevHash = location.hash;
  render(true);
});

const go = (hash) => {
  if (location.hash === hash) render();
  else location.hash = hash;
};

async function render(scroll = false) {
  if (!st.me) return;
  const r = route();
  if (mgr() && (r.name === "triage" || r.name === "vacancies")) return go("#/candidates");
  for (const a of document.querySelectorAll(".nav a")) {
    const on = a.dataset.sec === (r.name === "cand" || r.name === "base" ? "candidates" : r.name);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  $("#summary").hidden = $("#detail").hidden = r.name !== "cand";
  if (scroll) window.scrollTo({ top: 0 });
  if (r.name === "triage") {
    stopPoll();
    return renderTriage();
  }
  if (r.name === "vacancies") {
    stopPoll();
    return renderVacs(r);
  }
  if (r.name === "cand") return renderCand(r);
  return renderBase(r);
}

/* ---------- «Кандидаты» ---------- */

async function renderBase(r) {
  if (!view.base) $("#top").innerHTML = stateBox("load", "Загружаем кандидатов…");
  try {
    const q = `?pii=${st.pii}${st.q ? `&q=${encodeURIComponent(st.q)}` : ""}`;
    const [list, vacs, stats] = await Promise.all([
      api(`/api/candidates${q}`),
      api("/api/vacancies"),
      mgr() ? null : api("/api/stats"),
    ]);
    remember(list);
    view.base = { list, vacs, stats };
  } catch (e) {
    $("#top").innerHTML = failed(e);
    return;
  }
  $("#top").innerHTML = topBase();
  renderQueue();
  if (st.batch && !poll) startPoll();
  const sel = r.id ?? view.base.list[0]?.id;
  if (sel != null) renderCard(sel);
  else $("#doc").innerHTML = `<div class="stub" style="padding:18px">${ic("users")}Загрузите резюме — карточка кандидата появится здесь.</div>`;
  $("#navbadge").textContent = mgr() ? view.base.list.length || "" : view.base.stats.waiting || "";
}

function topBase() {
  const { list, vacs, stats } = view.base;
  if (mgr()) st.flt = "cust";
  const n = (f) => list.filter((c) => f === "all" || c.status === f).length;
  const week = list.filter((c) => Date.now() - new Date(c.created_at) < 7 * 864e5).length;
  const open = vacs.filter((v) => v.is_open).length;
  const sel = route().id ?? list[0]?.id;
  let h = `<div class="head"><span class="avatar blue">${ic("db")}</span><div class="who"><h1>Кандидаты</h1>
    <div class="sub"><span>${ic("users", "i-sm")}${mgr() ? `${list.length} у вас на рассмотрении` : `${list.length} в базе`}</span><span>${ic("brief", "i-sm")}${open} ${plural(open, "открытая вакансия", "открытые вакансии", "открытых вакансий")}</span></div></div>${mgr() ? "" : `<div class="side">${piiTog()}</div>`}</div>`;
  if (!mgr()) {
    const avg = stats.avg_reply_days;
    h += `<div class="stats">
    <div class="card stat warn"><div class="k">${ic("bell", "i-sm")}Ждут ответа</div><div class="v num">${stats.waiting}</div></div>
    <div class="card stat"><div class="k">${ic("clock", "i-sm")}Среднее время ответа</div><div class="v num">${avg == null ? "–" : `${avg} дн.`}</div></div>
    <div class="card stat"><div class="k">${ic("archive", "i-sm")}В резерве</div><div class="v num">${n("reserve")}</div></div>
    <div class="card stat"><div class="k">${ic("inbox", "i-sm")}Новых за неделю</div><div class="v num">${week}</div></div></div>
   <div id="queue"></div>`;
  }
  for (const v of mgr() ? [] : vacs.filter((x) => x.reserve_matches.length)) {
    const m = v.reserve_matches.length;
    h += `<div class="callout"><span class="ico">${ic("archive")}</span><div><b>Новая вакансия «${esc(v.title)}»: в резерве ${plural(m, "нашёлся", "нашлись", "нашлись")} ${m} ${plural(m, "подходящий", "подходящих", "подходящих")}.</b> ${v.reserve_matches.map(cname).join(", ")}.</div><a class="btn btn-white" href="#/candidates/${v.reserve_matches[0]}">Показать</a></div>`;
  }
  const rows = list.filter((c) => st.flt === "all" || c.status === st.flt);
  h += `<div class="card rank"><div class="tools"><label class="search">${ic("search", "i-sm")}<input id="q" type="search" value="${esc(st.q)}" placeholder="Навык, должность…" aria-label="Поиск"></label><div class="chips">
    ${FILTERS.filter(([k]) => !mgr() || k === "cust").map(([k, t]) => `<button class="chip" aria-pressed="${st.flt === k}" data-flt="${k}">${t} <span class="num">${n(k)}</span></button>`).join("")}</div></div>`;
  if (!rows.length)
    h += `<div class="stub" style="padding:18px">${ic("search")}${list.length || st.q ? "Никого не нашлось — измените поиск или фильтр." : "В базе пока пусто. Нажмите «Загрузить»."}</div>`;
  h += rows
    .map(
      (c) => `<button class="crow" aria-selected="${c.id === sel}" data-href="#/candidates/${c.id}">
     <span class="tile round">${ic("user")}</span>
     <span><div class="t" style="font-weight:600">${cname(c.id)}</div><div class="meta">${c.position ? `<span>${ic("brief", "i-sm")}${esc(c.position)}</span>` : ""}<span>${ic("cal", "i-sm")}${day(c.created_at)}</span>${c.versions > 1 ? `<span>${ic("hist", "i-sm")}версий: ${c.versions}</span>` : ""}</div></span>
     <span class="best"><b class="num">${num(c.best_vacancy_score ?? c.best_score)}</b><span>${esc(c.best_title ?? "лучший балл")}</span></span>
     <span><span class="pill ${PS[c.status][1]}">${PS[c.status][0]}</span>${!mgr() && c.waiting && c.waiting_days ? `<div class="wait">${ic("bell", "i-sm")}ждёт ${c.waiting_days} дн.</div>` : ""}</span>
     ${mgr() ? "<span></span>" : `<span class="reply btn btn-ghost" data-reply="${c.id}">${ic("send", "i-sm")}Ответить</span>`}</button>`,
    )
    .join("");
  return h + "</div>";
}

const EVENT = {
  uploaded: () => "Загружено резюме",
  possible_duplicate: (d) => `Похож на ${cname(d.of)} — возможный дубль`,
  merged: (d) => `Объединён с карточкой ${cname(d.from)}`,
  not_duplicate: () => "Отмечено: не дубль",
  evaluated: (d, t) => `Оценка по «${esc(t)}»: ${num(d.score)}`,
  status: (d) =>
    `${d.decision === "invite" ? "Решение: пригласить на интервью" : d.decision === "reject" ? "Решение: не подходит" : d.package ? "Отправлен заказчику в пакете" : typeof d.decision === "number" ? `Решение в разборе: ${ACT[d.decision][0].toLowerCase()}` : `Статус: ${esc(PS[d.to]?.[0] ?? d.to)}`}${d.comment ? ` · «${esc(d.comment)}»` : ""}`,
  reply_sent: () => "Отмечено: ответ отправлен",
  spot_check: (d) => `Выборочная проверка: ${d.agree ? "согласен с отказом" : "не согласен — на решение"}`,
  clarified: (d) => `Уточнено: ${esc(d.check)}`,
  answer: (d, t) => `Ответ по ${esc(d.requirement)} («${esc(t)}»): ${esc(ST[d.answer]?.[0] ?? d.answer)} · балл ${num(d.before)} → ${num(d.after)}`,
  deleted: () => "Удалён",
};

async function renderCard(id) {
  let c;
  try {
    c = await api(`/api/candidates/${id}?pii=${st.pii}`);
    remember([c]);
  } catch (e) {
    $("#doc").innerHTML = `<div class="state err">${ic("alert")}${esc(e.message)}</div>`;
    return;
  }
  view.card = c;
  const title = (vid) => c.evaluations.find((e) => e.vacancy_id === vid)?.title ?? vid ?? "";
  const ev = [...c.evaluations].sort((a, b) => b.score - a.score);
  const best = [...c.evaluations].sort((a, b) => (a.place ?? 99) - (b.place ?? 99))[0];
  const hist = c.events.filter((e) => EVENT[e.type]);
  const dup = c.possible_duplicate_of;
  const acts = mgr()
    ? `<textarea class="mnote" id="mgrnote" aria-label="Комментарий рекрутеру" placeholder="Комментарий рекрутеру"></textarea>${best ? `<a class="btn btn-ghost" href="#/candidates/${c.id}/vacancy/${encodeURIComponent(best.vacancy_id)}">${ic("list", "i-sm")}Открыть разбор</a>` : ""}<button class="btn btn-primary" data-mdec="invite">${ic("check", "i-sm")}Пригласить на интервью</button><button class="btn btn-danger" data-mdec="reject">${ic("x", "i-sm")}Не подходит</button>`
    : `${best ? `<a class="btn btn-ghost" href="#/candidates/${c.id}/vacancy/${encodeURIComponent(best.vacancy_id)}">${ic("list", "i-sm")}Открыть разбор</a>` : ""}<button class="btn btn-primary" data-reply="${c.id}">${ic("send", "i-sm")}Ответить</button>
    ${c.status !== "reserve" ? `<button class="btn btn-ghost" data-confirm="reserve">${ic("archive", "i-sm")}В резерв</button>` : ""}<button class="btn btn-danger" data-confirm="delete">${ic("trash", "i-sm")}Удалить</button>`;
  $("#doc").innerHTML = `<div class="ccard"><div class="ch1"><span class="avatar">${ic("user")}</span><div><h2>${cname(c.id)}</h2><div class="sub" style="margin-top:2px">${c.position ? `<span>${ic("brief", "i-sm")}${esc(c.position)}</span>` : ""}<span>${c.versions.length} ${plural(c.versions.length, "версия", "версии", "версий")} резюме · с ${day(c.created_at)}</span></div></div></div>
   ${dup && !mgr() ? `<div class="callout"><span class="ico">${ic("users")}</span><div><b>Возможный повтор:</b> похож на ${cname(dup)}.</div><button class="btn btn-white" data-merge="${dup}">Объединить</button><button class="btn btn-white" data-notdup>Разные люди</button></div>` : ""}
   <div class="sec">Статус</div><span class="pill ${PS[c.status][1]}">${PS[c.status][0]}</span>${!mgr() && c.replied_at ? ` <span class="done-chip">${ic("check", "i-sm")}Отмечено: отправлен ${day(c.replied_at)}</span>` : !mgr() && c.waiting_days ? ` <span class="wait">${ic("bell", "i-sm")}ждёт ответа ${c.waiting_days} дн.</span>` : ""}
   <div class="sec">Оценки по вакансиям</div>${ev.length ? ev.map((e) => { const v = VERDICT[e.verdict]; return `<a class="ev" href="#/candidates/${c.id}/vacancy/${encodeURIComponent(e.vacancy_id)}">${ic(v[3], v[4])}<span>${esc(e.title ?? e.vacancy_id)}</span><span class="n num">${num(e.score)}</span></a>`; }).join("") : `<div class="stub">${ic("clock")}Оценок пока нет.</div>`}
   <div class="sec">История</div><div class="tl">${hist.map((e) => `<div class="tli ${e.who !== "система" ? "acc" : ""}"><time class="num">${stamp(e.at)}</time>${EVENT[e.type](e.details, title(e.vacancy_id))}${e.who !== "система" ? ` <span class="who">· ${esc(e.who)}</span>` : ""}</div>`).join("")}</div>
   <div class="cactions">${acts}</div></div>`;
}

/* ---------- прогресс пачки ---------- */

function stopPoll() {
  clearTimeout(poll);
  poll = null;
}

async function startPoll() {
  stopPoll();
  if (!st.batch || route().name !== "base") return;
  try {
    view.batch = await api(`/api/uploads/${st.batch}`);
  } catch {
    st.batch = null; // пачка пропала после перезапуска сервера — прогресс не показываем
    sessionStorage.removeItem("rvm-batch");
    view.batch = null;
  }
  const running = view.batch?.files.some((f) => f.state === "queued" || f.state === "reading");
  if (running) poll = setTimeout(startPoll, POLL_MS);
  else if (view.batch && !view.batchDone) {
    view.batchDone = true; // пачка дочиталась — обновляем список один раз
    return renderBase(route());
  }
  renderQueue();
}

const fileState = (f) =>
  f.state === "error" && f.message.startsWith(f.name) ? f.message : f.name + " · " + (f.state === "error" ? f.message : "распознаём");

function renderQueue() {
  const el = $("#queue");
  const b = view.batch;
  if (!el) return;
  if (!st.batch || !b) {
    el.innerHTML = "";
    return;
  }
  const n = (s) => b.files.filter((f) => f.state === s).length;
  const all = b.files.length || 1;
  const [ok, run, q, err] = [n("done"), n("reading"), n("queued"), n("error")];
  const pct = (x) => `${(x / all) * 100}%`;
  el.innerHTML = `<div class="card queue"><div class="qh">${ic("upload", "c-acc")}<span><b>Загрузка</b> · ${b.files.length} ${plural(b.files.length, "файл", "файла", "файлов")} · ${ok} оценено${run ? `, ${run} в работе` : ""}${q ? `, ${q} в очереди` : ""}${err ? `, ${err} с ошибкой` : ""}</span>${run || q ? "" : `<button class="btn btn-ghost" data-qclose style="margin-left:auto">Скрыть</button>`}</div>
    <div class="bar2"><i style="width:${pct(ok)};background:var(--full)"></i><i style="width:${pct(run)};background:var(--accent)"></i><i style="width:${pct(err)};background:var(--no)"></i></div>
    <div class="qfiles">${ok ? `<span class="qf ok">${ic("check", "i-sm")}${ok} готово</span>` : ""}${b.files
      .filter((f) => f.state === "reading" || f.state === "error")
      .map((f) => `<span class="qf ${f.state === "error" ? "err" : "run"}">${ic(f.state === "error" ? "alert" : "clock", "i-sm")}${esc(fileState(f))}</span>`)
      .join("")}</div></div>`;
}

/* ---------- разбор кандидата ---------- */

async function renderCand(r) {
  stopPoll();
  if (view.cand?.id !== r.id) {
    $("#top").innerHTML = stateBox("load", "Загружаем разбор…");
    $("#summary").hidden = $("#detail").hidden = true;
    $("#doc").innerHTML = "";
    try {
      const [card, report] = await Promise.all([api(`/api/candidates/${r.id}?pii=${st.pii}`), api(`/api/evaluations?candidate=${r.id}`)]);
      remember([card]);
      view.cand = { id: r.id, card, report };
    } catch (e) {
      $("#top").innerHTML = failed(e);
      return;
    }
  }
  const { report } = view.cand;
  if (!report.vacancies.length) {
    $("#top").innerHTML = topCand(null) + stateBox("empty", report.error || "Оценок нет: нет открытых вакансий или резюме не прочитано.");
    $("#summary").hidden = $("#detail").hidden = true;
    renderDoc();
    return;
  }
  const vac = report.vacancies.find((v) => v.id === r.vid) ?? report.vacancies[0];
  view.vac = vac;
  $("#summary").hidden = $("#detail").hidden = false;
  $("#top").innerHTML = topCand(vac);
  renderSummary();
  renderPane();
  renderDoc();
}

function scoreCell(s) {
  return `<span><span class="score"><span class="n num">${num(s.score)}</span><span class="range"><i style="left:${s.low}%;right:${100 - s.high}%"></i><b style="left:calc(${s.score}% - 1px)"></b></span></span><span class="lbl num">вилка ${num(s.low)}–${num(s.high)}</span></span>`;
}

function topCand(vac) {
  const { id, card, report } = view.cand;
  const title = (vid) => report.vacancies.find((v) => v.id === vid)?.title ?? vid;
  const back = st.from && !st.from.startsWith(`#/candidates/${id}/vacancy`) ? st.from : `#/candidates/${id}`;
  const backLabel = back.startsWith("#/triage") ? "Разбор откликов" : back.startsWith("#/vacancies") ? "Вакансии" : "Кандидаты";
  const file = card.versions[0]?.name;
  let h = `<a class="back" href="${esc(back)}">${ic("chev", "i-sm")}${backLabel}</a><div class="head"><span class="avatar">${ic("user")}</span><div class="who"><h1>${cname(id)}</h1>
    <div class="sub">${report.applied ? `<span>${ic("inbox", "i-sm")}отклик на «${esc(title(report.applied))}»${report.applied_guessed ? " · определён по письму" : ""}</span>` : ""}${st.pii && file ? `<span>${ic("file", "i-sm")}${esc(file)}</span>` : ""}<span>${ic("cal", "i-sm")}${day(card.created_at)}</span></div></div>
    <div class="side"><span style="display:flex;gap:8px"><a class="btn btn-ghost" href="#/candidates/${id}">${ic("hist", "i-sm")}Карточка и история</a>${mgr() ? "" : `<button class="btn btn-ghost" data-reply="${id}">${ic("send", "i-sm")}Ответить</button>${vac ? `<button class="btn btn-ghost" data-pack="${esc(vac.id)}" data-ids="${id}">${ic("send", "i-sm")}Для заказчика</button>` : ""}`}</span>${mgr() ? "" : piiTog()}</div></div>`;
  if (!vac) return h;
  if (report.model.startsWith("демо-модель")) h += `<div class="callout"><span class="ico">${ic("alert")}</span><div><b>${esc(report.model)}</b></div></div>`;
  const rd = report.redirect;
  if (rd) {
    const s = (vid) => num(report.vacancies.find((v) => v.id === vid)?.score.score);
    h += `<div class="callout"><span class="ico">${ic("arrow")}</span><div><b>Лучше подходит «${esc(title(rd.to_vacancy))}»: ${s(rd.to_vacancy)} против ${s(rd.from_vacancy)}</b> у вакансии отклика «${esc(title(rd.from_vacancy))}».</div><a class="btn btn-white" href="#/candidates/${id}/vacancy/${encodeURIComponent(rd.to_vacancy)}">Открыть</a></div>`;
  } else if (report.none_fit) {
    h += `<div class="callout"><span class="ico">${ic("alert")}</span><div><b>Ни одна открытая вакансия не подходит.</b> Кандидата можно отправить в резерв — он будет сверен с новыми вакансиями.</div></div>`;
  }
  h += `<div class="card rank" role="listbox" aria-label="Рейтинг вакансий">${report.vacancies
    .map((v, i) => {
      const vv = VERDICT[v.verdict];
      const b = v.score.blockers.length;
      return `<button class="rrow" role="option" aria-selected="${v.id === vac.id}" data-href="#/candidates/${id}/vacancy/${encodeURIComponent(v.id)}"><span class="tile ${vv[2]}">${ic("brief")}<em class="num">${v.place ?? i + 1}</em></span>
     <span><div class="t">${esc(v.title)}</div><div class="meta">${v.id === report.applied ? `<span>${ic("inbox", "i-sm")}отклик</span>` : ""}${v.score.exceeds_count ? `<span class="ex">${ic("up", "i-sm")}сверх требований: ${v.score.exceeds_count}</span>` : ""}${b ? `<span class="bl">${ic("lock", "i-sm")}${b} ${plural(b, "блокер", "блокера", "блокеров")}</span>` : ""}</div></span>${scoreCell(v.score)}<span class="verdict ${vv[1]}">${ic(vv[3], "i-sm")}${vv[0]}</span></button>`;
    })
    .join("")}</div>`;
  return h;
}

/* ---------- «Вакансии» ---------- */

const CELL = { fit: "s-full", partial: "s-partial" };
const isOk = (c) => c.verdict === "fit" || c.verdict === "partial";
let vacTimer = null;

async function renderVacs(r) {
  clearTimeout(vacTimer);
  if (!view.vl) $("#top").innerHTML = stateBox("load", "Загружаем вакансии…");
  try {
    const vacs = (await api("/api/vacancies")).filter((v) => v.is_open);
    const ranks = await Promise.all(vacs.map((v) => api(`/api/vacancies/${encodeURIComponent(v.id)}/candidates?pii=${st.pii}`)));
    ranks.forEach(remember);
    view.vl = { vacs, ranks };
  } catch (e) {
    $("#top").innerHTML = failed(e);
    $("#doc").innerHTML = "";
    return;
  }
  const { vacs } = view.vl;
  const i = Math.max(0, vacs.findIndex((v) => v.id === r.vid));
  $("#top").innerHTML = topVacs(i);
  $("#doc").innerHTML = vacCard(i);
  // сверка резерва идёт в фоне — перечитываем, пока не закончится
  if (vacs.some((v) => v.reserve_state === "running"))
    vacTimer = setTimeout(() => route().name === "vacancies" && renderVacs(route()), POLL_MS);
}

function topVacs(i) {
  const { vacs, ranks } = view.vl;
  const add = `<button class="btn btn-ghost" data-upload="vac">${ic("plus", "i-sm")}Добавить вакансию</button>`;
  const head = (title, sub) =>
    `<div class="head"><span class="avatar blue">${ic("brief")}</span><div class="who"><h1>${title}</h1>${sub}</div><div class="side"><span style="display:flex;gap:8px">${add}</span>${piiTog()}</div></div>`;
  if (!vacs.length) return head("Вакансии", "") + stateBox("empty", "Открытых вакансий нет. Добавьте вакансию файлом, текстом или с hh.ru.");
  const v = vacs[i];
  const rows = ranks[i];
  const ok = rows.filter(isOk).length;
  let h = head(
    esc(v.title),
    `<div class="sub"><span>${ic("brief", "i-sm")}${vacs.length} ${plural(vacs.length, "открытая вакансия", "открытые вакансии", "открытых вакансий")}</span><span>${ic("users", "i-sm")}${rows.length} резюме · ${ok} ${plural(ok, "подходит", "подходят", "подходят")} хотя бы частично</span></div>`,
  );
  const m = v.reserve_matches.length;
  if (v.reserve_state === "running")
    h += `<div class="callout"><span class="ico">${ic("archive")}</span><div><b>Сверяем с резервом…</b> Проверено ${v.reserve_checked}.</div></div>`;
  else if (v.reserve_state === "error")
    h += `<div class="callout"><span class="ico">${ic("alert")}</span><div><b>Сверка с резервом не удалась:</b> ${esc(v.reserve_error)}</div></div>`;
  else if (m)
    h += `<div class="callout"><span class="ico">${ic("archive")}</span><div><b>В резерве ${plural(m, "нашёлся", "нашлись", "нашлись")} ${m} ${plural(m, "подходящий", "подходящих", "подходящих")}.</b> ${v.reserve_matches.map(cname).join(", ")}.</div><a class="btn btn-white" href="#/candidates/${v.reserve_matches[0]}/vacancy/${encodeURIComponent(v.id)}">Показать</a></div>`;
  const cols = [...new Set(ranks.flat().map((c) => c.id))];
  const col = (id) => (st.pii && names[id] ? esc(names[id].split(" ")[0]) : `К${id}`);
  if (cols.length)
    h += `<div class="card heat"><div class="heat-h"><b>${ic("grid", "c-acc")}Все вакансии × кандидаты</b><span class="legend"><span><i style="background:var(--full-bg)"></i>подходит</span><span><i style="background:var(--part-bg)"></i>частично</span><span><i style="background:var(--unk-bg)"></i>нет</span></span></div>
   <div style="overflow-x:auto"><div class="hg" style="grid-template-columns:minmax(200px,1fr) repeat(${cols.length},56px)"><span></span>${cols.map((id) => `<span class="ch" title="${cname(id)}">${col(id)}</span>`).join("")}
   ${vacs
     .map(
       (x, k) =>
         `<button class="hname ${k === i ? "on" : ""}" data-href="#/vacancies/${encodeURIComponent(x.id)}">${ic("brief", "i-sm")}${esc(x.title)}</button>${cols
           .map((id) => {
             const c = ranks[k].find((y) => y.id === id);
             return c
               ? `<button class="hcell num ${CELL[c.verdict] ?? "s-unknown"}" data-href="#/candidates/${id}/vacancy/${encodeURIComponent(x.id)}" title="${cname(id)} · ${esc(x.title)}">${num(c.score)}</button>`
               : `<span class="hcell" title="нет оценки" style="color:var(--fg3)">–</span>`;
           })
           .join("")}`,
     )
     .join("")}</div></div></div>`;
  h += `<div class="card rank" aria-label="Рейтинг кандидатов">${
    rows.length
      ? rows
          .map((c, n) => {
            const vv = VERDICT[c.verdict];
            return `<button class="rrow" data-href="#/candidates/${c.id}/vacancy/${encodeURIComponent(v.id)}"><span class="tile round ${vv[2]}">${ic("user")}<em class="num">${n + 1}</em></span>
     <span><div class="t">${cname(c.id)}</div><div class="meta">${c.position ? `<span>${ic("brief", "i-sm")}${esc(c.position)}</span>` : ""}<span>${PS[c.status][0]}</span>${c.blockers.length ? `<span class="bl">${ic("lock", "i-sm")}${esc(whyShort(c.blockers))}</span>` : ""}</div></span>${scoreCell(c)}<span class="verdict ${vv[1]}">${ic(vv[3], "i-sm")}${vv[0]}</span></button>`;
          })
          .join("")
      : `<div class="stub" style="padding:18px">${ic("users")}Оценок по вакансии пока нет: они появятся после загрузки резюме и сверки резерва.</div>`
  }</div>`;
  return h;
}

function vacCard(i) {
  const v = view.vl.vacs[i];
  if (!v) return `<div class="stub" style="padding:18px">${ic("brief")}Добавьте первую вакансию — её карточка появится здесь.</div>`;
  const rows = view.vl.ranks[i];
  const n = (f) => rows.filter(f).length;
  const src = v.source.startsWith("hh:") ? "с hh.ru" : v.source === "файл" ? "из файла" : "из текста";
  return `<div class="ccard"><div class="ch1"><span class="avatar blue">${ic("brief")}</span><div><h2>${esc(v.title)}</h2><div class="sub" style="margin-top:2px"><span>${ic("cal", "i-sm")}добавлена ${day(v.created_at)} ${src}</span></div></div></div>
   <div class="sec">Кандидаты</div>
   <div class="ev">${ic("check", "c-full")}<span>подходят</span><span class="n num">${n((c) => c.verdict === "fit")}</span></div>
   <div class="ev">${ic("half", "c-part")}<span>частично</span><span class="n num">${n((c) => c.verdict === "partial")}</span></div>
   <div class="ev">${ic("x")}<span>не подходят</span><span class="n num">${n((c) => !isOk(c))}</span></div>
   <div class="cactions"><button class="btn btn-danger" data-vclose="${esc(v.id)}">${ic("x", "i-sm")}Закрыть вакансию</button></div></div>`;
}

function openVacClose(vid) {
  const v = view.vl.vacs.find((x) => x.id === vid);
  modal(
    "confirm",
    `<div class="alert" role="alertdialog" aria-label="Подтверждение">
   <div class="ai" style="background:var(--no-bg);color:var(--no)">${ic("x")}</div>
   <h3>Закрыть вакансию «${esc(v.title)}»?</h3>
   <p>Она пропадёт из рейтингов и сверки резерва. Оценки кандидатов останутся в их карточках.</p>
   <div class="row"><button class="btn btn-ghost" data-cancel>Отмена</button><button class="btn btn-red" data-vclosed="${esc(vid)}">Закрыть</button></div></div>`,
  );
}

async function closeVac(vid) {
  modal("confirm", null);
  try {
    await post(`/api/vacancies/${encodeURIComponent(vid)}/close`);
  } catch (e) {
    return toast(`Не получилось: ${e.message}`);
  }
  toast("Вакансия закрыта");
  go("#/vacancies");
}

// первая цитата, на которую ссылается пункт вывода: R… — строка требования
function lineOf(refs) {
  for (const ref of refs) {
    const row = view.vac.rows.find((x) => x.requirement_id === ref);
    if (row?.evidence.length) return row.evidence[0].line;
  }
  return null;
}

function renderSummary() {
  const v = view.vac;
  const ins = v.insights;
  const head = `<div class="sum-h">${ic("bulb", "c-acc")}Кратко <span class="of">· ${esc(v.title)}</span></div>`;
  if (!ins) {
    $("#summary").innerHTML = `${head}<div class="stub">${ic("help")}Выводов по этой вакансии нет.</div>`;
    return;
  }
  const pts = (a, icn, c) =>
    a.length
      ? a.map((p) => { const n = lineOf(p.refs); return `<button class="pt"${n ? ` data-l="${n}"` : ""}>${ic(icn, c)}<span>${esc(p.text)}${n ? `<span class="ref">L${n}</span>` : ""}</span></button>`; }).join("")
      : `<div class="stub">—</div>`;
  $("#summary").innerHTML = `${head}
   <div class="sum-cols"><div><div class="h3">${ic("spark", "c-full")}Сильные стороны</div>${pts(ins.strengths, "check", "c-full")}</div>
   <div><div class="h3">${ic("alert", "c-part")}Риски</div>${pts(ins.risks, "alert", "c-part")}</div></div>
   <div class="rec">${ic("arrow", "c-acc")}<span><b>Рекомендация.</b> ${esc(ins.recommendation.text)}</span></div>`;
}

const quote = (ev) => ev.map((e) => `<code>L${e.line}</code>«${esc(e.quote)}»`).join(" · ");

function renderPane() {
  const v = view.vac;
  const qs = v.questions?.items ?? [];
  const rowOf = (rid) => v.rows.find((x) => x.requirement_id === rid);
  const reqOf = (rid) => v.requirements.find((x) => x.id === rid);
  $("#qcnt").textContent = qs.length;
  if (mgr() && st.tab === "mail") st.tab = "req";
  for (const b of document.querySelectorAll("[data-tab]")) b.setAttribute("aria-selected", b.dataset.tab === st.tab);
  let h = "";
  if (st.tab === "req") {
    const s = v.score;
    h = `<div class="why">Балл ${num(s.score)} из 100, вилка ${num(s.low)}–${num(s.high)} — граница, если «не указано» окажется «нет» или «да». Обязательные закрыты на ${num(s.must_coverage)}%.${s.blockers.length ? ` Блокеры: ${s.blockers.map(esc).join(", ")}.` : ""}</div>`;
    let g = "";
    for (const req of v.requirements) {
      const row = rowOf(req.id) ?? { status: "unknown", evidence: [], flags: [], note: "" };
      if (req.section !== g) {
        g = req.section;
        h += `<div class="grp">${esc(SECTION[g] ?? g)}</div>`;
      }
      const flags = [
        ...(row.exceeds ? [`<span class="flag plus">${ic("up", "i-sm")}${esc(row.exceeds)}</span>`] : []),
        ...row.flags.map((f) => `<span class="flag ${f === FROM_CANDIDATE ? "" : "warn"}">${f === FROM_CANDIDATE ? "" : ic("alert", "i-sm")}${esc(FLAG[f] ?? f)}</span>`),
        ...(row.note ? [`<span class="flag">${esc(row.note)}</span>`] : []),
      ].join("");
      const q = row.evidence.length ? quote(row.evidence) : row.status === "unknown" ? `<span class="ask">${ic("chat", "i-sm")}не упоминается → вопрос кандидату</span>` : "";
      h += `<button class="req" data-req="${esc(req.id)}"><span class="st s-${row.status}">${ic(ST[row.status][1])}${ST[row.status][0]}</span><span><div class="txt">${esc(req.text)}${flags}</div><div class="q">${q}</div></span></button>`;
    }
  } else if (st.tab === "qs") {
    h = `<div class="why" style="display:flex;gap:6px;align-items:center">${ic("shield", "c-full")}Из строк «не указано», «частично» и несостыковок. Вопросы о семье, здоровье, возрасте отсекает фильтр. Отметьте ответ после разговора — оценка пересчитается.</div>`;
    h += qs.length
      ? `<ul class="list">${qs
          .map((q, i) => {
            const req = reqOf(q.requirement_id);
            const row = rowOf(q.requirement_id);
            const done = row?.flags.includes(FROM_CANDIDATE);
            return `<li>${ic("chat", "c-acc")}<span style="flex:1">${esc(q.text)}${req && !mgr() ? `<div class="qa"><div class="seg">${ANSWERS.map(([val, l]) => `<button aria-pressed="${done && row.status === val}" data-ans="${val}" data-q="${i}">${l}</button>`).join("")}</div><span class="eff">${done ? `→ «${esc(req.text)}»: <b>${ST[row.status][0]}</b> · ${FROM_CANDIDATE}` : `влияет на «${esc(req.text)}»`}</span><input placeholder="заметка рекрутера" aria-label="Заметка" data-note="${i}" value="${esc(done ? row.note : "")}"></div>` : ""}</span></li>`;
          })
          .join("")}</ul>`
      : `<div class="stub">${ic("check")}Вопросов нет: по резюме всё ясно.</div>`;
  } else {
    const ls = v.letters;
    const cur = ls.find((l) => l.kind === st.letter) ?? ls.find((l) => l.suggested) ?? ls[0];
    h = cur
      ? `<div class="why">Черновик — отправляет рекрутер.</div>${ls.length > 1 ? `<div class="seg" style="width:max-content;margin-top:10px">${ls.map((l) => `<button aria-pressed="${l === cur}" data-letter="${esc(l.kind)}">${esc(LETTER[l.kind] ?? l.kind)}</button>`).join("")}</div>` : ""}<div class="letter" id="lettertext">${esc(cur.text)}</div><div class="actions"><button class="btn btn-ghost" data-copy="#lettertext">${ic("copy", "i-sm")}Скопировать</button></div>`
      : `<div class="why">Для этой пары письма нет.</div>`;
  }
  const p = $("#pane");
  p.innerHTML = h;
  p.classList.remove("enter");
  void p.offsetWidth;
  p.classList.add("enter");
}

const MASK_RE = /\[([^\[\]]{2,24})\]/g;

const renderDoc = () => ($("#doc").innerHTML = docHtml(view.cand.id, view.cand.report.resume));

function docHtml(id, r) {
  const lines = st.pii ? r.original : r.lines;
  let h = `<h2>${ic("file")}${cname(id)} · ${st.pii ? "исходный текст" : "текст, который видела модель"}</h2>`;
  let blk = null;
  for (const ln of lines) {
    let tx = esc(ln.text);
    if (!st.pii) tx = tx.replace(MASK_RE, (_, k) => `<span class="mask">${ic("eyeoff", "i-sm")}<b>${k} скрыто</b></span>`);
    if (ln.block && ln.block !== blk) h += `<div class="blk">${esc(ln.block)}</div>`;
    blk = ln.block;
    h += `<div class="ln" id="L${ln.n}"><span>${ln.n}</span><span>${tx}</span></div>`;
  }
  return h;
}

function highlight(ev) {
  for (const e of document.querySelectorAll(".ln.hl")) {
    e.classList.remove("hl");
    const s = e.lastChild;
    if (s.dataset.orig) s.innerHTML = s.dataset.orig;
  }
  ev.forEach(({ line, quote: q }, j) => {
    const el = $(`#L${line}`);
    if (!el) return;
    const s = el.lastChild;
    s.dataset.orig = s.dataset.orig || s.innerHTML;
    if (q && !st.pii) s.innerHTML = s.dataset.orig.replace(esc(q), () => `<mark>${esc(q)}</mark>`);
    el.classList.add("hl");
    if (j === 0) el.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
  });
}

async function answer(i, val) {
  const { card, id } = view.cand;
  const v = view.vac;
  const q = v.questions.items[i];
  const note = document.querySelector(`[data-note="${i}"]`)?.value ?? "";
  const rid = card.versions[0].id;
  try {
    const s = await api(`/api/evaluations/${rid}/${encodeURIComponent(v.id)}/rows/${encodeURIComponent(q.requirement_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: val, note }),
    });
    view.cand.report = await api(`/api/evaluations?candidate=${id}`); // места вакансий могли поменяться
    renderCand(route());
    toast(`Пересчитано: ${v.title} — ${num(s.score)} (вилка ${num(s.low)}–${num(s.high)})`);
  } catch (e) {
    toast(`Ответ не сохранён: ${e.message}`);
  }
}

/* ---------- «Разбор» ---------- */

const UNDECIDED = ["new", "review"];
const whyShort = (a) => (a.length > 1 ? `${a[0]} и ещё ${a.length - 1}` : a[0] ?? "");

async function renderTriage() {
  if (!view.tri) $("#top").innerHTML = stateBox("load", "Собираем отклики…");
  try {
    const [t, list] = await Promise.all([api("/api/triage"), api(`/api/candidates?sort=date&pii=${st.pii}`)]);
    remember(list);
    view.tri = { t, list, prev: Object.fromEntries(list.map((c) => [c.id, c.status])) };
  } catch (e) {
    $("#top").innerHTML = failed(e);
    return;
  }
  drawTriage();
}

function drawTriage() {
  const { t, list } = view.tri;
  const done = list.filter((c) => !UNDECIDED.includes(c.status));
  const left = t.need.length + t.fit.length + t.nofit.length;
  const all = left + done.length + t.pending;
  const cnt = { need: t.need.length, fit: t.fit.length, nofit: t.nofit.length, done: done.length };
  $("#trbadge").textContent = left || "";
  let h = `<div class="head"><span class="avatar blue">${ic("inbox")}</span><div class="who"><h1>Разбор откликов</h1>
    <div class="sub"><span>${ic("users", "i-sm")}${all} ${plural(all, "отклик", "отклика", "откликов")}</span><span>${ic("check", "i-sm")}разобрано ${done.length} из ${all}</span>${t.pending ? `<span>${ic("clock", "i-sm")}ещё оцениваются: ${t.pending}</span>` : ""}</div></div>
    <div class="side">${piiTog()}</div></div>
   <div><div class="buckets">${Object.keys(BK).map((b) => `<button class="bk" aria-pressed="${st.bk === b}" data-bk="${b}">${BK[b]} <span class="num">${cnt[b]}</span></button>`).join("")}</div>
   <div class="prog"><i style="width:${all ? (done.length / all) * 100 : 0}%"></i></div></div>`;
  view.tc = null;
  if (st.bk === "need") h += needCard(t.need);
  else if (st.bk === "done")
    h += `<div class="card rank">${
      done
        .map(
          (c) =>
            `<div class="trow" role="button" tabindex="0" data-href="#/candidates/${c.id}" style="cursor:pointer" title="Открыть карточку в «Кандидатах»"><span></span><span class="tile round">${ic("user")}</span><span><b>${cname(c.id)}</b><div class="rs">${esc(c.best_title ?? "")}</div></span><span class="num"><b>${num(c.best_vacancy_score ?? c.best_score)}</b></span><span><span class="pill ${PS[c.status][1]}">${PS[c.status][0]}</span></span></div>`,
        )
        .join("") || `<div class="stub" style="padding:16px">${ic("help")}Пока ничего не разобрано. Начните с «Нужно ваше решение».</div>`
    }</div>`;
  else h += bucketList(t[st.bk], st.bk === "fit");
  $("#top").innerHTML = h;
  triageDoc(view.tc?.id);
}

function needCard(rows) {
  if (!rows.length)
    return `<div class="card tcard"><div class="stub">${ic("check", "c-full")}Все отклики, где нужно ваше решение, разобраны.<button class="btn btn-ghost" data-bk="fit" style="margin-left:auto">Явно подходят</button><button class="btn btn-ghost" data-bk="nofit">Явно не подходят</button></div></div>`;
  st.ti = Math.max(0, Math.min(st.ti, rows.length - 1));
  const r = (view.tc = rows[st.ti]);
  const vv = VERDICT[r.verdict];
  const pts = (a, icn, c) =>
    a.length
      ? a.map((p) => `<button class="pt"${p.lines.length ? ` data-l="${p.lines[0]}"` : ""}>${ic(icn, c)}<span>${esc(p.text)}${p.lines.length ? `<span class="ref">L${p.lines[0]}</span>` : ""}</span></button>`).join("")
      : `<div class="stub">—</div>`;
  return `<div class="card tcard"><div class="tc-top"><span class="avatar" style="width:44px;height:44px">${ic("user")}</span><div><h2>${cname(r.id)}</h2><div class="sub" style="margin-top:2px">${r.position ? `<span>${ic("brief", "i-sm")}${esc(r.position)}</span>` : ""}</div></div>
    <div class="tc-nav"><button data-tn="-1" aria-label="Предыдущий">${ic("chev", "i-sm")}</button><span class="num">${st.ti + 1} из ${rows.length}</span><button data-tn="1" aria-label="Следующий" style="transform:scaleX(-1)">${ic("chev", "i-sm")}</button></div></div>
   <div class="tc-best"><span class="big num">${num(r.score)}</span><span><b>${esc(r.title)}</b><div class="lbl num">лучшая вакансия · вилка ${num(r.low)}–${num(r.high)}</div></span><span class="verdict ${vv[1]}">${ic(vv[3], "i-sm")}${vv[0]}</span></div>
   <div class="why-here">${ic("flag", "i-sm")}Почему здесь: ${esc(r.why.join("; "))}</div>
   <div class="pc"><div><div class="h3">${ic("spark", "c-full")}За</div>${pts(r.pros, "check", "c-full")}</div><div><div class="h3">${ic("alert", "c-part")}Против</div>${pts(r.cons, "alert", "c-part")}</div></div>
   <div class="decide">${[1, 2, 3, 4].map((i) => `<button class="dbtn ${r.proposed === i ? "sug" : ""}" data-act="${i}"><b>${ic(ACT[i][1], "i-sm")}${ACT[i][0]}<kbd>${i}</kbd></b><small>${r.proposed === i ? "предлагает система" : ACT[i][2]}</small></button>`).join("")}</div>
   <div class="actions" style="margin-top:12px"><a class="btn btn-ghost" href="#/candidates/${r.id}/vacancy/${encodeURIComponent(r.vacancy_id)}">${ic("list", "i-sm")}Открыть разбор <kbd style="font:600 11px ui-monospace;color:var(--fg3)">Enter</kbd></a><span class="why" style="padding:0;margin-left:auto">${ic("key", "i-sm")} 1–4 — решение, ← → — листать</span></div></div>`;
}

function bucketList(all, fit) {
  const spot = fit ? null : all.find((r) => r.spot_check);
  const rows = all.filter((r) => r !== spot); // проверяемый — отдельно сверху, не в пачке
  let h = spot
    ? `<div class="spot">${ic("help", "c-acc i-lg")}<div><b>Выборочная проверка.</b> Согласны, что ${cname(spot.id)} не подходит на «${esc(spot.title)}»? Причина: ${esc(whyShort(spot.why))}.<div class="lbl">Часть откликов из «явно не подходят» система показывает вам отдельно.</div></div><button class="btn btn-ghost" data-spot="${spot.id}" data-agree="">Не согласен</button><button class="btn btn-primary" data-spot="${spot.id}" data-agree="1">Согласен</button></div>`
    : "";
  h += `<div class="card rank">${
    rows
      .map(
        (r) =>
          `<label class="trow"><input type="checkbox" checked data-pickrow="${r.id}"><span class="tile round ${fit ? "t-ok" : "t-bad"}">${ic("user")}</span><span><b>${cname(r.id)}</b><div class="rs">${ic(fit ? "check" : "x", "i-sm")} ${esc(whyShort(r.why))}</div></span><span class="num"><b>${num(r.score)}</b> <span class="lbl" style="display:inline">${num(r.low)}–${num(r.high)}</span></span><span class="rs">${esc(r.title)}</span></label>`,
      )
      .join("") || `<div class="stub" style="padding:16px">${ic("check", "c-full")}Пусто.</div>`
  }`;
  if (rows.length)
    h += `<div class="tfoot"><span class="why" style="padding:0">${fit ? "Система предлагает отправить заказчику. Решение — ваше." : "Система предлагает отказ с причинами. Автоотказа нет — нужно ваше подтверждение."}</span>${
      fit
        ? `<button class="btn btn-primary" data-tpack>${ic("send", "i-sm")}Пакет для заказчика <span id="tcount">(${rows.length})</span></button>`
        : `<button class="btn btn-ghost" data-tbatch>${ic("x", "i-sm")}Подтвердить отказ <span id="tcount">(${rows.length})</span></button>`
    }</div>`;
  return h + "</div>";
}

const picked = () => [...document.querySelectorAll("[data-pickrow]:checked")].map((x) => +x.dataset.pickrow);

async function triageDoc(id) {
  if (id == null) {
    $("#doc").innerHTML = `<div class="stub" style="padding:18px">${ic("file")}Текст резюме появится здесь, когда откроете отклик в «Нужно ваше решение».</div>`;
    return;
  }
  const docs = (view.docs ??= {});
  try {
    docs[id] ??= (await api(`/api/evaluations?candidate=${id}`)).resume;
  } catch (e) {
    $("#doc").innerHTML = stateBox("err", e.message);
    return;
  }
  if (view.tc?.id === id) $("#doc").innerHTML = docHtml(id, docs[id]);
}

async function decide(ids, a, vid) {
  if (!ids.length) return toast("Отметьте хотя бы одного кандидата");
  const prev = ids.map((id) => [id, view.tri?.prev[id] ?? "new"]);
  try {
    await post("/api/triage/decide", { candidate_ids: ids, decision: a, vacancy_id: vid ?? null });
  } catch (e) {
    return toast(`Решение не сохранено: ${e.message}`);
  }
  const who = ids.length === 1 ? nameText(ids[0]) : `${ids.length} ${plural(ids.length, "кандидат", "кандидата", "кандидатов")}`;
  toast(
    `${who} → ${PS[DEC2ST[a]][0]}`,
    async () => {
      for (const [id, s] of prev) await setStatus(id, s);
      renderTriage();
    },
    ["Показать в «Кандидатах»", () => go(`#/candidates/${ids[0]}`)],
  );
  renderTriage();
}

async function spotAnswer(id, agree) {
  try {
    await post("/api/triage/spot", { candidate_id: id, agree });
  } catch (e) {
    return toast(`Ответ не сохранён: ${e.message}`);
  }
  toast(agree ? `${nameText(id)}: отказ подтверждён проверкой` : `${nameText(id)} перенесён в «Нужно ваше решение»`);
  renderTriage();
}

/* ---------- пакет для заказчика ---------- */

async function openPack(vid, ids) {
  const d = modal("pack", `<div class="sheet" role="dialog" aria-label="Пакет для заказчика" style="width:min(760px,calc(100vw - 32px))"><h3>${ic("send", "c-acc i-lg")}Пакет для заказчика</h3><div class="stub">${ic("clock")}Собираем таблицу…</div></div>`);
  let reps;
  try {
    reps = await Promise.all(ids.map((id) => api(`/api/evaluations?candidate=${id}`)));
  } catch (e) {
    d.querySelector(".stub").outerHTML = `<div class="state err">${ic("alert")}${esc(e.message)}</div><div class="actions"><button class="btn btn-ghost" data-pclose>Закрыть</button></div>`;
    return;
  }
  const items = ids.map((id, i) => ({ id, v: reps[i].vacancies.find((x) => x.id === vid) })).filter((x) => x.v);
  const sel = new Set(items.map((x) => x.id));
  const title = items[0]?.v.title ?? vid;
  const musts = items[0]?.v.requirements.filter((r) => r.section === "must") ?? [];
  const chosen = () => items.filter((x) => sel.has(x.id)).map((x) => x.id);
  const query = () => `vacancy_id=${encodeURIComponent(vid)}&${chosen().map((i) => `candidate_ids=${i}`).join("&")}&names=${st.packNames}`;
  const cell = (x, rid) => {
    const s = x.v.rows.find((r) => r.requirement_id === rid)?.status ?? "unknown";
    return `<td class="c"><span class="st s-${s}">${ic(ST[s][1])}${ST[s][0]}</span></td>`;
  };
  const draw = () => {
    const ch = items.filter((x) => sel.has(x.id));
    const dis = ch.length ? "" : "disabled";
    d.querySelector(".sheet").innerHTML = `<h3>${ic("send", "c-acc i-lg")}Пакет для заказчика · ${esc(title)}</h3>
     <div class="picks">${items.map((x) => `<label class="pick"><input type="checkbox" ${sel.has(x.id) ? "checked" : ""} data-ppick="${x.id}">${cname(x.id)} <b class="num">${num(x.v.score.score)}</b></label>`).join("")}<label class="switch" style="margin-left:auto">ФИО в документе <button class="tog" role="switch" aria-checked="${st.packNames}" data-pnames aria-label="ФИО в документе"></button></label></div>
     ${ch.length ? `<table class="cmp"><thead><tr><th>Обязательные требования</th>${ch.map((x) => `<th class="c">${cname(x.id)}</th>`).join("")}</tr></thead><tbody>${musts.map((r) => `<tr><td>${esc(r.text)}</td>${ch.map((x) => cell(x, r.id)).join("")}</tr>`).join("")}</tbody></table>` : `<div class="stub">${ic("help")}Отметьте хотя бы одного кандидата.</div>`}
     <div class="hint" style="display:flex;gap:6px;align-items:center">${ic("file", "i-sm")}В документе: эта таблица, по каждому кандидату — «Кратко», риски и вопросы на собеседование. После скачивания DOCX кандидаты получат статус «у заказчика».</div>
     <div class="actions"><button class="btn btn-ghost" data-pclose>Отмена</button><button class="btn btn-ghost" data-pmail ${dis}>${ic("copy", "i-sm")}Текст письма</button><button class="btn btn-ghost" data-ppdf ${dis}>${ic("download", "i-sm")}PDF</button><button class="btn btn-primary" data-pdocx ${dis}>${ic("download", "i-sm")}Скачать DOCX</button></div>`;
  };
  d.addEventListener("change", (e) => {
    const p = e.target.closest("[data-ppick]");
    if (!p) return;
    if (p.checked) sel.add(+p.dataset.ppick);
    else sel.delete(+p.dataset.ppick);
    draw();
  });
  d.addEventListener("click", async (e) => {
    const on = (s) => e.target.closest(s);
    if (on("[data-pnames]")) {
      st.packNames = !st.packNames;
      draw();
    } else if (on("[data-ppdf]")) window.open(`/api/package?${query()}&format=html`, "_blank");
    else if (on("[data-pmail]")) {
      const n = chosen().length;
      const text = `Добрый день!\n\nНаправляю ${n} ${plural(n, "кандидата", "кандидатов", "кандидатов")} на вакансию «${title}». Во вложении — сравнительная таблица по обязательным требованиям, по каждому кандидату краткий разбор, риски и вопросы на собеседование.\n\nПодскажите, пожалуйста, кого пригласить на интервью — решение можно отметить в системе.`;
      try {
        await navigator.clipboard.writeText(text);
        toast("Текст письма заказчику скопирован");
      } catch {
        toast("Не удалось скопировать текст письма");
      }
    } else if (on("[data-pdocx]")) {
      const ids2 = chosen();
      try {
        const r = await api("/api/package", { method: "POST", raw: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vacancy_id: vid, candidate_ids: ids2, names: st.packNames }) });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(await r.blob());
        a.download = `Пакет — ${title}.docx`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      } catch (err) {
        return toast(`Пакет не собран: ${err.message}`);
      }
      modal("pack", null);
      view.base = null;
      toast(`Пакет скачан: ${ids2.length} → у заказчика`, null, ["Показать в «Кандидатах»", () => go(`#/candidates/${ids2[0]}`)]);
      render();
    }
  });
  draw();
}

/* ---------- окна ---------- */

function modal(id, html) {
  $(`#${id}`)?.remove();
  if (!html) return null;
  const d = document.createElement("div");
  d.id = id;
  d.className = "scrim";
  d.innerHTML = html;
  document.body.appendChild(d);
  return d;
}

async function openReply(cid) {
  const d = modal("sheet", `<div class="sheet" role="dialog" aria-label="Быстрый ответ"><h3>${ic("send", "c-acc i-lg")}Ответ: ${cname(cid)}</h3><div class="stub">${ic("clock")}Собираем письмо…</div></div>`);
  let v;
  try {
    v = (await api(`/api/evaluations?candidate=${cid}`)).vacancies[0];
  } catch (e) {
    d.querySelector(".stub").outerHTML = `<div class="state err">${ic("alert")}${esc(e.message)}</div><div class="actions"><button class="btn btn-ghost" data-close>Закрыть</button></div>`;
    return;
  }
  const ls = v?.letters ?? [];
  let cur = ls.find((l) => l.suggested) ?? ls[0];
  const draw = () => {
    d.querySelector(".sheet").innerHTML = `<h3>${ic("send", "c-acc i-lg")}Ответ: ${cname(cid)}</h3>${
      cur
        ? `<div class="seg">${ls.map((l) => `<button aria-pressed="${l === cur}" data-rkind="${esc(l.kind)}">${esc(LETTER[l.kind] ?? l.kind)}</button>`).join("")}</div>
       <label class="fld"><span>Тема</span><input value="МФТИ: ваш отклик на «${esc(v.title)}»" aria-label="Тема"></label>
       <textarea class="ed" aria-label="Текст письма" id="replytext">${esc(cur.text)}</textarea>
       <div class="hint" style="display:flex;gap:6px;align-items:center">${ic("shield", "c-full i-sm")}Текст собран из таблицы требований, его можно править. Смена шаблона заменит текст.</div>
       <div class="actions"><button class="btn btn-ghost" data-close>Отмена</button><button class="btn btn-ghost" data-sent>${ic("check", "i-sm")}Отметить: отправлен</button><button class="btn btn-primary" data-copy="#replytext">${ic("copy", "i-sm")}Скопировать</button></div>`
        : `<div class="stub">${ic("help")}Для этого кандидата письма нет: нет оценок по вакансиям.</div><div class="actions"><button class="btn btn-ghost" data-close>Закрыть</button></div>`
    }`;
  };
  d.addEventListener("click", async (e) => {
    const k = e.target.closest("[data-rkind]");
    if (k) {
      cur = ls.find((l) => l.kind === k.dataset.rkind);
      draw();
    } else if (e.target.closest("[data-sent]")) {
      try {
        await post(`/api/candidates/${cid}/reply-sent`, { template: cur.kind, vacancy_id: v.id });
      } catch (err) {
        return toast(`Не отмечено: ${err.message}`);
      }
      modal("sheet", null);
      toast(`${nameText(cid)}: отмечено — ответ отправлен`);
      render();
    }
  });
  draw();
}

function openConfirm(kind) {
  const c = view.card;
  const res = kind === "reserve";
  const n = cname(c.id).replace(/^Кандидат /, "Кандидата ");
  modal(
    "confirm",
    `<div class="alert" role="alertdialog" aria-label="Подтверждение">
   <div class="ai" style="background:${res ? "#f1ecfb" : "var(--no-bg)"};color:${res ? "#6b3fc4" : "var(--no)"}">${ic(res ? "archive" : "trash")}</div>
   <h3>${res ? `Отправить ${n} в резерв?` : `Удалить ${n}?`}</h3>
   <p>${res ? "Карточка останется в базе. Когда появится подходящая вакансия, система сама предложит кандидата." : "Удалятся резюме, оценки, история и отпечатки контактов. Действие нельзя отменить."}</p>
   <div class="row"><button class="btn btn-ghost" data-cancel>Отмена</button><button class="btn ${res ? "btn-primary" : "btn-red"}" data-ok="${kind}">${res ? "В резерв" : "Удалить"}</button></div></div>`,
  );
}

const setStatus = (id, status) =>
  api(`/api/candidates/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });

async function confirmed(kind) {
  const c = view.card;
  modal("confirm", null);
  try {
    if (kind === "reserve") {
      const prev = c.status;
      await setStatus(c.id, "reserve");
      render();
      toast(`${nameText(c.id)} в резерве`, async () => {
        await setStatus(c.id, prev);
        render();
      }, ["Открыть резерв", () => { st.flt = "reserve"; go(`#/candidates/${c.id}`); }]);
    } else {
      const del = await api(`/api/candidates/${c.id}`, { method: "DELETE" });
      toast(`${nameText(c.id)} удалён со всеми данными${del?.warning ? ` (${del.warning})` : ""}`);
      go("#/candidates");
    }
  } catch (e) {
    toast(`Не получилось: ${e.message}`);
  }
}

const uploadTabs = (res) =>
  `<div class="seg"><button aria-pressed="${res}" data-upload="res">${ic("user", "i-sm")}Резюме</button><button aria-pressed="${!res}" data-upload="vac">${ic("brief", "i-sm")}Вакансии</button></div>`;

// файлы из выбора и перетаскивания; onPick(files)
function dropZone(d, onPick) {
  d.querySelector("#files").addEventListener("change", (e) => onPick([...e.target.files]));
  const drop = d.querySelector("#drop");
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    onPick([...e.dataTransfer.files]);
  });
}

async function openUpload(tab) {
  if (tab === "vac") return openVacUpload();
  const d = modal(
    "upload",
    `<div class="sheet" role="dialog" aria-label="Загрузка"><h3>${ic("upload", "c-acc i-lg")}Загрузить</h3>
   ${uploadTabs(true)}
   <label class="drop" id="drop"><input type="file" id="files" multiple hidden accept=".docx,.doc,.pdf,.rtf,.txt,.odt,.html,.htm,.jpg,.jpeg,.png,.heic,.webp,.tif,.tiff,.zip"><div class="di">${ic("upload")}</div><b>Перетащите резюме или выберите файлы</b><small>одно, пачка или zip · docx, pdf, rtf, txt, doc, сканы jpg и png</small><div class="files" id="picked"></div></label>
   <label class="fld" style="margin-top:8px"><span>Отклик</span><select class="sel" id="applied" aria-label="Отклик на вакансию"><option value="">не указано</option></select></label>
   <div class="hint" style="display:flex;gap:6px;align-items:center">${ic("eyeoff", "i-sm")}ФИО, контакты, пол и возраст скрываются до анализа. Повторное резюме того же человека попадёт в его карточку.</div>
   <div class="actions"><button class="btn btn-ghost" data-uclose>Отмена</button><button class="btn btn-primary" data-ugo disabled>${ic("upload", "i-sm")}Загрузить</button></div></div>`,
  );
  let files = [];
  dropZone(d, (list) => {
    files = list;
    d.querySelector("#picked").textContent = files.map((f) => f.name).join(", ");
    d.querySelector("[data-ugo]").disabled = !files.length;
  });
  d.querySelector("[data-ugo]").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    const ap = d.querySelector("#applied").value;
    try {
      const b = await api(`/api/uploads${ap ? `?applied=${encodeURIComponent(ap)}` : ""}`, { method: "POST", body: fd });
      st.batch = b.batch_id;
      sessionStorage.setItem("rvm-batch", b.batch_id);
      view.batch = null;
      view.batchDone = false;
      modal("upload", null);
      st.flt = "all";
      go("#/candidates");
      toast("Файлы в очереди — прогресс в «Кандидатах»");
    } catch (err) {
      e.target.disabled = false;
      toast(`Загрузка не принята: ${err.message}`);
    }
  });
  try {
    const vacs = (await api("/api/vacancies")).filter((v) => v.is_open);
    d.querySelector("#applied").insertAdjacentHTML("beforeend", vacs.map((v) => `<option value="${esc(v.id)}">${esc(v.title)}</option>`).join(""));
  } catch {
    /* без списка вакансий загрузка всё равно возможна */
  }
}

function openVacUpload() {
  const d = modal(
    "upload",
    `<div class="sheet" role="dialog" aria-label="Загрузка"><h3>${ic("upload", "c-acc i-lg")}Загрузить</h3>
   ${uploadTabs(false)}
   <label class="drop" id="drop"><input type="file" id="files" multiple hidden accept=".docx,.doc,.pdf,.rtf,.txt,.odt,.html,.htm"><div class="di">${ic("file")}</div><b>Перетащите файл вакансии или выберите</b><small>docx, pdf, rtf, txt</small><div class="files" id="picked"></div></label>
   <textarea class="ed" id="vtext" style="min-height:90px" aria-label="Текст вакансии" placeholder="или вставьте текст вакансии"></textarea>
   <div class="or">или</div>
   <button class="mipt" id="hhload"><span class="di">${ic("download")}</span><span><b>Актуальные вакансии МФТИ</b><small>открытые вакансии на hh.ru</small></span></button>
   <div id="hh"></div>
   <div class="hint" style="display:flex;gap:6px;align-items:center">${ic("archive", "i-sm")}Новая вакансия сразу сверяется с кадровым резервом.</div>
   <div class="actions"><button class="btn btn-ghost" data-uclose>Отмена</button><button class="btn btn-primary" data-vgo disabled>${ic("plus", "i-sm")}Добавить</button></div></div>`,
  );
  let files = [];
  const go_ = d.querySelector("[data-vgo]");
  const hhIds = () => [...d.querySelectorAll("#hh input:checked:not(:disabled)")].map((x) => x.value);
  const count = () => files.length + (d.querySelector("#vtext").value.trim() ? 1 : 0) + hhIds().length;
  const update = () => {
    const n = count();
    go_.disabled = !n;
    go_.lastChild.textContent = n > 1 ? `Добавить ${n} ${plural(n, "вакансию", "вакансии", "вакансий")}` : "Добавить";
  };
  dropZone(d, (list) => {
    files = list;
    d.querySelector("#picked").textContent = files.map((f) => f.name).join(", ");
    update();
  });
  d.querySelector("#vtext").addEventListener("input", update);
  d.querySelector("#hh").addEventListener("change", update);
  d.querySelector("#hhload").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    const box = d.querySelector("#hh");
    box.innerHTML = `<div class="stub">${ic("clock")}Загружаем список с hh.ru…</div>`;
    try {
      const list = await api("/api/vacancies/hh");
      box.innerHTML = list.length
        ? `<div class="vlist">${list.map((v) => `<label class="vli"><input type="checkbox" value="${esc(v.id)}" ${v.imported ? "checked disabled" : ""}>${esc(v.name)}<small>${v.imported ? "уже в базе" : esc(v.area)}</small></label>`).join("")}</div>`
        : `<div class="stub">${ic("inbox")}Открытых вакансий на hh.ru нет.</div>`;
    } catch (err) {
      const off = /не настроена/.test(err.message);
      box.innerHTML = `<div class="state err">${ic("alert")}${off ? "Загрузка с hh не настроена на сервере. Добавьте вакансию файлом или текстом." : esc(err.message)}</div>`;
    }
  });
  go_.addEventListener("click", async () => {
    go_.disabled = true;
    const ids = [];
    const errs = [];
    const text = d.querySelector("#vtext").value.trim();
    const forms = files.map((f) => ["file", f, f.name]);
    if (text) forms.push(["text", text]);
    for (const [k, val, name] of forms) {
      const fd = new FormData();
      if (name) fd.append(k, val, name);
      else fd.append(k, val);
      try {
        ids.push((await api("/api/vacancies", { method: "POST", body: fd })).id);
      } catch (err) {
        errs.push((name ?? "текст") + ": " + err.message); // текст для тоста
      }
    }
    if (hhIds().length) {
      try {
        const r = await post("/api/vacancies/hh/import", { ids: hhIds() });
        ids.push(...r.added);
        errs.push(...Object.values(r.errors));
      } catch (err) {
        errs.push("hh.ru: " + err.message);
      }
    }
    if (!ids.length) {
      go_.disabled = false;
      return toast(`Вакансии не добавлены: ${errs.join("; ")}`);
    }
    modal("upload", null);
    view.vl = null;
    go(`#/vacancies/${encodeURIComponent(ids.at(-1))}`);
    const n = ids.length;
    toast(`${n > 1 ? `Добавлено ${n} ${plural(n, "вакансия", "вакансии", "вакансий")}` : "Вакансия добавлена"} · сверяем с резервом${errs.length ? `. Не добавлено: ${errs.join("; ")}` : ""}`);
  });
}

async function managerDecide(kind) {
  const c = view.card;
  try {
    await post(`/api/candidates/${c.id}/manager-decision`, { decision: kind, comment: $("#mgrnote")?.value.trim() ?? "" });
  } catch (e) {
    return toast(`Решение не сохранено: ${e.message}`);
  }
  toast(`${nameText(c.id)}: ${kind === "invite" ? "пригласить на интервью" : "не подходит"} — рекрутер увидит решение`);
  view.base = null;
  go("#/candidates");
}

async function duplicate(merge) {
  const c = view.card;
  const keep = c.possible_duplicate_of;
  try {
    if (merge) await post(`/api/candidates/${keep}/merge`, { other: c.id });
    else await post(`/api/candidates/${c.id}/dismiss-duplicate`);
  } catch (e) {
    return toast(`Не получилось: ${e.message}`);
  }
  toast(merge ? `Карточки объединены в ${nameText(keep)}` : "Отмечено: разные люди");
  view.base = null;
  go(`#/candidates/${merge ? keep : c.id}`);
}

/* ---------- вход ---------- */

function showLogin() {
  st.me = null;
  view = {};
  stopPoll();
  for (const x of document.querySelectorAll(".scrim, #toast")) x.remove();
  $("#login").hidden = false;
  api("/api/login-options")
    .then((o) => ($("#demo").hidden = !o.demo))
    .catch(() => {});
}

function enter(user) {
  st.me = user;
  st.flt = mgr() ? "cust" : "all";
  st.pii = false;
  $("#login").hidden = true;
  $("#lgerr").textContent = "";
  $("#mename").textContent = user.name;
  for (const el of document.querySelectorAll("[data-rec]")) el.hidden = mgr();
  const r = route();
  if (mgr() && !location.hash.startsWith("#/candidates")) return go("#/candidates");
  if (!mgr() && !location.hash) return go("#/triage");
  if (r.name !== "triage" && !mgr()) api("/api/triage").then((t) => ($("#trbadge").textContent = t.need.length + t.fit.length + t.nofit.length || "")).catch(() => {});
  render();
}

async function login(body) {
  try {
    enter(await post("/api/login", body));
  } catch (e) {
    $("#lgerr").textContent = e.message;
  }
}

let toastTimer;
let toastAct = {};
function toast(text, undo, goTo) {
  $("#toast")?.remove();
  clearTimeout(toastTimer);
  const d = document.createElement("div");
  d.id = "toast";
  d.className = "toast";
  d.setAttribute("role", "status");
  d.innerHTML = `<span></span>${goTo ? `<button data-tgo>${goTo[0]}</button>` : ""}${undo ? "<button data-undo>Отменить</button>" : ""}`;
  const span = d.firstChild;
  span.textContent = text; // текст тоста — только текстом: в нём ФИО и ошибки сервера
  document.body.appendChild(d);
  toastAct = { undo, go: goTo?.[1] };
  toastTimer = setTimeout(() => d.remove(), 6000);
}

/* ---------- события ---------- */

document.addEventListener("click", async (e) => {
  const t = e.target;
  const on = (sel) => t.closest(sel);
  let el;
  if (on("[data-tgo]") || on("[data-undo]")) {
    const f = on("[data-undo]") ? toastAct.undo : toastAct.go;
    $("#toast")?.remove();
    f?.();
  } else if ((el = on("[data-copy]"))) {
    const src = $(el.dataset.copy);
    try {
      await navigator.clipboard.writeText(src.value ?? src.textContent);
      toast("Текст письма скопирован");
    } catch {
      toast("Не удалось скопировать — выделите текст вручную");
    }
  } else if ((el = on("[data-login]"))) login({ role: el.dataset.login });
  else if (on("#logout")) {
    await post("/api/logout").catch(() => {});
    showLogin();
  } else if ((el = on("[data-bk]"))) {
    st.bk = el.dataset.bk;
    st.ti = 0;
    drawTriage();
  } else if ((el = on("[data-tn]"))) {
    st.ti += +el.dataset.tn;
    drawTriage();
  } else if ((el = on("[data-act]"))) decide([view.tc.id], +el.dataset.act, view.tc.vacancy_id);
  else if ((el = on("[data-spot]"))) spotAnswer(+el.dataset.spot, !!el.dataset.agree);
  else if (on("[data-tbatch]")) decide(picked(), 4);
  else if (on("[data-tpack]")) {
    const rows = view.tri.t.fit.filter((r) => picked().includes(r.id));
    if (!rows.length) return toast("Отметьте хотя бы одного кандидата");
    const vid = rows[0].vacancy_id;
    const same = rows.filter((r) => r.vacancy_id === vid);
    if (same.length < rows.length) toast(`Пакет — по «${rows[0].title}». Остальных отправьте отдельным пакетом по их вакансии.`);
    openPack(vid, same.map((r) => r.id));
  } else if ((el = on("[data-pack]"))) openPack(el.dataset.pack, el.dataset.ids.split(",").map(Number));
  else if (on("[data-pclose]") || t.id === "pack") modal("pack", null);
  else if ((el = on("[data-mdec]"))) managerDecide(el.dataset.mdec);
  else if (on("[data-merge]")) duplicate(true);
  else if (on("[data-notdup]")) duplicate(false);
  else if (on("[data-close]") || t.id === "sheet") modal("sheet", null);
  else if (on("[data-uclose]") || t.id === "upload") modal("upload", null);
  else if (on("[data-cancel]") || t.id === "confirm") modal("confirm", null);
  else if ((el = on("[data-ok]"))) confirmed(el.dataset.ok);
  else if ((el = on("[data-reply]"))) {
    e.preventDefault();
    openReply(+el.dataset.reply);
  } else if ((el = on("[data-upload]"))) openUpload(el.dataset.upload || (route().name === "vacancies" ? "vac" : "res"));
  else if ((el = on("[data-vclose]"))) openVacClose(el.dataset.vclose);
  else if ((el = on("[data-vclosed]"))) closeVac(el.dataset.vclosed);
  else if ((el = on("[data-confirm]"))) openConfirm(el.dataset.confirm);
  else if (on("[data-qclose]")) {
    st.batch = view.batch = null;
    sessionStorage.removeItem("rvm-batch");
    renderQueue();
  } else if ((el = on("[data-flt]"))) {
    st.flt = el.dataset.flt;
    $("#top").innerHTML = topBase();
    renderQueue();
  } else if ((el = on("[data-href]"))) go(el.dataset.href);
  else if (on("[data-pii]")) {
    st.pii = !st.pii;
    const r = route();
    if (r.name === "cand") view.cand = null; // перечитать карточку с ФИО или без
    if (r.name === "triage") renderTriage();
    else render();
  } else if ((el = on("[data-tab]"))) {
    st.tab = el.dataset.tab;
    renderPane();
  } else if ((el = on("[data-letter]"))) {
    st.letter = el.dataset.letter;
    renderPane();
  } else if ((el = on("[data-ans]"))) answer(+el.dataset.q, el.dataset.ans);
  else if ((el = on(".req"))) {
    for (const b of document.querySelectorAll(".req.on")) b.classList.remove("on");
    el.classList.add("on");
    highlight(view.vac.rows.find((r) => r.requirement_id === el.dataset.req)?.evidence ?? []);
  } else if ((el = on(".pt[data-l]"))) {
    for (const b of document.querySelectorAll(".pt.on")) b.classList.remove("on");
    el.classList.add("on");
    highlight([{ line: +el.dataset.l, quote: "" }]);
  }
});

let searchTimer;
document.addEventListener("input", (e) => {
  if (e.target.id !== "q") return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    st.q = e.target.value.trim();
    await renderBase(route());
    const q = $("#q");
    q.focus();
    q.setSelectionRange(q.value.length, q.value.length);
  }, 300);
});

document.addEventListener("change", (e) => {
  if (e.target.matches("[data-pickrow]") && $("#tcount")) $("#tcount").textContent = `(${picked().length})`;
});

document.addEventListener("submit", (e) => {
  if (e.target.id !== "lgform") return;
  e.preventDefault();
  const f = new FormData(e.target);
  login({ login: f.get("login"), password: f.get("password") });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") ["sheet", "upload", "confirm", "pack"].forEach((id) => modal(id, null));
  if (e.key === "Enter" && e.target.matches("[role=button][data-href]")) return go(e.target.dataset.href);
  // «Разбор»: 1–4 — решение, ← → — листать, Enter — открыть разбор кандидата
  const tc = view.tc;
  if (route().name !== "triage" || st.bk !== "need" || !tc || !st.me || document.querySelector(".scrim")) return;
  if (e.target.closest("input,textarea,select") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (["1", "2", "3", "4"].includes(e.key)) decide([tc.id], +e.key, tc.vacancy_id);
  else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    st.ti += e.key === "ArrowRight" ? 1 : -1;
    drawTriage();
  } else if (e.key === "Enter" && !e.target.closest("button,a")) go(`#/candidates/${tc.id}/vacancy/${encodeURIComponent(tc.vacancy_id)}`);
});

prevHash = location.hash;
api("/api/me")
  .then(enter)
  .catch(() => showLogin());
