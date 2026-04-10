// --- Текущее состояние на фронте ---
let serverState = null;

// --- Получение DOM-элементов ---
const pipelineEl = document.getElementById("pipeline");
const stageTitleEl = document.getElementById("stageTitle");
const stageDescriptionEl = document.getElementById("stageDescription");
const stageStateLabelEl = document.getElementById("stageStateLabel");
const releaseMetaEl = document.getElementById("releaseMeta");
const filesCountEl = document.getElementById("filesCount");

const globalStatusEl = document.getElementById("globalStatus");
const releaseNameEl = document.getElementById("releaseName");
const currentStageValueEl = document.getElementById("currentStageValue");
const modeValueEl = document.getElementById("modeValue");
const runValueEl = document.getElementById("runValue");
const artifactValueEl = document.getElementById("artifactValue");
const logBoxEl = document.getElementById("logBox");

const releaseInputEl = document.getElementById("releaseInput");
const uploadMetaEl = document.getElementById("uploadMeta");
const badBuildEl = document.getElementById("badBuild");
const badTestsEl = document.getElementById("badTests");
const badDeployEl = document.getElementById("badDeploy");
const badProdEl = document.getElementById("badProd");

const btnUpload = document.getElementById("btnUpload");
const btnApplyFlags = document.getElementById("btnApplyFlags");
const btnStart = document.getElementById("btnStart");
const btnStop = document.getElementById("btnStop");
const btnNext = document.getElementById("btnNext");
const btnPrev = document.getElementById("btnPrev");
const btnReset = document.getElementById("btnReset");

// --- Универсальный запрос к API ---
async function api(url, method = "GET", body = null, isForm = false) {
  const options = { method };

  if (body) {
    if (isForm) {
      options.body = body;
    } else {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
  }

  const response = await fetch(url, options);
  const data = await response.json();

  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "Ошибка запроса");
  }

  return data;
}

// --- Текстовое представление состояния этапа ---
function mapStageState(state) {
  const map = {
    idle: "Ожидание",
    running: "Выполняется",
    success: "Успешно",
    failed: "Ошибка"
  };
  return map[state] || "—";
}

// --- Статус верхнего бейджа ---
function computeGlobalStatus(state) {
  const failed = state.stage_states.includes("failed");
  const allSuccess = state.stage_states.every(item => item === "success");

  if (state.running) {
    return { text: "Выполнение", cls: "badge warning" };
  }

  if (failed) {
    return { text: "Ошибка релиза", cls: "badge error" };
  }

  if (allSuccess) {
    return { text: "Релиз доставлен", cls: "badge success" };
  }

  return { text: "Ожидание", cls: "badge" };
}

// --- Отрисовка пайплайна ---
function renderPipeline(state) {
  pipelineEl.innerHTML = "";

  state.stages.forEach((stage, index) => {
    const stageState = state.stage_states[index];
    const selected = index === state.selected_stage_index;

    const card = document.createElement("div");
    card.className = `stage ${stageState} ${selected ? "selected" : ""}`;

    card.innerHTML = `
      <div class="stage-head">
        <div class="stage-title">${stage.title}</div>
        <div class="stage-dot"></div>
      </div>
      <div class="stage-desc">${stage.description}</div>
    `;

    card.addEventListener("click", async () => {
      try {
        await api("/api/select-stage", "POST", { index });
        await refreshState();
      } catch (err) {
        alert(err.message);
      }
    });

    pipelineEl.appendChild(card);
  });
}

// --- Отрисовка описания выбранного этапа ---
function renderStageInfo(state) {
  const selectedIndex = state.selected_stage_index;
  const stage = state.stages[selectedIndex];

  stageTitleEl.textContent = stage.title;
  stageDescriptionEl.textContent = stage.description;
  stageStateLabelEl.textContent = mapStageState(state.stage_states[selectedIndex]);

  if (state.release && state.release.name) {
    releaseMetaEl.textContent = `${state.release.name} (${state.release.upload_name || "без имени"})`;
    filesCountEl.textContent = String(state.release.files_count || 0);
  } else {
    releaseMetaEl.textContent = "Релиз ещё не загружен";
    filesCountEl.textContent = "0";
  }
}

