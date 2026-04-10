import io
import os
import re
import json
import time
import uuid
import shutil
import zipfile
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, render_template, request, send_from_directory

# --- Инициализация приложения ---
app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static"
)

# --- Базовые директории ---
BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
WORKSPACE_DIR = DATA_DIR / "workspace"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
DEPLOY_DIR = DATA_DIR / "deploy"
CURRENT_DEPLOY_DIR = DEPLOY_DIR / "current"

# --- Создаём директории, если их нет ---
for path in [UPLOADS_DIR, WORKSPACE_DIR, ARTIFACTS_DIR, DEPLOY_DIR, CURRENT_DEPLOY_DIR]:
    path.mkdir(parents=True, exist_ok=True)

# --- Конфигурация этапов пайплайна ---
PIPELINE_STAGES = [
    {
        "key": "source",
        "title": "Source",
        "description": "Загрузка релиза, распаковка архива и первичная валидация структуры проекта."
    },
    {
        "key": "build",
        "title": "Build",
        "description": "Сборка артефактов, синтаксическая проверка Python-кода и подготовка сборки."
    },
    {
        "key": "test",
        "title": "Test",
        "description": "Запуск тестового сценария приложения. Релиз должен корректно пройти smoke-проверку."
    },
    {
        "key": "deploy",
        "title": "Deploy",
        "description": "Развёртывание артефакта в тестовый production-каталог и подготовка релиза."
    },
    {
        "key": "prod",
        "title": "PROD",
        "description": "Проверка уже развернутого релиза: smoke-запуск и имитация состояния production."
    }
]

# --- Глобальное состояние стенда ---
STATE = {
    "lock": threading.Lock(),
    "run_id": None,
    "running": False,
    "stop_requested": False,
    "auto_mode": False,
    "selected_stage_index": 0,
    "current_stage_index": -1,
    "stage_states": ["idle" for _ in PIPELINE_STAGES],
    "logs": [],
    "release": {
        "name": None,
        "upload_name": None,
        "saved_archive_path": None,
        "workspace_path": None,
        "artifact_path": None,
        "deployed_path": None,
        "files_count": 0,
    },
    "bad_flags": {
        "bad_build": False,
        "bad_tests": False,
        "bad_deploy": False,
        "bad_prod": False,
    }
}

# --- Имя фонового потока ---
THREAD_NAME = "cicd-auto-runner"


def now_str() -> str:
    # --- Текущее время для журнала ---
    return datetime.now().strftime("%H:%M:%S")


def add_log(message: str, level: str = "info") -> None:
    # --- Добавление записи в лог ---
    with STATE["lock"]:
        STATE["logs"].append({
            "time": now_str(),
            "level": level,
            "message": message
        })
        # --- Ограничиваем длину лога ---
        STATE["logs"] = STATE["logs"][-500:]


def reset_pipeline_runtime() -> None:
    # --- Сброс состояний этапов ---
    with STATE["lock"]:
        STATE["current_stage_index"] = -1
        STATE["stage_states"] = ["idle" for _ in PIPELINE_STAGES]
        STATE["running"] = False
        STATE["stop_requested"] = False
        STATE["auto_mode"] = False


def full_reset_state(keep_release: bool = True) -> None:
    # --- Полный сброс состояния стенда ---
    with STATE["lock"]:
        STATE["current_stage_index"] = -1
        STATE["selected_stage_index"] = 0
        STATE["stage_states"] = ["idle" for _ in PIPELINE_STAGES]
        STATE["running"] = False
        STATE["stop_requested"] = False
        STATE["auto_mode"] = False
        STATE["logs"] = []
        if not keep_release:
            STATE["release"] = {
                "name": None,
                "upload_name": None,
                "saved_archive_path": None,
                "workspace_path": None,
                "artifact_path": None,
                "deployed_path": None,
                "files_count": 0,
            }
            STATE["bad_flags"] = {
                "bad_build": False,
                "bad_tests": False,
                "bad_deploy": False,
                "bad_prod": False,
            }


def safe_delete_dir(path: Path) -> None:
    # --- Безопасное удаление каталога ---
    if path.exists() and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def safe_delete_file(path: Path) -> None:
    # --- Безопасное удаление файла ---
    if path.exists() and path.is_file():
        path.unlink(missing_ok=True)


def count_files(directory: Path) -> int:
    # --- Подсчёт файлов в каталоге ---
    return sum(1 for item in directory.rglob("*") if item.is_file())


def find_python_files(directory: Path):
    # --- Поиск всех Python-файлов ---
    return [p for p in directory.rglob("*.py") if p.is_file()]


