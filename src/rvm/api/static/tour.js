/* Тур по сценариям работы: затемнение с вырезом вокруг элемента и подсказка рядом.
   Живёт отдельно от app.js: ходит по hash-маршрутам и читает DOM. Кандидатов для сценариев
   выбирает по данным API в момент запуска — под конкретные файлы ничего не зашито. */

const KEY = "rvm-tour"; // sessionStorage: сценарии, шаг, выбранные кандидаты — переживают перезагрузку при смене роли
const OFFERED = "rvm-tour-offered"; // localStorage: приглашение уже показывали
const PASSED = "rvm-tour-passed"; // sessionStorage: ключи пройденных сценариев — отметка «✓ пройден» в меню
const FROM = "со слов кандидата";
const PAD = 6;
const GAP = 12;

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const num = (x) => Math.round(x);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const cand = (id, vid) => `#/candidates/${id}/vacancy/${encodeURIComponent(vid)}`;
const load = (k, s = sessionStorage) => {
  try {
    return JSON.parse(s.getItem(k));
  } catch {
    return null;
  }
};
const save = (k, v, s = sessionStorage) => {
  try {
    if (v == null) s.removeItem(k);
    else s.setItem(k, JSON.stringify(v));
  } catch {
    /* хранилище недоступно — тур просто не переживёт перезагрузку */
  }
};

async function api(path, body) {
  const opt = { headers: { "X-Requested-With": "fetch" } };
  if (body !== undefined) Object.assign(opt, { method: "POST", body: JSON.stringify(body), headers: { ...opt.headers, "Content-Type": "application/json" } });
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(`сервер ответил ${r.status}`);
  return r.json();
}

/* ---------- кандидаты для сценариев ---------- */

const best = (a, f) => a.reduce((m, x) => (m && f(m) >= f(x) ? m : x), null);

function questionOf(v, open) {
  return (v.questions?.items ?? []).findIndex((q) => {
    const row = v.rows.find((x) => x.requirement_id === q.requirement_id);
    if (!row || !v.requirements.some((x) => x.id === q.requirement_id)) return false;
    return open ? row.status === "unknown" && !row.flags.includes(FROM) : row.flags.includes(FROM);
  });
}

export function pick(reps, tri) {
  const pairs = reps.flatMap(({ id, r }) => r.vacancies.map((v) => ({ id, r, v })));
  const title = (r, vid) => r.vacancies.find((v) => v.id === vid)?.title ?? vid;
  const sc = (p) => p && { id: p.id, vid: p.v.id, title: p.v.title, score: p.v.score.score, low: p.v.score.low, high: p.v.score.high };
  const rd = best(reps.filter((x) => x.r.redirect), (x) => x.r.redirect.gap);
  const one = rd ?? best(reps, (x) => x.r.vacancies.length);
  const strong = best(pairs.filter((p) => p.v.verdict === "fit" && !p.v.score.blockers.length), (p) => p.v.score.score);
  const partial = best(pairs.filter((p) => p.v.verdict === "partial" && p.id !== strong?.id), (p) => p.v.score.high - p.v.score.low);
  const none = reps.find((x) => x.r.none_fit);
  // ответы: сначала неотвеченная строка «не указано»; на повторном проходе — уже отвеченная
  const order = [...pairs].sort((a, b) => (b.id === one?.id) - (a.id === one?.id));
  const open = order.find((p) => questionOf(p.v, true) >= 0);
  const ans = open ?? order.find((p) => questionOf(p.v, false) >= 0);
  const q = ans && questionOf(ans.v, !!open);
  return {
    one: one && { id: one.id, vid: one.r.vacancies[0].id, n: one.r.vacancies.length },
    rd: rd && { id: rd.id, from: rd.r.redirect.from_vacancy, to: rd.r.redirect.to_vacancy, fromT: title(rd.r, rd.r.redirect.from_vacancy), toT: title(rd.r, rd.r.redirect.to_vacancy) },
    strong: sc(strong),
    partial: sc(partial),
    none: none && { id: none.id, vid: none.r.vacancies[0].id },
    tri: !!tri && tri.need.length + tri.fit.length + tri.nofit.length > 0,
    ans: ans && { ...sc(ans), q, rid: ans.v.questions.items[q].requirement_id, answered: !open },
  };
}