// --- Отрисовка сводки справа ---
function renderSummary(state) {
  const currentIndex = state.current_stage_index;
  const currentStage = currentIndex >= 0 ? state.stages[currentIndex].title : "—";

  const globalStatus = computeGlobalStatus(state);
  globalStatusEl.className = globalStatus.cls;
  globalStatusEl.textContent = globalStatus.text;

  releaseNameEl.textContent = state.release && state.release.name
    ? `Релиз: ${state.release.name}`
    : "Релиз не загружен";

  currentStageValueEl.textContent = currentStage;
  modeValueEl.textContent = state.auto_mode ? "Авто" : "Ручной";
  runValueEl.textContent = state.running ? "Идёт выполнение" : "Остановлен";

  artifactValueEl.textContent = state.release && state.release.artifact_path
    ? state.release.artifact_path.split("/").pop()
    : "—";

  uploadMetaEl.textContent = state.release && state.release.upload_name
    ? `Загружен архив: ${state.release.upload_name}`
    : "Архив ещё не загружен.";

  btnStop.disabled = !state.running;
}

// --- Отрисовка логов ---
function renderLogs(state) {
  const previousScrollHeight = logBoxEl.scrollHeight;
  const previousScrollTop = logBoxEl.scrollTop;
  const nearBottom = previousScrollHeight - previousScrollTop - logBoxEl.clientHeight < 60;

  logBoxEl.innerHTML = "";

  state.logs.forEach(item => {
    const row = document.createElement("div");
    row.className = "log-entry";

    row.innerHTML = `
      <span class="log-time">[${item.time}]</span>
      <span class="log-${item.level}">${item.message}</span>
    `;

    logBoxEl.appendChild(row);
  });

  if (nearBottom) {
    logBoxEl.scrollTop = logBoxEl.scrollHeight;
  }
}

// --- Полная отрисовка интерфейса ---
function renderAll(state) {
  renderPipeline(state);
  renderStageInfo(state);
  renderSummary(state);
  renderLogs(state);

  badBuildEl.checked = !!state.bad_flags.bad_build;
  badTestsEl.checked = !!state.bad_flags.bad_tests;
  badDeployEl.checked = !!state.bad_flags.bad_deploy;
  badProdEl.checked = !!state.bad_flags.bad_prod;
}

// --- Обновление состояния с сервера ---
async function refreshState() {
  const result = await api("/api/state");
  serverState = result.state;
  renderAll(serverState);
}

// --- Загрузка релиза ---
btnUpload.addEventListener("click", async () => {
  try {
    const file = releaseInputEl.files[0];
    if (!file) {
      alert("Выбери zip-архив.");
      return;
    }

    const formData = new FormData();
    formData.append("release", file);

    await api("/api/upload", "POST", formData, true);
    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Применение плохих сценариев ---
btnApplyFlags.addEventListener("click", async () => {
  try {
    await api("/api/config", "POST", {
      bad_build: badBuildEl.checked,
      bad_tests: badTestsEl.checked,
      bad_deploy: badDeployEl.checked,
      bad_prod: badProdEl.checked
    });

    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Кнопка Пуск ---
btnStart.addEventListener("click", async () => {
  try {
    await api("/api/start", "POST");
    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Кнопка Стоп ---
btnStop.addEventListener("click", async () => {
  try {
    await api("/api/stop", "POST");
    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Кнопка Вперёд ---
btnNext.addEventListener("click", async () => {
  try {
    await api("/api/next", "POST");
    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Кнопка Назад ---
btnPrev.addEventListener("click", async () => {
  try {
    await api("/api/prev", "POST");
    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Кнопка Сброс ---
btnReset.addEventListener("click", async () => {
  try {
    await api("/api/reset", "POST");
    await refreshState();
  } catch (err) {
    alert(err.message);
  }
});

// --- Таймер автообновления интерфейса ---
setInterval(() => {
  refreshState().catch(() => {});
}, 1200);

// --- Первый запуск ---
refreshState().catch(() => {});
