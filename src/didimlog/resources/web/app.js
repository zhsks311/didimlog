"use strict";

const state = {
  library: null,
  health: null,
  selectedLesson: null,
};

const views = {
  loading: document.querySelector("#loading"),
  error: document.querySelector("#error-view"),
  bookshelf: document.querySelector("#bookshelf-view"),
  reader: document.querySelector("#reader-view"),
  lessons: document.querySelector("#lessons-view"),
};

function element(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(name, String(value));
    }
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function clear(node) {
  node.replaceChildren();
}

function show(name) {
  for (const [key, view] of Object.entries(views)) view.hidden = key !== name;
  document.querySelector("#content").focus({ preventScroll: true });
}

function setNavigation(current) {
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

async function api(path) {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    payload = { error: { token: "GUI_RESPONSE_INVALID", message: "로컬 GUI 응답을 읽지 못했습니다." } };
  }
  if (!response.ok) {
    const error = new Error(payload.error?.message || "로컬 GUI 요청이 실패했습니다.");
    error.payload = payload.error || { token: "GUI_REQUEST_FAILED" };
    throw error;
  }
  return payload;
}

function showError(error) {
  const payload = error.payload || {};
  clear(views.error);
  views.error.append(
    element("p", { className: "eyebrow", text: "LOCAL READ ERROR" }),
    element("h1", { text: "자료를 안전하게 열지 못했습니다" }),
    element("p", { text: payload.message || error.message || "알 수 없는 오류입니다." }),
    element("p", { className: "machine", text: payload.token || "GUI_REQUEST_FAILED" }),
  );
  if (payload.logical_path) {
    views.error.append(element("p", { className: "logical-path", text: payload.logical_path }));
  }
  if (payload.reason) views.error.append(element("p", { text: payload.reason }));
  show("error");
}

function tokenSurface(label, surface) {
  const section = element("div", { className: `health-surface ${surface.state}` });
  section.append(
    element("span", { text: label }),
    element("strong", { text: surface.token }),
  );
  return section;
}

function renderHealth(health) {
  state.health = health;
  const content = document.querySelector("#health-content");
  clear(content);
  content.append(
    tokenSurface("개인 지식 index", health.personal_index),
    tokenSurface("프로젝트 근거 index", health.project.index),
    tokenSurface("Claude 연결", health.claude),
  );
  const projectName = health.project.name || "현재 프로젝트 없음";
  content.prepend(
    element("div", { className: "health-surface" }, [
      element("span", { text: "환경" }),
      element("strong", { text: `Didimlog ${health.version} · ${projectName}` }),
    ]),
  );
  for (const issue of health.issues) {
    content.append(
      element("article", { className: "issue" }, [
        element("strong", { text: issue.token }),
        element("p", { text: issue.impact }),
        element("p", { text: `다음 행동: ${issue.action}` }),
      ]),
    );
  }
  if (!health.issues.length) {
    content.append(element("p", { className: "readonly-note", text: "진단된 문제가 없습니다." }));
  }

  const indicesCurrent = health.personal_index.current && (
    health.project.index.current || health.project.index.state === "unconfigured"
  );
  const allHealthy = indicesCurrent && health.claude.state !== "problem";
  const button = document.querySelector("#health-button");
  button.classList.toggle("is-current", allHealthy);
  button.classList.toggle("is-warning", !allHealthy);
  document.querySelector("#health-label").textContent = allHealthy
    ? "상태 정상"
    : (
      !indicesCurrent
        ? (
          health.personal_index.current
            ? health.project.index.token
            : health.personal_index.token
        )
        : health.claude.token
    );
}


function freshnessNotice(health) {
  const current = health.personal_index.current;
  const notice = element("div", { className: `index-notice${current ? "" : " warning"}` });
  notice.append(
    element("strong", {
      text: current
        ? "원문과 개인 index가 일치합니다"
        : "원문을 직접 검증해 표시했지만 retrieval index는 최신이 아닙니다",
    }),
    element("span", { className: "machine", text: health.personal_index.token }),
  );
  return notice;
}

