FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt .
RUN apk add --no-cache nftables && pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 9100
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","9100"]
