FROM python:3.12-alpine

WORKDIR /app

COPY app/app.py /app/app.py

RUN chmod 0444 /app/app.py

EXPOSE 8080

CMD ["python", "/app/app.py"]