FROM ubuntu:22.04

# Install Python 3.11 from deadsnakes (more reliable)
RUN apt-get update && apt-get install -y \
    software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3.11-distutils \
    gcc \
    g++ \
    make \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install pip for Python 3.11
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11

# Set Python 3.11 as default
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

WORKDIR /app

COPY requirements.txt .
RUN python3.11 -m pip install --upgrade pip && \
    python3.11 -m pip install --no-cache-dir \
    Flask==2.3.3 \
    Werkzeug==2.3.0 \
    requests==2.31.0 \
    aiohttp==3.8.6 \
    python-telegram-bot==13.15 \
    blackboxprotobuf==1.0.1 \
    pycryptodome==3.19.0 \
    python-dotenv==1.0.0 \
    psutil==5.9.6

COPY . .

RUN mkdir -p uploads logs

EXPOSE 5000

ENV PYTHONUNBUFFERED=1

CMD ["python3.11", "app.py"]
