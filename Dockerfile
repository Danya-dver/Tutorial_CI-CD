FROM python:3.11-slim

# --- Базовые настройки окружения ---
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --- Устанавливаем зависимости ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Копируем приложение ---
COPY app ./app

# --- Создаём рабочие директории ---
RUN mkdir -p /app/data/uploads /app/data/workspace /app/data/deploy /app/data/artifacts

EXPOSE 8000

# --- Старт приложения ---
CMD ["python", "app/app.py"]