function renderBookshelf() {
  setNavigation("bookshelf");
  const root = views.bookshelf;
  clear(root);
  const health = state.library.health;
  root.append(
    element("div", { className: "page-heading" }, [
      element("div", {}, [
        element("p", { className: "eyebrow", text: "EDITORIAL BOOKSHELF" }),
        element("h1", { text: "책장" }),
        element("p", { className: "lede", text: "검증한 로컬 지식을 scope별로 읽습니다. 책을 여는 동안 원문·index·설정은 바뀌지 않습니다." }),
      ]),
      freshnessNotice(health),
    ]),
  );

  let totalBooks = 0;
  for (const scope of state.library.scopes) {
    totalBooks += scope.book_count;
    const section = element("section", { className: "scope-section" });
    section.append(
      element("div", { className: "scope-header" }, [
        element("h2", { text: scope.scope }),
        element("span", { className: "count", text: `책 ${scope.book_count} · 교훈 ${scope.lesson_count}` }),
      ]),
    );
    if (!scope.books.length) {
      section.append(
        element("p", {
          className: "empty-state",
          text: scope.lesson_count
            ? "이 scope에는 교훈이 있지만 아직 기존 책이 없습니다. Milestone A에서는 책을 만들거나 갱신하지 않습니다."
            : "이 scope에는 읽을 기존 책이 없습니다.",
        }),
      );
    } else {
      const grid = element("div", { className: "book-grid" });
      for (const book of scope.books) {
        const card = element("button", {
          className: "book-card",
          attrs: { type: "button", "data-book-id": book.id },
        }, [
          element("span", { className: "book-scope", text: `${book.scope} · BOOK` }),
          element("h3", { text: book.title }),
          element("p", { className: "find-when", text: `찾을 때 · ${book.find_when.join(" · ")}` }),
          element("span", { className: "logical-path", text: book.logical_path }),
        ]);
        card.addEventListener("click", () => { window.location.hash = `#/books/${book.id}`; });
        grid.append(card);
      }
      section.append(grid);
    }
    root.append(section);
  }
  if (!state.library.scopes.length || !totalBooks) {
    root.append(
      element("p", {
        className: "empty-state",
        text: state.library.scopes.length
          ? "현재 검증된 source snapshot에 기존 책이 없습니다."
          : "아직 표시할 개인 지식 scope가 없습니다.",
      }),
    );
  }
  show("bookshelf");
}

async function renderReader(identifier) {
  setNavigation(null);
  const root = views.reader;
  clear(root);
  show("loading");
  const book = await api(`/api/v1/books/${encodeURIComponent(identifier)}`);
  const back = element("button", { className: "text-button", attrs: { type: "button" }, text: "← 책장으로" });
  back.addEventListener("click", () => { window.location.hash = "#/bookshelf"; });
  const lessonsLink = element("button", { className: "text-button", attrs: { type: "button" }, text: "이 scope의 교훈 보기 →" });
  lessonsLink.addEventListener("click", () => {
    window.location.hash = `#/lessons?scope=${encodeURIComponent(book.scope)}`;
  });
  root.append(
    element("div", { className: "reader-top" }, [back, lessonsLink]),
    element("div", { className: "reader-title" }, [
      element("p", { className: "eyebrow", text: `${book.scope} · CANONICAL BOOK` }),
      element("h1", { text: book.title }),
      element("p", { className: "lede", text: `찾을 때 · ${book.find_when.join(" · ")}` }),
    ]),
  );

  const toc = element("nav", { className: "toc", attrs: { "aria-label": "책 목차" } });
  toc.append(element("h2", { text: "목차" }));
  for (const heading of book.headings) {
    const item = element("button", {
      text: heading.text,
      attrs: { type: "button", "data-level": heading.level },
    });
    item.addEventListener("click", () => {
      const target = [...article.querySelectorAll("[id]")]
        .find((node) => node.id === heading.anchor);
      target?.scrollIntoView({ block: "start" });
    });
    toc.append(item);
  }
  if (!book.headings.length) toc.append(element("span", { text: "본문 제목 없음" }));

  const article = element("article", { className: "book-body" });
  // body_html is produced only by the existing server-side safe book renderer.
  article.innerHTML = book.body_html;
  for (const link of article.querySelectorAll("a[href]")) {
    if (/^(https?:|mailto:)/i.test(link.getAttribute("href"))) {
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
    }
  }

  const rail = element("aside", { className: "source-rail" }, [
    element("h2", { text: "SOURCE" }),
    element("dl", {}, [
      element("dt", { text: "원문" }),
      element("dd", { className: "logical-path", text: book.logical_path }),
      element("dt", { text: "표시 방식" }),
      element("dd", { text: "메모리에서 만든 재생성 가능한 HTML view" }),
      element("dt", { text: "Source of truth" }),
      element("dd", { text: "canonical Markdown" }),
    ]),
  ]);
  root.append(element("div", { className: "reader-grid" }, [toc, article, rail]));
  show("reader");

  if (window.mermaid && article.querySelector(".mermaid")) {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });
    try { await window.mermaid.run({ nodes: article.querySelectorAll(".mermaid") }); } catch (_) { /* Keep escaped source visible. */ }
  }
}

