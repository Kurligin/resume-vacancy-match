// Интерфейс рекрутера: «Кандидаты», разбор кандидата, загрузка. Данные — из /api, без сборки.

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
};
const FILTERS = [
  ["all", "Все"], ["new", "Новые"], ["review", "На рассмотрении"], ["cust", "У заказчика"],
  ["alt", "Другая вакансия"], ["invited", "Приглашены"], ["reserve", "Резерв"], ["rejected", "Отказ"],
];
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

const st = { pii: false, flt: "all", q: "", tab: "req", from: null, batch: sessionStorage.getItem("rvm-batch") };
let view = {}; // данные текущего экрана
let poll = null;

async function api(path, opt = {}) {
  const r = await fetch(path, opt);
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
  return r.status === 204 ? null : r.json();
}

const stateBox = (kind, text) =>
  `<div class="card state ${kind === "err" ? "err" : ""}">${ic(kind === "err" ? "alert" : kind === "empty" ? "inbox" : "clock")}${esc(text)}</div>`;
const failed = (e) => stateBox("err", `Не удалось загрузить данные: ${e.message}. Обновите страницу.`);
const cname = (id) => `Кандидат ${id}`;
const plural = (n, one, few, many) =>
  n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;
const piiTog = () =>
  `<label class="switch">ФИО и пол <button class="tog" role="switch" aria-checked="${st.pii}" data-pii aria-label="Показать ФИО и пол"></button></label>`;

/* ---------- роутинг ---------- */

