FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=8080 NCI_DB=/data/nci.db
VOLUME /data
CMD ["python", "server.py"]