async function collect() {
  const list = await api("/api/candidates");
  // shortcut: отчёты читаются по первым 60 кандидатам; для большой базы нужен подбор на сервере
  const reps = await Promise.all(
    list.slice(0, 60).map((c) =>
      api(`/api/evaluations?candidate=${c.id}`)
        .then((r) => ({ id: c.id, r }))
        .catch(() => null),
    ),
  );
  const tri = await api("/api/triage").catch(() => null);
  return pick(reps.filter((x) => x?.r.vacancies.length), tri);
}

/* ---------- шаги ---------- */

const RANK = ".rank[role=listbox]";
const SEL_ROW = ".rank[role=listbox] [aria-selected=true]";
const press = (sel, attr = "aria-selected") => {
  const b = $(sel);
  if (b && b.getAttribute(attr) !== "true") b.click();
};
const tab = (t) => () => press(`[data-tab=${t}]`);
const bucket = (b) => () => press(`[data-bk=${b}]`, "aria-pressed");
const clickIt = (el) => el.click();

const SCENARIOS = [
  {
    key: "one",
    title: "Резюме → рейтинг вакансий",
    steps: (c) => [
      { go: cand(c.one.id, c.one.vid), sel: RANK, title: "Рейтинг вакансий", text: `Одно резюме сверено со всеми открытыми вакансиями (${c.one.n}). У каждой — балл, вилка и вердикт. Вакансия с блокером стоит ниже вакансий без блокеров.` },
      { prep: tab("req"), sel: "#detail", title: "Таблица требований", text: "Каждое требование вакансии: статус, цитата из резюме с номером строки и комментарий. Цитату, которой нет в резюме, код не пропускает — строка становится «не указано»." },
      {
        prep: tab("req"),
        sel: () => $("#pane .req .q code")?.closest(".req"),
        title: "Проверка цитаты",
        text: "Нажмите на строку — цитата подсветится в тексте резюме.",
        act: clickIt,
        done: () => !!$("#doc .ln.hl"),
      },
      { sel: () => $("#doc .ln.hl") ?? $("#doc"), title: "Текст резюме", text: "Справа — текст, который видела модель, с номерами строк. Любой статус можно проверить по исходнику." },
      { sel: "#summary", title: "Кратко", text: "Сильные стороны, риски и рекомендация. Пункты ссылаются на строки резюме." },
      { sel: "[data-tab=qs]", title: "Вопросы кандидату", text: "Собраны из строк «не указано», «частично» и несостыковок. Вопросы о семье, здоровье и возрасте отсекает фильтр.", act: clickIt, done: () => $("[data-tab=qs]")?.getAttribute("aria-selected") === "true" },
    ],
  },
  {
    key: "rd",
    title: "Переадресация на другую вакансию",
    steps: (c) => [
      { go: cand(c.rd.id, c.rd.from), sel: "[data-tour=redirect]", title: "Лучше подходит другая вакансия", text: `Кандидат откликнулся на «${c.rd.fromT}», но выше оценка по «${c.rd.toT}». Плашка показывает обе оценки.` },
      { sel: RANK, title: "Почему", text: "Переадресация — если у лучшей вакансии нет блокеров и вердикт не хуже «частично», а у вакансии отклика есть блокер или разрыв от 10 баллов." },
      {
        sel: "[data-tour=redirect] a",
        title: "Разбор по лучшей вакансии",
        text: "Нажмите «Открыть» — таблица требований по вакансии, куда стоит предложить кандидата.",
        act: clickIt,
        done: () => location.hash === cand(c.rd.id, c.rd.to),
      },
      { go: "#/triage", prep: bucket("need"), sel: ".decide [data-act='2']", title: "Решение в «Разборе»", text: "Такие отклики попадают в «Нужно ваше решение». Вариант 2 — предложить кандидату другую вакансию; в работе его выбирают кнопкой или клавишей 2 (под туром клавиши не действуют)." },
    ],
  },
  {
    key: "cmp",
    title: "Сильный и частичный кандидат",
    steps: (c) => [
      { go: cand(c.strong.id, c.strong.vid), sel: SEL_ROW, title: "Сильный кандидат", text: `«${c.strong.title}»: балл ${num(c.strong.score)}, вилка ${num(c.strong.low)}–${num(c.strong.high)}. Узкая вилка — ответы на вопросы итог почти не изменят.` },
      { sel: "#summary .rec", title: "Рекомендация", text: "Рекомендация следует из вердикта и вилки. Сильного — сразу в пакет руководителю." },
      { go: cand(c.partial.id, c.partial.vid), sel: SEL_ROW, title: "Частичный кандидат", text: `«${c.partial.title}»: балл ${num(c.partial.score)}, вилка ${num(c.partial.low)}–${num(c.partial.high)}. Итог зависит от ответов по строкам «не указано».` },
      { prep: tab("req"), sel: "#pane .why", title: "Ширина вилки — главный сигнал", text: "Нижняя граница — если всё «не указано» окажется «нет», верхняя — если «да». Что спросить — во вкладке «Вопросы»." },
      { sel: "#summary .rec", title: "Другая рекомендация", text: "Для частичного — уточнить пробелы на звонке или оставить в резерве, а не отказ вслепую." },
    ],
  },
  {
    key: "none",
    title: "Ни одна вакансия не подходит",
    steps: (c) => [
      { go: cand(c.none.id, c.none.vid), sel: "[data-tour=nonefit]", title: "Подходящей вакансии нет", text: "Система говорит это прямо и предлагает резерв, а не подбирает «лучшее из плохого»." },
      { sel: RANK, title: "Рейтинг всё равно есть", text: "По каждой вакансии видно балл и каких обязательных требований не хватило." },
      { go: `#/candidates/${c.none.id}`, sel: "[data-confirm=reserve]", title: "Резерв", text: "Кандидат из резерва сверяется с каждой новой вакансией. Совпадения видны в разделе «Вакансии»." },
      { go: "#/triage", prep: bucket("nofit"), sel: ".buckets", title: "Автоотказа нет", text: "В «Разборе» такие отклики лежат в «Явно не подходят». Отказ подтверждает рекрутер, часть откликов система выносит на выборочную проверку." },
    ],
  },
  {
    key: "tri",
    title: "Пачка откликов → пакет руководителю",
    writes: true,
    steps: () => [
      { go: "#/triage", prep: bucket("need"), sel: ".buckets", title: "Разбор откликов", text: "Пачка откликов разложена по корзинам. Внимание нужно только «Нужно ваше решение»." },
      { prep: bucket("need"), sel: ".tcard", title: "Карточка отклика", text: "Лучшая вакансия, балл с вилкой, почему отклик здесь, доводы за и против со ссылками на строки резюме." },
      { prep: bucket("need"), sel: ".decide", title: "Решение", text: "В работе решение принимают кнопкой или клавишами 1–4 (под туром клавиши не действуют). Подсвечен вариант системы, решает рекрутер." },
      {
        prep: bucket("fit"),
        sel: "[data-tpack]",
        title: "Пакет руководителю",
        text: "В «Явно подходят» нажмите «Пакет для заказчика».",
        miss: "Сейчас в «Явно подходят» никого нет. Пакет собирается из отмеченных там кандидатов или из разбора кандидата — кнопка «Для заказчика».",
        act: clickIt,
        done: () => !!$("#pack table.cmp"),
      },
      {
        sel: "#pack .sheet",
        title: "Сравнительная таблица",
        text: "Обязательные требования по кандидатам. В DOCX — ещё «Кратко», риски и вопросы, ФИО по умолчанию скрыты. «Скачать DOCX» передаст кандидатов руководителю; «Далее» — пропустить.",
        act: (el) => el.querySelector("[data-pclose]")?.click(),
        done: () => !$("#pack"),
        optional: true, // окно пакета не открывали — шаг пропускается
        leave: () => $("#pack [data-pclose]")?.click(),
      },
      {
        role: "manager",
        go: "#/candidates",
        sel: "#top .card.rank",
        title: "Взгляд руководителя",
        text: () => ($("#top .crow") ? "Руководитель видит только переданных ему кандидатов, без ФИО, с тем же разбором." : "Руководитель видит только переданных ему кандидатов. Пока никого не передали — после скачивания пакета они появятся здесь."),
      },
      { role: "manager", go: "#/candidates", sel: "#doc .cactions", title: "Решение руководителя", text: "«Пригласить на интервью» или «Не подходит» с комментарием. Решение попадает в журнал кандидата." },
    ],
  },
  {
    key: "ans",
    title: "Ответы кандидата → пересчёт",
    writes: true,
    steps: (c) => {
      const li = () => $(`#pane [data-q="${c.ans.q}"]`)?.closest("li");
      let did = !!c.ans.answered; // залипает: на вкладке «Требования» строки вопроса уже нет
      const answered = () => (did ||= !!li()?.querySelector(".eff")?.textContent.includes(FROM));
      return [
        { go: cand(c.ans.id, c.ans.vid), prep: tab("req"), sel: "#pane .why", title: "До ответов", text: `«${c.ans.title}»: балл ${num(c.ans.score)}, вилка ${num(c.ans.low)}–${num(c.ans.high)}. Строки «не указано» — то, что стоит спросить.` },
        { sel: "[data-tab=qs]", title: "Вопросы", text: "Откройте вкладку «Вопросы».", act: clickIt, done: () => $("[data-tab=qs]")?.getAttribute("aria-selected") === "true" },
        {
          prep: tab("qs"),
          sel: li,
          title: "Ответ кандидата",
          text: () => (answered() ? "Ответ по этой строке уже отмечен — он учтён в балле. Поменять его можно кнопками в строке." : "Если кандидат ответил на звонке «да», нажмите «Да» в подсвеченной строке — ответ запишется, балл пересчитает код без модели. «Далее» — дальше без записи."),
          manual: true, // запись только по кнопке пользователя: «Далее» за него ничего не нажимает
          done: answered,
        },
        { sel: SEL_ROW, title: "Балл пересчитан", text: () => (answered() ? "Балл, вилка и вердикт обновились. Места вакансий и переадресация пересчитываются вместе с ними." : "После ответа здесь обновятся балл, вилка и вердикт, а с ними места вакансий и переадресация.") },
        { sel: "#summary", title: "«Кратко» пересобран", text: () => (answered() ? "Закрытые строки ушли из рисков, рекомендация — с новым баллом." : "После ответа закрытые строки уйдут из рисков, рекомендация пересчитается с новым баллом.") },
        { prep: tab("req"), sel: `#pane .req[data-req="${c.ans.rid}"]`, title: "Видно источник", text: () => (answered() ? "Строка помечена «со слов кандидата»: подтверждено ответом, а не резюме." : "После ответа строка получит пометку «со слов кандидата»: подтверждено ответом, а не резюме.") },
      ];
    },
  },
];

