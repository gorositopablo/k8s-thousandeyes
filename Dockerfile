FROM python:3.13-slim
WORKDIR /app
# Install required Python packages
RUN pip install --no-cache-dir requests pyyaml kubernetes flask prometheus_client tenacity
COPY ./src/thousandeyes-sync.py .
CMD ["python", "thousandeyes-sync.py"]