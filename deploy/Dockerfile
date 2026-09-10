FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY console_registration_api.py .
COPY console_config.py .
COPY db.py .
COPY ar_agent.py .
COPY master_agent.py .
COPY qbo_client.py .
COPY qbo_config.py .
COPY sub_agents/ sub_agents/
COPY static/ static/

EXPOSE 8000
CMD ["uvicorn", "console_registration_api:app", "--host", "0.0.0.0", "--port", "8000"]