// шаг без своего адреса — на экране предыдущего шага сценария: так «Назад» возвращает на нужный экран.
// Демо-вход отключён — шаги чужой роли выпадают (demo не задан у состояния старой версии — не фильтруем)
// si — номер сценария в туре, k/n — номер шага внутри сценария и их число
const stepsOf = (keys, ctx) =>
  keys.flatMap((key, si) => {
    const sc = SCENARIOS.find((s) => s.key === key);
    let go = null;
    const a = sc
      .steps(ctx)
      .map((s) => ({ ...s, go: (go = s.go ?? go), sc: sc.title, key }))
      .filter((s) => ctx.demo !== false || (s.role ?? "recruiter") === ctx.role);
    return a.map((s, k) => ({ ...s, si, k, n: a.length }));
  });
const hit = (s) => !!(s?.act || s?.manual); // подсвеченный элемент кликабелен

/* ---------- слой: затемнение, вырез, подсказка ---------- */

let st = null; // { keys, i, ctx, menu? } — текущее состояние тура
let ui = null;
let steps = [];
let role = "recruiter";
let tick = null;
let gen = 0; // номер показа: поздний ответ старого шага не трогает новый
let shown = -1; // шаг, чья подсказка на экране; -1 — идёт переход. «Далее»/«Назад» действуют только от него
let calm = 0; // до этого момента кнопки молчат: «Далее», нажатое для шага до автоперехода, не листает новый