def extract_zip_checked(archive_path: Path, target_dir: Path) -> None:
    # --- Распаковка zip-файла с базовой защитой от path traversal ---
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            member_path = target_dir / member.filename
            resolved_target = member_path.resolve()
            if not str(resolved_target).startswith(str(target_dir.resolve())):
                raise RuntimeError("Обнаружен небезопасный путь внутри архива.")
        zf.extractall(target_dir)


def run_cmd(cmd, cwd: Path, timeout: int = 20, extra_env: dict | None = None):
    # --- Выполнение внешней команды ---
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env
    )
    return result


def set_stage_state(index: int, value: str) -> None:
    # --- Установка состояния конкретного этапа ---
    with STATE["lock"]:
        STATE["stage_states"][index] = value


def mark_previous_as_success(index: int) -> None:
    # --- Все предыдущие этапы считаем успешными ---
    with STATE["lock"]:
        for i in range(index):
            if STATE["stage_states"][i] != "failed":
                STATE["stage_states"][i] = "success"


def set_selected_stage(index: int) -> None:
    # --- Выбранный в UI этап ---
    with STATE["lock"]:
        STATE["selected_stage_index"] = max(0, min(index, len(PIPELINE_STAGES) - 1))


def get_state_snapshot():
    # --- Снимок состояния для отдачи в UI ---
    with STATE["lock"]:
        return {
            "run_id": STATE["run_id"],
            "running": STATE["running"],
            "stop_requested": STATE["stop_requested"],
            "auto_mode": STATE["auto_mode"],
            "selected_stage_index": STATE["selected_stage_index"],
            "current_stage_index": STATE["current_stage_index"],
            "stage_states": list(STATE["stage_states"]),
            "logs": list(STATE["logs"]),
            "release": dict(STATE["release"]),
            "bad_flags": dict(STATE["bad_flags"]),
            "stages": PIPELINE_STAGES
        }


def fail_stage(index: int, message: str) -> bool:
    # --- Унифицированный перевод этапа в ошибку ---
    set_stage_state(index, "failed")
    add_log(message, "error")
    with STATE["lock"]:
        STATE["running"] = False
        STATE["auto_mode"] = False
    return False


def check_stop_requested() -> bool:
    # --- Проверка запроса на остановку ---
    with STATE["lock"]:
        return STATE["stop_requested"]