function route() {
  const p = location.hash.replace(/^#\/?/, "").split("/");
  if (p[0] === "candidates" && p[2] === "vacancy") return { name: "cand", id: +p[1], vid: p[3] ? decodeURIComponent(p[3]) : null };
  if (p[0] === "candidates" && p[1]) return { name: "base", id: +p[1] };
  if (p[0] === "triage" || p[0] === "vacancies") return { name: p[0] };
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
  const r = route();
  for (const a of document.querySelectorAll(".nav a")) {
    const on = a.dataset.sec === (r.name === "cand" || r.name === "base" ? "candidates" : r.name);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  $("#summary").hidden = $("#detail").hidden = r.name !== "cand";
  if (scroll) window.scrollTo({ top: 0 });
  if (r.name === "triage" || r.name === "vacancies") {
    stopPoll();
    const t = r.name === "triage" ? "Разбор откликов" : "Вакансии";
    $("#top").innerHTML = `<div class="head"><span class="avatar blue">${ic(r.name === "triage" ? "inbox" : "brief")}</span><div class="who"><h1>${t}</h1></div></div>${stateBox("empty", "Раздел появится в следующей версии.")}`;
    $("#doc").innerHTML = "";
    return;
  }
  if (r.name === "cand") return renderCand(r);
  return renderBase(r);
}

/* ---------- «Кандидаты» ---------- */

async function renderBase(r) {
  if (!view.base) $("#top").innerHTML = stateBox("load", "Загружаем кандидатов…");
  try {
    const q = st.q ? `?q=${encodeURIComponent(st.q)}` : "";
    const [list, vacs] = await Promise.all([api(`/api/candidates${q}`), api("/api/vacancies")]);
    view.base = { list, vacs };
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
  $("#navbadge").textContent = view.base.list.filter((c) => c.status === "new").length || "";
}

function topBase() {
  const { list, vacs } = view.base;
  const n = (f) => list.filter((c) => f === "all" || c.status === f).length;
  const week = list.filter((c) => Date.now() - new Date(c.created_at) < 7 * 864e5).length;
  const open = vacs.filter((v) => v.is_open).length;
  const sel = route().id ?? list[0]?.id;
  let h = `<div class="head"><span class="avatar blue">${ic("db")}</span><div class="who"><h1>Кандидаты</h1>
    <div class="sub"><span>${ic("users", "i-sm")}${list.length} в базе</span><span>${ic("brief", "i-sm")}${open} ${plural(open, "открытая вакансия", "открытые вакансии", "открытых вакансий")}</span></div></div></div>
   <div class="stats">
    <div class="card stat warn"><div class="k">${ic("bell", "i-sm")}Новые</div><div class="v num">${n("new")}</div></div>
    <div class="card stat"><div class="k">${ic("clock", "i-sm")}На рассмотрении</div><div class="v num">${n("review")}</div></div>
    <div class="card stat"><div class="k">${ic("archive", "i-sm")}В резерве</div><div class="v num">${n("reserve")}</div></div>
    <div class="card stat"><div class="k">${ic("inbox", "i-sm")}Загружено за неделю</div><div class="v num">${week}</div></div></div>
   <div id="queue"></div>`;
  for (const v of vacs.filter((x) => x.reserve_matches.length)) {
    const m = v.reserve_matches.length;
    h += `<div class="callout"><span class="ico">${ic("archive")}</span><div><b>Новая вакансия «${esc(v.title)}»: в резерве ${plural(m, "нашёлся", "нашлись", "нашлись")} ${m} ${plural(m, "подходящий", "подходящих", "подходящих")}.</b> ${v.reserve_matches.map(cname).join(", ")}.</div><a class="btn btn-white" href="#/candidates/${v.reserve_matches[0]}">Показать</a></div>`;
  }
  const rows = list.filter((c) => st.flt === "all" || c.status === st.flt);
  h += `<div class="card rank"><div class="tools"><label class="search">${ic("search", "i-sm")}<input id="q" type="search" value="${esc(st.q)}" placeholder="Навык, должность…" aria-label="Поиск"></label><div class="chips">
    ${FILTERS.map(([k, t]) => `<button class="chip" aria-pressed="${st.flt === k}" data-flt="${k}">${t} <span class="num">${n(k)}</span></button>`).join("")}</div></div>`;
  if (!rows.length)
    h += `<div class="stub" style="padding:18px">${ic("search")}${list.length || st.q ? "Никого не нашлось — измените поиск или фильтр." : "В базе пока пусто. Нажмите «Загрузить»."}</div>`;
  h += rows
    .map(
      (c) => `<button class="crow" aria-selected="${c.id === sel}" data-href="#/candidates/${c.id}">
     <span class="tile round">${ic("user")}</span>
     <span><div class="t" style="font-weight:600">${cname(c.id)}</div><div class="meta"><span>${ic("cal", "i-sm")}${day(c.created_at)}</span><span>${esc(c.source)}</span>${c.versions > 1 ? `<span>${ic("hist", "i-sm")}версий: ${c.versions}</span>` : ""}</div></span>
     <span class="best"><b class="num">${num(c.best_score)}</b><span>лучший балл</span></span>
     <span><span class="pill ${PS[c.status][1]}">${PS[c.status][0]}</span></span>
     <span class="reply btn btn-ghost" data-reply="${c.id}">${ic("send", "i-sm")}Ответить</span></button>`,
    )
    .join("");
  return h + "</div>";
}

const EVENT = {
  uploaded: () => "Загружено резюме",
  possible_duplicate: (d) => `Похож на ${cname(d.of)} — возможный дубль`,
  merged: (d) => `Объединён с карточкой ${cname(d.from)}`,
  not_duplicate: () => "Отмечено: не дубль",
  evaluated: (d, t) => `Оценка по «${t}»: ${num(d.score)}`,
  status: (d) => `Статус: ${PS[d.to]?.[0] ?? d.to}`,
  clarified: (d) => `Уточнено: ${d.check}`,
  answer: (d, t) => `Ответ по ${d.requirement} («${t}»): ${ST[d.answer]?.[0] ?? d.answer} · балл ${num(d.before)} → ${num(d.after)}`,
  deleted: () => "Удалён",
};

async function renderCard(id) {
  let c;
  try {
    c = await api(`/api/candidates/${id}`);
  } catch (e) {
    $("#doc").innerHTML = `<div class="state err">${ic("alert")}${esc(e.message)}</div>`;
    return;
  }
  view.card = c;
  const title = (vid) => c.evaluations.find((e) => e.vacancy_id === vid)?.title ?? vid ?? "";
  const ev = [...c.evaluations].sort((a, b) => b.score - a.score);
  const best = [...c.evaluations].sort((a, b) => (a.place ?? 99) - (b.place ?? 99))[0];
  const hist = c.events.filter((e) => EVENT[e.type]);
  $("#doc").innerHTML = `<div class="ccard"><div class="ch1"><span class="avatar">${ic("user")}</span><div><h2>${cname(c.id)}</h2><div class="sub" style="margin-top:2px"><span>${c.versions.length} ${plural(c.versions.length, "версия", "версии", "версий")} резюме · с ${day(c.created_at)}</span></div></div></div>
   <div class="sec">Статус</div><span class="pill ${PS[c.status][1]}">${PS[c.status][0]}</span>
   <div class="sec">Оценки по вакансиям</div>${ev.length ? ev.map((e) => { const v = VERDICT[e.verdict]; return `<a class="ev" href="#/candidates/${c.id}/vacancy/${encodeURIComponent(e.vacancy_id)}">${ic(v[3], v[4])}<span>${esc(e.title ?? e.vacancy_id)}</span><span class="n num">${num(e.score)}</span></a>`; }).join("") : `<div class="stub">${ic("clock")}Оценок пока нет.</div>`}
   <div class="sec">История</div><div class="tl">${hist.map((e) => `<div class="tli ${e.who !== "система" ? "acc" : ""}"><time class="num">${stamp(e.at)}</time>${esc(EVENT[e.type](e.details, title(e.vacancy_id)))}</div>`).join("")}</div>
   <div class="cactions">${best ? `<a class="btn btn-ghost" href="#/candidates/${c.id}/vacancy/${encodeURIComponent(best.vacancy_id)}">${ic("list", "i-sm")}Открыть разбор</a>` : ""}<button class="btn btn-primary" data-reply="${c.id}">${ic("send", "i-sm")}Ответить</button>
    ${c.status !== "reserve" ? `<button class="btn btn-ghost" data-confirm="reserve">${ic("archive", "i-sm")}В резерв</button>` : ""}<button class="btn btn-danger" data-confirm="delete">${ic("trash", "i-sm")}Удалить</button></div></div>`;
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
      .map((f) => `<span class="qf ${f.state === "error" ? "err" : "run"}">${ic(f.state === "error" ? "alert" : "clock", "i-sm")}${esc(f.state === "error" && f.message.startsWith(f.name) ? f.message : `${f.name} · ${f.state === "error" ? f.message : "распознаём"}`)}</span>`)
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
      const [card, report] = await Promise.all([api(`/api/candidates/${r.id}`), api(`/api/evaluations?candidate=${r.id}`)]);
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
    <div class="side"><span style="display:flex;gap:8px"><a class="btn btn-ghost" href="#/candidates/${id}">${ic("hist", "i-sm")}Карточка и история</a><button class="btn btn-ghost" data-reply="${id}">${ic("send", "i-sm")}Ответить</button></span>${piiTog()}</div></div>`;
  if (!vac) return h;
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
        h += `<div class="grp">${SECTION[g] ?? esc(g)}</div>`;
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
            return `<li>${ic("chat", "c-acc")}<span style="flex:1">${esc(q.text)}${req ? `<div class="qa"><div class="seg">${ANSWERS.map(([val, l]) => `<button aria-pressed="${done && row.status === val}" data-ans="${val}" data-q="${i}">${l}</button>`).join("")}</div><span class="eff">${done ? `→ «${esc(req.text)}»: <b>${ST[row.status][0]}</b> · ${FROM_CANDIDATE}` : `влияет на «${esc(req.text)}»`}</span><input placeholder="заметка рекрутера" aria-label="Заметка" data-note="${i}" value="${esc(done ? row.note : "")}"></div>` : ""}</span></li>`;
          })
          .join("")}</ul>`
      : `<div class="stub">${ic("check")}Вопросов нет: по резюме всё ясно.</div>`;
  } else {
    const ls = v.letters;
    const cur = ls.find((l) => l.kind === st.letter) ?? ls.find((l) => l.suggested) ?? ls[0];
    h = cur
      ? `<div class="why">Черновик — отправляет рекрутер.</div>${ls.length > 1 ? `<div class="seg" style="width:max-content;margin-top:10px">${ls.map((l) => `<button aria-pressed="${l === cur}" data-letter="${esc(l.kind)}">${LETTER[l.kind] ?? esc(l.kind)}</button>`).join("")}</div>` : ""}<div class="letter" id="lettertext">${esc(cur.text)}</div><div class="actions"><button class="btn btn-ghost" data-copy="#lettertext">${ic("copy", "i-sm")}Скопировать</button></div>`
      : `<div class="why">Для этой пары письма нет.</div>`;
  }
  const p = $("#pane");
  p.innerHTML = h;
  p.classList.remove("enter");
  void p.offsetWidth;
  p.classList.add("enter");
}

const MASK_RE = /\[([^\[\]]{2,24})\]/g;

function renderDoc() {
  const r = view.cand.report.resume;
  const lines = st.pii ? r.original : r.lines;
  let h = `<h2>${ic("file")}${cname(view.cand.id)} · ${st.pii ? "исходный текст" : "текст, который видела модель"}</h2>`;
  let blk = null;
  for (const ln of lines) {
    let tx = esc(ln.text);
    if (!st.pii) tx = tx.replace(MASK_RE, (_, k) => `<span class="mask">${ic("eyeoff", "i-sm")}<b>${k} скрыто</b></span>`);
    if (ln.block && ln.block !== blk) h += `<div class="blk">${esc(ln.block)}</div>`;
    blk = ln.block;
    h += `<div class="ln" id="L${ln.n}"><span>${ln.n}</span><span>${tx}</span></div>`;
  }
  $("#doc").innerHTML = h;
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
    if (q && !st.pii) s.innerHTML = s.dataset.orig.replace(esc(q), `<mark>${esc(q)}</mark>`);
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
    toast(`Пересчитано: ${esc(v.title)} — ${num(s.score)} (вилка ${num(s.low)}–${num(s.high)})`);
  } catch (e) {
    toast(`Ответ не сохранён: ${esc(e.message)}`);
  }
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
        ? `<div class="seg">${ls.map((l) => `<button aria-pressed="${l === cur}" data-rkind="${esc(l.kind)}">${LETTER[l.kind] ?? esc(l.kind)}</button>`).join("")}</div>
       <label class="fld"><span>Тема</span><input value="МФТИ: ваш отклик на «${esc(v.title)}»" aria-label="Тема"></label>
       <textarea class="ed" aria-label="Текст письма" id="replytext">${esc(cur.text)}</textarea>
       <div class="hint" style="display:flex;gap:6px;align-items:center">${ic("shield", "c-full i-sm")}Текст собран из таблицы требований, его можно править. Смена шаблона заменит текст.</div>
       <div class="actions"><button class="btn btn-ghost" data-close>Отмена</button><button class="btn btn-primary" data-copy="#replytext">${ic("copy", "i-sm")}Скопировать</button></div>`
        : `<div class="stub">${ic("help")}Для этого кандидата письма нет: нет оценок по вакансиям.</div><div class="actions"><button class="btn btn-ghost" data-close>Закрыть</button></div>`
    }`;
  };
  d.addEventListener("click", (e) => {
    const k = e.target.closest("[data-rkind]");
    if (k) {
      cur = ls.find((l) => l.kind === k.dataset.rkind);
      draw();
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
      toast(`${cname(c.id)} в резерве`, async () => {
        await setStatus(c.id, prev);
        render();
      }, ["Открыть резерв", () => { st.flt = "reserve"; go(`#/candidates/${c.id}`); }]);
    } else {
      await api(`/api/candidates/${c.id}`, { method: "DELETE" });
      toast(`${cname(c.id)} удалён со всеми данными`);
      go("#/candidates");
    }
  } catch (e) {
    toast(`Не получилось: ${esc(e.message)}`);
  }
}