function mount() {
  if (ui) return ui;
  const d = document.createElement("div");
  d.className = "tour";
  d.innerHTML = `<div class="tour-catch"></div><div class="tour-hole"></div>
   <div class="tour-pop" role="dialog" aria-modal="true" aria-labelledby="tour-t" tabindex="-1">
    <div class="tour-bar" aria-hidden="true"><i></i></div><div class="tour-k"></div><h3 id="tour-t"></h3><div class="tour-x" aria-live="polite"></div><div class="tour-nav"></div></div>`;
  document.body.appendChild(d);
  d.querySelector(".tour-catch").addEventListener("click", (e) => e.stopPropagation());
  d.querySelector(".tour-pop").addEventListener("click", onPop);
  ui = { d, catch: d.querySelector(".tour-catch"), hole: d.querySelector(".tour-hole"), pop: d.querySelector(".tour-pop") };
  return ui;
}

function onPop(e) {
  e.stopPropagation();
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.t === "next") next();
  else if (b.dataset.t === "prev") stepBack();
  else if (b.dataset.t === "end") end();
  else if (b.dataset.t === "menu") open();
  else if (b.dataset.sc) start(b.dataset.sc === "*" ? SCENARIOS.filter((s) => usable(s, st.ctx)).map((s) => s.key) : [b.dataset.sc]);
}

