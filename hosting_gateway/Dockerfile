FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# deck_contract.py and deck_runtime/ are copied here from proposal_agent/app by infra/deploy.sh
# (single source of truth lives in proposal_agent/app).
COPY main.py deck_contract.py ./
COPY deck_runtime ./deck_runtime

ENV PORT=8080 PYTHONUNBUFFERED=1 DECK_RUNTIME_DIR=/app/deck_runtime
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