async function openUpload() {
  const d = modal(
    "upload",
    `<div class="sheet" role="dialog" aria-label="Загрузка"><h3>${ic("upload", "c-acc i-lg")}Загрузить</h3>
   <div class="seg"><button aria-pressed="true">${ic("user", "i-sm")}Резюме</button><button aria-pressed="false" disabled title="скоро">${ic("brief", "i-sm")}Вакансии</button></div>
   <label class="drop" id="drop"><input type="file" id="files" multiple hidden accept=".docx,.doc,.pdf,.rtf,.txt,.odt,.html,.htm,.jpg,.jpeg,.png,.heic,.webp,.tif,.tiff,.zip"><div class="di">${ic("upload")}</div><b>Перетащите резюме или выберите файлы</b><small>одно, пачка или zip · docx, pdf, rtf, txt, doc, сканы jpg и png</small><div class="files" id="picked"></div></label>
   <label class="fld" style="margin-top:8px"><span>Отклик</span><select class="sel" id="applied" aria-label="Отклик на вакансию"><option value="">не указано</option></select></label>
   <div class="hint" style="display:flex;gap:6px;align-items:center">${ic("eyeoff", "i-sm")}ФИО, контакты, пол и возраст скрываются до анализа. Повторное резюме того же человека попадёт в его карточку.</div>
   <div class="actions"><button class="btn btn-ghost" data-uclose>Отмена</button><button class="btn btn-primary" data-ugo disabled>${ic("upload", "i-sm")}Загрузить</button></div></div>`,
  );
  let files = [];
  const pick = (list) => {
    files = [...list];
    d.querySelector("#picked").textContent = files.map((f) => f.name).join(", ");
    d.querySelector("[data-ugo]").disabled = !files.length;
  };
  d.querySelector("#files").addEventListener("change", (e) => pick(e.target.files));
  const drop = d.querySelector("#drop");
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    pick(e.dataTransfer.files);
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
      toast(`Загрузка не принята: ${esc(err.message)}`);
    }
  });
  try {
    const vacs = (await api("/api/vacancies")).filter((v) => v.is_open);
    d.querySelector("#applied").insertAdjacentHTML("beforeend", vacs.map((v) => `<option value="${esc(v.id)}">${esc(v.title)}</option>`).join(""));
  } catch {
    /* без списка вакансий загрузка всё равно возможна */
  }
}

let toastTimer;
let toastAct = {};
function toast(html, undo, goTo) {
  $("#toast")?.remove();
  clearTimeout(toastTimer);
  const d = document.createElement("div");
  d.id = "toast";
  d.className = "toast";
  d.setAttribute("role", "status");
  d.innerHTML = `<span>${html}</span>${goTo ? `<button data-tgo>${goTo[0]}</button>` : ""}${undo ? "<button data-undo>Отменить</button>" : ""}`;
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
  } else if (on("[data-close]") || t.id === "sheet") modal("sheet", null);
  else if (on("[data-uclose]") || t.id === "upload") modal("upload", null);
  else if (on("[data-cancel]") || t.id === "confirm") modal("confirm", null);
  else if ((el = on("[data-ok]"))) confirmed(el.dataset.ok);
  else if ((el = on("[data-reply]"))) {
    e.preventDefault();
    openReply(+el.dataset.reply);
  } else if (on("[data-upload]")) openUpload();
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
    $("#top").innerHTML = topCand(view.vac);
    renderDoc();
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

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") ["sheet", "upload", "confirm"].forEach((id) => modal(id, null));
});

prevHash = location.hash;
render();