const NEED = { one: (c) => c.one, rd: (c) => c.rd, cmp: (c) => c.strong && c.partial, none: (c) => c.none, tri: (c) => c.tri, ans: (c) => c.ans };
const need = (s, c) => !!NEED[s.key](c);
const usable = (s, c) => need(s, c) && stepsOf([s.key], c).length > 0;

function popup(kicker, title, html, nav, done = 0) {
  const { pop } = mount();
  pop.querySelector(".tour-bar i").style.width = `${done * 100}%`;
  pop.querySelector(".tour-k").textContent = kicker;
  pop.querySelector("#tour-t").textContent = title;
  pop.querySelector(".tour-x").innerHTML = html;
  pop.querySelector(".tour-nav").innerHTML = nav;
}

function hole(r, interactive) {
  const { hole: h, catch: c } = ui;
  if (!r) {
    Object.assign(h.style, { left: "50%", top: "50%", width: "0px", height: "0px" });
    c.style.clipPath = "none";
    return;
  }
  const x = r.left - PAD;
  const y = r.top - PAD;
  const x2 = r.right + PAD;
  const y2 = r.bottom + PAD;
  Object.assign(h.style, { left: `${x}px`, top: `${y}px`, width: `${x2 - x}px`, height: `${y2 - y}px` });
  // вырез в ловушке кликов: по подсвеченному элементу клик проходит, по затемнению — нет
  c.style.clipPath = interactive ? `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${x}px ${y}px, ${x2}px ${y}px, ${x2}px ${y2}px, ${x}px ${y2}px, ${x}px ${y}px)` : "none";
}

function place(el, interactive) {
  const vw = innerWidth;
  const vh = innerHeight;
  const { pop } = ui;
  let r = null;
  if (el?.isConnected && el.getClientRects().length) {
    const b = el.getBoundingClientRect();
    // видимая часть цели: высокий блок обрезается по краю экрана
    r = { left: Math.max(b.left, 4), top: Math.max(b.top, 4), right: Math.min(b.right, vw - 4), bottom: Math.min(b.bottom, vh - 4) };
    if (r.right <= r.left || r.bottom <= r.top) r = null;
  }
  hole(r, interactive);
  const pw = pop.offsetWidth;
  const ph = pop.offsetHeight;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
  let left = (vw - pw) / 2;
  let top = (vh - ph) / 2;
  if (r) {
    const cx = clamp((r.left + r.right) / 2 - pw / 2, GAP, vw - pw - GAP);
    const cy = clamp((r.top + r.bottom) / 2 - ph / 2, GAP, vh - ph - GAP);
    if (r.bottom + PAD + GAP + ph <= vh - GAP) [left, top] = [cx, r.bottom + PAD + GAP];
    else if (r.top - PAD - GAP - ph >= GAP) [left, top] = [cx, r.top - PAD - GAP - ph];
    else if (r.right + PAD + GAP + pw <= vw - GAP) [left, top] = [r.right + PAD + GAP, cy];
    else if (r.left - PAD - GAP - pw >= GAP) [left, top] = [r.left - PAD - GAP - pw, cy];
    else {
      // цель во весь экран — подсказка в углу, который меньше всего её закрывает
      const over = ([x, y]) => Math.max(0, Math.min(x + pw, r.right) - Math.max(x, r.left)) * Math.max(0, Math.min(y + ph, r.bottom) - Math.max(y, r.top));
      const corners = [[GAP, GAP], [vw - pw - GAP, GAP], [GAP, vh - ph - GAP], [vw - pw - GAP, vh - ph - GAP]];
      [left, top] = corners.reduce((m, c) => (over(c) < over(m) ? c : m));
    }
  }
  pop.style.left = `${clamp(left, GAP, Math.max(GAP, vw - pw - GAP))}px`;
  pop.style.top = `${clamp(top, GAP, Math.max(GAP, vh - ph - GAP))}px`;
}