function allLessons() {
  return state.library.scopes.flatMap((scope) => scope.lessons);
}

function option(value, label = value) {
  return element("option", { text: label, attrs: { value } });
}

function unique(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, "ko"));
}

function filterControl(label, id, values, allLabel) {
  const select = element("select", { attrs: { id } });
  select.append(option("", allLabel));
  for (const value of values) select.append(option(value));
  return element("label", { className: "filter" }, [
    element("span", { text: label }),
    select,
  ]);
}

function dateControl(label, id) {
  return element("label", { className: "filter" }, [
    element("span", { text: label }),
    element("input", { attrs: { id, type: "date" } }),
  ]);
}

function selectedScopeFromHash() {
  const hash = window.location.hash;
  const query = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
  return new URLSearchParams(query).get("scope") || "";
}

function filteredLessons() {
  const lessons = allLessons();
  const scope = document.querySelector("#filter-scope")?.value || "";
  const topic = document.querySelector("#filter-topic")?.value || "";
  const tag = document.querySelector("#filter-tag")?.value || "";
  const booked = document.querySelector("#filter-booked")?.value || "";
  const date = document.querySelector("#filter-date")?.value || "";
  const review = document.querySelector("#filter-review")?.value || "";
  return lessons.filter((lesson) => (
    (!scope || lesson.scope === scope)
    && (!topic || lesson.topic === topic)
    && (!tag || lesson.tags.includes(tag))
    && (!booked || lesson.booked_state === booked)
    && (!date || lesson.date === date)
    && (!review || lesson.review_by === review)
  ));
}

function renderLessonList() {
  const list = document.querySelector("#lesson-list");
  clear(list);
  const lessons = filteredLessons();
  list.append(element("div", { className: "lesson-result-count", text: `${lessons.length}개 교훈 · metadata exact filter` }));
  for (const lesson of lessons) {
    const row = element("button", {
      className: "lesson-row",
      attrs: {
        type: "button",
        "data-lesson-id": lesson.id,
        "aria-current": state.selectedLesson === lesson.id ? "true" : "false",
      },
    }, [
      element("strong", { text: lesson.title }),
      element("p", { text: lesson.summary }),
      element("span", { className: "lesson-meta", text: `${lesson.scope} · ${lesson.topic} · ${lesson.date} · ${lesson.booked_state}` }),
    ]);
    row.addEventListener("click", () => selectLesson(lesson.id));
    list.append(row);
  }
  if (!lessons.length) list.append(element("p", { className: "empty-state", text: "선택한 metadata와 정확히 일치하는 교훈이 없습니다." }));
}

async function selectLesson(identifier) {
  state.selectedLesson = identifier;
  renderLessonList();
  const detail = document.querySelector("#lesson-detail");
  clear(detail);
  detail.append(element("p", { className: "loading", text: "교훈 원문을 확인하고 있습니다." }));
  try {
    const lesson = await api(`/api/v1/lessons/${encodeURIComponent(identifier)}`);
    clear(detail);
    detail.append(
      element("p", { className: "eyebrow", text: `${lesson.scope} · LESSON` }),
      element("h2", { text: lesson.title }),
      element("p", { className: "summary", text: lesson.summary }),
      element("div", { className: "lesson-metadata" }, [
        element("span", { text: `topic · ${lesson.topic}` }),
        element("span", { text: `date · ${lesson.date}` }),
        element("span", { text: `tags · ${lesson.tags.join(", ") || "없음"}` }),
        element("span", { text: `booked · ${lesson.booked_state}` }),
        lesson.review_by ? element("span", { text: `review_by · ${lesson.review_by}` }) : null,
      ]),
      element("p", { className: "logical-path", text: lesson.logical_path }),
      element("pre", { className: "markdown-source", text: lesson.markdown }),
      element("p", { className: "readonly-note", text: "canonical Markdown 원문을 읽기 전용으로 표시합니다. booked는 이 lesson의 topic-level 표식이며 특정 책 문장의 provenance가 아닙니다." }),
    );
  } catch (error) {
    clear(detail);
    detail.append(element("p", { className: "error-view", text: error.payload?.message || error.message }));
  }
}

