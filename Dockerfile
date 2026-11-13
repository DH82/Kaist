FROM apache/airflow:3.3.2-python3.12
USER root
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ffmpeg && rm -rf /var/lib/apt/lists/*
USER airflow