const find = (sel) => (typeof sel === "function" ? sel() : $(sel));

async function waitFor(s, g, ms = 5000) {
  for (const t0 = Date.now(); Date.now() - t0 < ms; await sleep(120)) {
    if (g !== gen) return null;
    s.prep?.();
    const el = find(s.sel);
    if (el?.getClientRects().length && !$("#top .state")) return el;
  }
  return null;
}

const hashTo = (h) =>
  location.hash === h
    ? Promise.resolve()
    : new Promise((ok) => {
        const done = () => {
          removeEventListener("hashchange", done);
          ok();
        };
        addEventListener("hashchange", done);
        location.hash = h;
        setTimeout(done, 400);
      });

async function show(i, auto = false) {
  if (i < 0) return;
  const back = st.i > i || (st.i === i && !!st.back); // направление переживает перезагрузку при смене роли
  const g = ++gen;
  shown = -1;
  const prev = steps[st.i];
  if (prev && st.i !== i) prev.leave?.();
  if (prev && i > st.i && prev.k === prev.n - 1) save(PASSED, [...new Set([...(load(PASSED) ?? []), prev.key])]);
  st.i = i;
  st.back = back;
  save(KEY, st);
  if (i >= steps.length) {
    const home = st.ctx?.role ?? "recruiter"; // итог показываем в той роли, с которой тур начали
    return role !== home ? switchRole(home) : finish();
  }
  const s = steps[i];
  if ((s.role ?? "recruiter") !== role) return switchRole(s.role ?? "recruiter");
  clearInterval(tick);
  mount();
  ui.d.classList.add("busy");
  const go = typeof s.go === "function" ? s.go() : s.go;
  if (go) await hashTo(go);
  const el = await waitFor(s, g);
  if (g !== gen) return;
  if (!el && s.optional) return show(back ? i - 1 : i + 1);
  ui.d.classList.remove("busy");
  const text = typeof s.text === "function" ? s.text() : s.text;
  const miss = el ? "" : `<p class="tour-miss">${esc(s.miss ?? "Этого элемента сейчас нет на экране — шаг показан без подсветки.")}</p>`;
  const last = i === steps.length - 1;
  const many = steps[steps.length - 1].si > 0;
  popup(
    `${many ? `Сценарий ${s.si + 1} из ${steps[steps.length - 1].si + 1} · ш` : "Ш"}аг ${s.k + 1} из ${s.n}\n${s.sc}`,
    s.title,
    `<p>${esc(text)}</p>${miss}`,
    `<button class="btn btn-ghost" data-t="prev" ${i ? "" : "disabled"}>‹ Назад</button><button class="btn btn-ghost" data-t="end">Завершить</button><button class="btn btn-primary" data-t="next">${last ? "Готово" : "Далее ›"}</button>`,
    (i + 1) / steps.length,
  );
  shown = i;
  if (auto) calm = Date.now() + 800;
  if (el) el.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
  place(el, hit(s));
  ui.pop.focus({ preventScroll: true });
  const was = s.done?.(); // уже сделано до показа шага — не перескакивать, ждать «Далее»
  tick = setInterval(() => {
    if (g !== gen) return clearInterval(tick);
    place(find(s.sel) ?? el, hit(s));
    if (!was && s.done?.()) {
      clearInterval(tick);
      setTimeout(() => g === gen && show(i + 1, true), 500); // дать увидеть результат действия
    }
  }, 250);
}

// итог после последнего шага: тур остановлен, сам заново не начинается
function finish() {
  save(KEY, null); // перезагрузка страницы итог не повторяет
  save(OFFERED, 1, localStorage);
  clearInterval(tick);
  mount();
  ui.d.classList.remove("busy");
  const list = [...new Set(steps.map((s) => s.sc))].map((t) => `<li>✓ ${esc(t)}</li>`).join("");
  popup("Готово", "Тур пройден", `<p>Пройдено сценариев: ${new Set(steps.map((s) => s.key)).size}.</p><ul class="tour-done">${list}</ul>`, `<button class="btn btn-ghost" data-t="end">Закрыть</button><button class="btn btn-primary" data-t="menu">К сценариям</button>`, 1);
  place(null);
  ui.pop.focus({ preventScroll: true });
}

