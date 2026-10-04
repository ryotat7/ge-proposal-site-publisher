# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Private preview renderer: headless Chromium + Japanese fonts. Deployed with --no-allow-unauthenticated.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-noto-cjk fonts-noto-color-emoji fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# deck_contract.py and deck_runtime/ are copied here from proposal_agent/app by infra/deploy.sh.
COPY main.py deck_contract.py ./
COPY deck_runtime ./deck_runtime

ENV CHROME_BIN=/usr/bin/chromium PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
