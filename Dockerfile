FROM python:3.12-slim
WORKDIR /app
COPY bot.py /app/bot.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STATE_DIR=/data PORT=8080
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "bot.py"]