async function renderLessons() {
  setNavigation("lessons");
  const root = views.lessons;
  clear(root);
  const lessons = allLessons();
  const filters = element("div", { className: "filters" }, [
    filterControl("SCOPE", "filter-scope", unique(lessons.map((item) => item.scope)), "모든 scope"),
    filterControl("TOPIC", "filter-topic", unique(lessons.map((item) => item.topic)), "모든 topic"),
    filterControl("TAG", "filter-tag", unique(lessons.flatMap((item) => item.tags)), "모든 tag"),
    filterControl("BOOKED", "filter-booked", ["booked", "unbooked"], "모든 상태"),
    dateControl("DATE", "filter-date"),
    dateControl("REVIEW DATE", "filter-review"),
  ]);
  root.append(
    element("div", { className: "lesson-heading" }, [
      element("p", { className: "eyebrow", text: "CANONICAL LESSONS" }),
      element("h1", { text: "교훈" }),
      element("p", { className: "lede", text: "scope, topic, tag, 날짜, 검토일, topic-level booked 상태로 정확히 좁혀 원문을 읽습니다." }),
    ]),
    filters,
    element("div", { className: "lesson-layout" }, [
      element("div", {
        className: "lesson-list",
        attrs: { id: "lesson-list" },
      }),
      element("article", {
        className: "lesson-detail",
        attrs: { id: "lesson-detail" },
      }, [
        element("p", { className: "empty-state", text: "왼쪽 목록에서 읽을 교훈을 선택하세요." }),
      ]),
    ]),
  );
  const requestedScope = selectedScopeFromHash();
  const scopeSelect = document.querySelector("#filter-scope");
  if (
    requestedScope
    && [...scopeSelect.options].some((item) => item.value === requestedScope)
  ) {
    scopeSelect.value = requestedScope;
  }
  for (const control of filters.querySelectorAll("select, input")) {
    control.addEventListener("change", renderLessonList);
  }
  renderLessonList();
  show("lessons");
  if (
    state.selectedLesson
    && filteredLessons().some((lesson) => lesson.id === state.selectedLesson)
  ) {
    await selectLesson(state.selectedLesson);
  } else {
    state.selectedLesson = null;
  }
}

async function route() {
  try {
    if (!state.library) {
      show("loading");
      state.library = await api("/api/v1/library");
      renderHealth(state.library.health);
    }
    const hash = window.location.hash || "#/bookshelf";
    const bookMatch = /^#\/books\/([0-9a-f]{64})$/.exec(hash);
    if (bookMatch) {
      await renderReader(bookMatch[1]);
      return;
    }
    if (hash.startsWith("#/lessons")) {
      await renderLessons();
      return;
    }
    renderBookshelf();
  } catch (error) {
    showError(error);
  }
}

const healthPanel = document.querySelector("#health-panel");
const healthButton = document.querySelector("#health-button");
healthButton.addEventListener("click", () => {
  const open = healthPanel.hidden;
  healthPanel.hidden = !open;
  healthButton.setAttribute("aria-expanded", String(open));
});
document.querySelector("#health-close").addEventListener("click", () => {
  healthPanel.hidden = true;
  healthButton.setAttribute("aria-expanded", "false");
  healthButton.focus();
});
document.querySelector("#health-refresh").addEventListener("click", async () => {
  try {
    state.library = await api("/api/v1/library");
    renderHealth(state.library.health);
    await route();
  } catch (error) {
    showError(error);
  }
});

window.addEventListener("hashchange", route);
route();
