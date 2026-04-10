# --- Пример минимального приложения для релиза ---
import os

# --- Режим теста ---
if os.getenv("CI_TEST") == "1":
    print("Smoke test passed")
    raise SystemExit(0)

# --- Режим проверки прод-среды ---
if os.getenv("PROD_CHECK") == "1":
    print("Production check passed")
    raise SystemExit(0)

# --- Обычный запуск ---
print("Hello from release")