def stage_source(index: int) -> bool:
    # --- Этап Source ---
    snapshot = get_state_snapshot()
    archive_path_str = snapshot["release"]["saved_archive_path"]

    if not archive_path_str:
        return fail_stage(index, "Релиз не загружен. Сначала выбери zip-архив релиза.")

    archive_path = Path(archive_path_str)

    if not archive_path.exists():
        return fail_stage(index, "Архив релиза не найден на диске.")

    run_id = snapshot["run_id"]
    workspace = WORKSPACE_DIR / run_id

    # --- Очищаем рабочую директорию текущего прогона ---
    safe_delete_dir(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    add_log("Source: распаковка архива релиза.", "info")

    try:
        extract_zip_checked(archive_path, workspace)
    except zipfile.BadZipFile:
        return fail_stage(index, "Source: архив повреждён или не является корректным zip-файлом.")
    except Exception as exc:
        return fail_stage(index, f"Source: ошибка распаковки архива: {exc}")

    files_total = count_files(workspace)
    python_files = find_python_files(workspace)

    if files_total == 0:
        return fail_stage(index, "Source: архив пустой. Нечего обрабатывать.")

    main_py = workspace / "main.py"
    if not main_py.exists():
        return fail_stage(index, "Source: в релизе отсутствует файл main.py.")

    with STATE["lock"]:
        STATE["release"]["workspace_path"] = str(workspace)
        STATE["release"]["files_count"] = files_total

    add_log(f"Source: распаковано файлов: {files_total}.", "success")
    add_log(f"Source: найдено Python-файлов: {len(python_files)}.", "info")
    add_log("Source: структура релиза валидна.", "success")
    return True


def stage_build(index: int) -> bool:
    # --- Этап Build ---
    snapshot = get_state_snapshot()
    workspace_path = snapshot["release"]["workspace_path"]

    if not workspace_path:
        return fail_stage(index, "Build: отсутствует рабочая директория релиза.")

    if snapshot["bad_flags"]["bad_build"]:
        return fail_stage(index, "Build: сценарий плохого релиза вызвал падение сборки.")

    workspace = Path(workspace_path)
    python_files = find_python_files(workspace)

    if not python_files:
        return fail_stage(index, "Build: в проекте нет Python-файлов для сборки.")

    add_log("Build: запуск синтаксической проверки Python-файлов.", "info")

    for py_file in python_files:
        if check_stop_requested():
            return fail_stage(index, "Build: выполнение остановлено пользователем.")

        result = run_cmd(
            ["python", "-m", "py_compile", str(py_file)],
            cwd=workspace,
            timeout=10
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return fail_stage(index, f"Build: ошибка компиляции файла {py_file.name}: {stderr}")

    add_log("Build: синтаксическая проверка пройдена.", "success")

    artifact_path = ARTIFACTS_DIR / f"{snapshot['run_id']}.zip"
    safe_delete_file(artifact_path)

    add_log("Build: упаковка артефакта сборки.", "info")

    with zipfile.ZipFile(artifact_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in workspace.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, arcname=str(file_path.relative_to(workspace)))

    with STATE["lock"]:
        STATE["release"]["artifact_path"] = str(artifact_path)

    add_log(f"Build: артефакт создан: {artifact_path.name}.", "success")
    return True


def stage_test(index: int) -> bool:
    # --- Этап Test ---
    snapshot = get_state_snapshot()
    workspace_path = snapshot["release"]["workspace_path"]

    if not workspace_path:
        return fail_stage(index, "Test: рабочая директория не определена.")

    if snapshot["bad_flags"]["bad_tests"]:
        return fail_stage(index, "Test: сценарий плохого релиза привёл к падению тестов.")

    workspace = Path(workspace_path)
    main_py = workspace / "main.py"

    if not main_py.exists():
        return fail_stage(index, "Test: файл main.py отсутствует.")

    add_log("Test: запуск smoke-проверки main.py.", "info")

    try:
        result = run_cmd(
            ["python", "main.py"],
            cwd=workspace,
            timeout=10,
            extra_env={"CI_TEST": "1"}
        )
    except subprocess.TimeoutExpired:
        return fail_stage(index, "Test: выполнение main.py превысило лимит времени.")
    except Exception as exc:
        return fail_stage(index, f"Test: ошибка запуска теста: {exc}")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if stdout:
        add_log(f"Test stdout: {stdout}", "info")
    if stderr:
        add_log(f"Test stderr: {stderr}", "warn")

    if result.returncode != 0:
        return fail_stage(index, f"Test: main.py завершился с кодом {result.returncode}.")

    add_log("Test: smoke-проверка успешно пройдена.", "success")
    return True


def stage_deploy(index: int) -> bool:
    # --- Этап Deploy ---
    snapshot = get_state_snapshot()
    artifact_path_str = snapshot["release"]["artifact_path"]
    workspace_path = snapshot["release"]["workspace_path"]

    if not artifact_path_str or not workspace_path:
        return fail_stage(index, "Deploy: отсутствуют артефакты для деплоя.")

    if snapshot["bad_flags"]["bad_deploy"]:
        return fail_stage(index, "Deploy: сценарий плохого релиза вызвал ошибку развёртывания.")

    run_id = snapshot["run_id"]
    deploy_target = DEPLOY_DIR / f"release-{run_id}"
    safe_delete_dir(deploy_target)
    deploy_target.mkdir(parents=True, exist_ok=True)

    add_log("Deploy: подготовка целевого production-каталога.", "info")

    try:
        shutil.copytree(Path(workspace_path), deploy_target, dirs_exist_ok=True)
    except Exception as exc:
        return fail_stage(index, f"Deploy: ошибка копирования файлов: {exc}")

    # --- Обновляем символическую текущую версию через замену содержимого ---
    safe_delete_dir(CURRENT_DEPLOY_DIR)
    CURRENT_DEPLOY_DIR.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copytree(deploy_target, CURRENT_DEPLOY_DIR, dirs_exist_ok=True)
    except Exception as exc:
        return fail_stage(index, f"Deploy: ошибка активации текущего релиза: {exc}")

    with STATE["lock"]:
        STATE["release"]["deployed_path"] = str(CURRENT_DEPLOY_DIR)

    add_log("Deploy: релиз успешно развернут в production-каталог.", "success")
    return True


def stage_prod(index: int) -> bool:
    # --- Этап PROD ---
    snapshot = get_state_snapshot()
    deployed_path_str = snapshot["release"]["deployed_path"]

    if not deployed_path_str:
        return fail_stage(index, "PROD: релиз ещё не развернут.")

    deployed_path = Path(deployed_path_str)
    main_py = deployed_path / "main.py"

    if not main_py.exists():
        return fail_stage(index, "PROD: в развернутом релизе отсутствует main.py.")

    add_log("PROD: запуск smoke-проверки развернутого приложения.", "info")

    if snapshot["bad_flags"]["bad_prod"]:
        return fail_stage(index, "PROD: приложение упало после деплоя. Прод-среда нестабильна.")

    try:
        result = run_cmd(
            ["python", "main.py"],
            cwd=deployed_path,
            timeout=10,
            extra_env={"PROD_CHECK": "1"}
        )
    except subprocess.TimeoutExpired:
        return fail_stage(index, "PROD: проверка production превысила лимит времени.")
    except Exception as exc:
        return fail_stage(index, f"PROD: ошибка запуска проверки: {exc}")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if stdout:
        add_log(f"PROD stdout: {stdout}", "info")
    if stderr:
        add_log(f"PROD stderr: {stderr}", "warn")

    if result.returncode != 0:
        return fail_stage(index, f"PROD: приложение завершилось с кодом {result.returncode}.")

    add_log("PROD: релиз стабилен и работает штатно.", "success")
    return True


STAGE_HANDLERS = {
    "source": stage_source,
    "build": stage_build,
    "test": stage_test,
    "deploy": stage_deploy,
    "prod": stage_prod,
}


def run_stage(stage_index: int) -> bool:
    # --- Выполнение одного этапа ---
    if stage_index < 0 or stage_index >= len(PIPELINE_STAGES):
        add_log("Попытка запуска несуществующего этапа.", "error")
        return False

    if check_stop_requested():
        return fail_stage(stage_index, "Выполнение остановлено пользователем.")

    stage = PIPELINE_STAGES[stage_index]

    with STATE["lock"]:
        STATE["current_stage_index"] = stage_index
        STATE["selected_stage_index"] = stage_index

    mark_previous_as_success(stage_index)
    set_stage_state(stage_index, "running")

    add_log(f"Начат этап {stage['title']}.", "info")

    handler = STAGE_HANDLERS[stage["key"]]
    ok = handler(stage_index)

    if ok:
        set_stage_state(stage_index, "success")
        add_log(f"Этап {stage['title']} завершён успешно.", "success")

    return ok


def auto_runner():
    # --- Автоматический последовательный прогон всех этапов ---
    try:
        for index in range(len(PIPELINE_STAGES)):
            if check_stop_requested():
                add_log("Автопрогон остановлен пользователем.", "warn")
                break

            ok = run_stage(index)
            if not ok:
                break

            time.sleep(1.0)

        with STATE["lock"]:
            STATE["running"] = False
            STATE["auto_mode"] = False
            STATE["stop_requested"] = False

        add_log("Автопрогон завершён.", "info")
    except Exception as exc:
        add_log(f"Критическая ошибка автопрогона: {exc}", "error")
        with STATE["lock"]:
            STATE["running"] = False
            STATE["auto_mode"] = False
            STATE["stop_requested"] = False


@app.route("/")
def index():
    # --- Главная страница ---
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    # --- Загрузка zip-релиза ---
    uploaded = request.files.get("release")
    if not uploaded:
        return jsonify({"ok": False, "error": "Файл релиза не передан."}), 400

    filename = uploaded.filename or ""
    if not filename.lower().endswith(".zip"):
        return jsonify({"ok": False, "error": "Поддерживается только zip-архив."}), 400

    run_id = uuid.uuid4().hex[:12]
    saved_name = f"{run_id}_{re.sub(r'[^a-zA-Z0-9._-]+', '_', filename)}"
    save_path = UPLOADS_DIR / saved_name

    uploaded.save(save_path)

    with STATE["lock"]:
        STATE["run_id"] = run_id
        STATE["release"]["name"] = f"release-{run_id}"
        STATE["release"]["upload_name"] = filename
        STATE["release"]["saved_archive_path"] = str(save_path)
        STATE["release"]["workspace_path"] = None
        STATE["release"]["artifact_path"] = None
        STATE["release"]["deployed_path"] = None
        STATE["release"]["files_count"] = 0

    reset_pipeline_runtime()
    add_log(f"Загружен релиз: {filename}.", "success")
    add_log(f"Создан идентификатор прогона: {run_id}.", "info")

    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/config", methods=["POST"])
def api_config():
    # --- Применение флагов плохого релиза ---
    data = request.get_json(silent=True) or {}

    with STATE["lock"]:
        for key in ["bad_build", "bad_tests", "bad_deploy", "bad_prod"]:
            if key in data:
                STATE["bad_flags"][key] = bool(data[key])

    add_log("Обновлены параметры плохого релиза.", "info")
    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    # --- Запуск полного автопрогона ---
    snapshot = get_state_snapshot()
    if not snapshot["release"]["saved_archive_path"]:
        return jsonify({"ok": False, "error": "Сначала загрузи релиз."}), 400

    with STATE["lock"]:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "Пайплайн уже выполняется."}), 400
        STATE["running"] = True
        STATE["auto_mode"] = True
        STATE["stop_requested"] = False
        STATE["current_stage_index"] = -1
        STATE["stage_states"] = ["idle" for _ in PIPELINE_STAGES]

    add_log("Запущен автоматический прогон пайплайна.", "success")

    thread = threading.Thread(target=auto_runner, name=THREAD_NAME, daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    # --- Остановка пайплайна ---
    with STATE["lock"]:
        STATE["stop_requested"] = True
        STATE["running"] = False
        STATE["auto_mode"] = False

    add_log("Получен запрос на остановку пайплайна.", "warn")
    return jsonify({"ok": True})


@app.route("/api/next", methods=["POST"])
def api_next():
    # --- Ручной запуск следующего этапа ---
    snapshot = get_state_snapshot()

    if snapshot["running"]:
        return jsonify({"ok": False, "error": "Нельзя двигать этапы вручную во время автопрогона."}), 400

    if not snapshot["release"]["saved_archive_path"]:
        return jsonify({"ok": False, "error": "Сначала загрузи релиз."}), 400

    if "failed" in snapshot["stage_states"]:
        return jsonify({"ok": False, "error": "Пайплайн уже завершился ошибкой. Сбрось прогон."}), 400

    next_index = snapshot["current_stage_index"] + 1
    if next_index >= len(PIPELINE_STAGES):
        return jsonify({"ok": False, "error": "Все этапы уже пройдены."}), 400

    with STATE["lock"]:
        STATE["running"] = True
        STATE["auto_mode"] = False
        STATE["stop_requested"] = False

    ok = run_stage(next_index)

    with STATE["lock"]:
        STATE["running"] = False
        STATE["stop_requested"] = False

    return jsonify({"ok": ok})


@app.route("/api/prev", methods=["POST"])
def api_prev():
    # --- Шаг назад по визуальному состоянию ---
    snapshot = get_state_snapshot()

    if snapshot["running"]:
        return jsonify({"ok": False, "error": "Нельзя откатывать состояние во время выполнения."}), 400

    prev_index = snapshot["current_stage_index"] - 1

    with STATE["lock"]:
        if prev_index < 0:
            STATE["current_stage_index"] = -1
            STATE["selected_stage_index"] = 0
            STATE["stage_states"] = ["idle" for _ in PIPELINE_STAGES]
        else:
            STATE["current_stage_index"] = prev_index
            STATE["selected_stage_index"] = prev_index
            STATE["stage_states"] = ["idle" for _ in PIPELINE_STAGES]
            for i in range(prev_index):
                STATE["stage_states"][i] = "success"
            STATE["stage_states"][prev_index] = "idle"

    add_log("Выполнен откат визуального состояния пайплайна на шаг назад.", "warn")
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    # --- Сброс прогона с сохранением загруженного архива ---
    snapshot = get_state_snapshot()

    if snapshot["running"]:
        return jsonify({"ok": False, "error": "Сначала останови текущий прогон."}), 400

    with STATE["lock"]:
        STATE["current_stage_index"] = -1
        STATE["selected_stage_index"] = 0
        STATE["stage_states"] = ["idle" for _ in PIPELINE_STAGES]
        STATE["stop_requested"] = False
        STATE["auto_mode"] = False
        STATE["release"]["workspace_path"] = None
        STATE["release"]["artifact_path"] = None
        STATE["release"]["deployed_path"] = None
        STATE["release"]["files_count"] = 0

    add_log("Состояние пайплайна сброшено.", "info")
    return jsonify({"ok": True})


@app.route("/api/state", methods=["GET"])
def api_state():
    # --- Текущее состояние стенда ---
    return jsonify({"ok": True, "state": get_state_snapshot()})


@app.route("/api/select-stage", methods=["POST"])
def api_select_stage():
    # --- Выбор этапа в UI ---
    data = request.get_json(silent=True) or {}
    index = int(data.get("index", 0))
    set_selected_stage(index)
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    # --- Примитивная проверка здоровья контейнера ---
    return jsonify({"ok": True, "status": "up"})


if __name__ == "__main__":
    # --- Старт веб-сервера ---
    app.run(host="0.0.0.0", port=8000, debug=False)