// кнопки действуют от шага на экране: пока идёт переход (автопереход, загрузка, смена роли), повторное нажатие не перескакивает шаг
function stepBack() {
  if (shown > 0 && shown === st?.i && Date.now() >= calm) show(shown - 1);
}

async function next() {
  const i = shown;
  if (i < 0 || i !== st?.i || Date.now() < calm) return;
  shown = -1;
  const s = steps[i];
  const g = gen;
  if (s?.act && !s.done?.()) {
    const el = find(s.sel);
    if (el) {
      s.act(el);
      for (let k = 0; k < 15 && !s.done?.() && g === gen; k++) await sleep(200);
    }
  }
  if (g === gen) show(i + 1);
}

async function switchRole(r) {
  save(KEY, st);
  ui?.d.classList.add("busy");
  try {
    await api("/api/login", { role: r });
  } catch (e) {
    // тур уже закрыт (возврат роли из end) — окна нет, только сообщение
    if (!st) return note(`Тур закрыт, но вернуть роль не удалось (${e.message}). Выйдите и войдите заново.`);
    popup("Тур", "Не удалось сменить роль", `<p>${esc(e.message)}. Пропустите шаг или завершите тур.</p>`, `<button class="btn btn-ghost" data-t="end">Завершить</button><button class="btn btn-primary" data-t="next">Пропустить шаг</button>`);
    shown = st.i;
    ui.d.classList.remove("busy");
    place(null);
    ui.pop.focus({ preventScroll: true });
    return;
  }
  // приложение после смены пользователя перечитывает всё с нуля — так же, как при выходе
  history.replaceState(null, "", r === "manager" ? "#/candidates" : "#/triage");
  location.reload();
}

function stop() {
  gen++;
  clearInterval(tick);
  ui?.d.remove();
  ui = null;
}

function end() {
  steps[st?.i]?.leave?.();
  const home = st?.ctx?.role ?? "recruiter"; // вернуть ту роль, с которой тур начали
  const back = role !== home;
  st = null;
  save(KEY, null);
  stop();
  if (back) switchRole(home);
}

function start(keys) {
  st = { keys, i: 0, ctx: st.ctx };
  steps = stepsOf(keys, st.ctx);
  show(0);
}

async function open() {
  stop();
  mount();
  ui.d.classList.add("busy");
  st = { keys: [], i: -1, ctx: null };
  save(OFFERED, 1, localStorage); // тур уже открывали — приглашение больше не нужно
  popup("Как это работает", "Сценарии работы", "<p>Подбираем кандидатов для примеров…</p>", "");
  place(null);
  try {
    role = (await api("/api/me")).role;
    const demo = (await api("/api/login-options").catch(() => ({}))).demo === true;
    st.ctx = { ...(await collect()), demo, role };
  } catch (e) {
    popup("Как это работает", "Данные не загрузились", `<p>${esc(e.message)}</p>`, `<button class="btn btn-ghost" data-t="end">Закрыть</button><button class="btn btn-primary" data-t="menu">Ещё раз</button>`);
    ui.d.classList.remove("busy");
    place(null);
    return;
  }
  const any = SCENARIOS.some((s) => usable(s, st.ctx));
  const passed = load(PASSED) ?? [];
  popup(
    "Как это работает",
    "Сценарии работы",
    `<p>Выберите сценарий или пройдите все. Примеры — кандидаты из базы, подобранные под сценарий. «Весь тур» включает сценарии с пометкой «меняет данные стенда».</p>
     <div class="tour-sc">${SCENARIOS.map((s, k) => {
       const ok = usable(s, st.ctx);
       const cut = ok && stepsOf([s.key], st.ctx).length < s.steps(st.ctx).length;
       const why = !need(s, st.ctx) ? ["нет такого кандидата в базе"] : !ok ? ["нужен демо-вход по ролям, а он отключён"] : [passed.includes(s.key) && "✓ пройден", cut && "без шагов другой роли: демо-вход отключён", s.writes && "меняет данные стенда"].filter(Boolean);
       return `<button data-sc="${s.key}" ${ok ? "" : "disabled"}><b>${k + 1}. ${esc(s.title)}</b>${why.length ? `<small>${why.join(" · ")}</small>` : ""}</button>`;
     }).join("")}</div>`,
    `<button class="btn btn-ghost" data-t="end">Закрыть</button><button class="btn btn-primary" data-sc="*" ${any ? "" : "disabled"}>Весь тур ›</button>`,
  );
  ui.d.classList.remove("busy");
  place(null);
  ui.pop.focus({ preventScroll: true });
}

