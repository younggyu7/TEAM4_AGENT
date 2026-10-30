# 서버 이미지 (TEAM4_AGENT 폴더에서 빌드: docker compose build api 또는 docker build .)
# 이미지에는 backend/ 코드만 넣는다. 설정(.env)은 넣지 않고 compose의 env_file로 준다
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OUTPUT_DIR=/data/output LLM_EXCHANGE_DIR=/data/output/llm_exchange
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# 산출물(output)·업로드 문서는 /data 볼륨에 둔다
RUN useradd --create-home appuser && mkdir -p /data/output /app/data/uploads \
    && chown -R appuser:appuser /data /app/data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