/* ---------- вход в тур ---------- */

async function signedIn(ms = 8000) {
  for (const t0 = Date.now(); Date.now() - t0 < ms; await sleep(100)) if ($("#mename")?.textContent) return true;
  return false;
}

// плашка в углу: приглашение в тур (ask) или сообщение
function note(text, ask) {
  const d = document.createElement("div");
  d.className = "tour-inv";
  d.setAttribute("role", "status");
  d.innerHTML = `<span>${esc(text)}</span>${ask ? `<button class="btn btn-primary" data-i="1">Показать</button>` : ""}<button class="btn btn-ghost" data-i="">${ask ? "Не сейчас" : "Закрыть"}</button>`;
  d.addEventListener("click", (e) => {
    const b = e.target.closest("[data-i]");
    if (!b) return;
    d.remove();
    if (b.dataset.i) open();
  });
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 15000);
}

document.addEventListener(
  "click",
  (e) => {
    if (e.target.closest("[data-tour-go]")) {
      save(OFFERED, 1, localStorage);
      $('[data-login="recruiter"]')?.click();
      signedIn().then((ok) => ok && open());
    } else if (e.target.closest("[data-tour-open]")) open();
    else if (e.target.closest('[data-login="recruiter"]') && !load(OFFERED, localStorage)) {
      save(OFFERED, 1, localStorage);
      signedIn().then((ok) => ok && note("Показать, как это работает?", true));
    }
  },
  true,
);

const FOC = "button:not([disabled]),a[href],input,select,textarea";

// aria-modal: Tab ходит по кнопкам подсказки и, если шаг кликабельный, по подсвеченному элементу
function tabbable() {
  const s = st?.i >= 0 ? steps[st.i] : null;
  const t = hit(s) ? find(s.sel) : null;
  return [...ui.pop.querySelectorAll(FOC), ...(t ? [t, ...t.querySelectorAll(FOC)].filter((x) => x.matches(FOC)) : [])];
}

// capture на window — раньше обработчиков app.js: под туром до приложения доходит только ввод в полях
addEventListener(
  "keydown",
  (e) => {
    if (!ui) return;
    if (e.key === "Escape") end();
    else if (e.key === "Tab") {
      const f = tabbable();
      const k = f.indexOf(document.activeElement);
      const d = e.shiftKey ? -1 : 1;
      if (!f.length) ui.pop.focus();
      else f[k < 0 ? (d > 0 ? 0 : f.length - 1) : (k + d + f.length) % f.length].focus();
    } else if (e.target.closest?.("input,textarea,select")) return;
    else if (st?.i >= 0 && e.key === "ArrowRight") next();
    else if (st?.i >= 0 && e.key === "ArrowLeft") stepBack();
    else return e.stopImmediatePropagation(); // 1–4, Enter и прочее не уходят в «Разбор»; Enter на кнопке подсказки работает — действие по умолчанию не отменено
    e.preventDefault();
    e.stopImmediatePropagation();
  },
  true,
);

const replace = () => ui && st?.i >= 0 && place(find(steps[st.i]?.sel), hit(steps[st.i]));
addEventListener("resize", replace);
addEventListener("scroll", replace, { capture: true, passive: true });

// продолжение после перезагрузки (смена роли)
const saved = load(KEY);
if (saved?.ctx && saved.i >= 0) {
  signedIn().then(async (ok) => {
    if (!ok) return save(KEY, null);
    role = (await api("/api/me").catch(() => ({}))).role ?? "recruiter";
    st = saved;
    steps = stepsOf(st.keys, st.ctx);
    show(st.i);
  });
} else {
  signedIn(30000).then(async (ok) => ok && (role = (await api("/api/me").catch(() => ({}))).role ?? "recruiter"));
}
