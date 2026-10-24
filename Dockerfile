# Netlithic TEST network node. Test coins have no value.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY config ./config
RUN useradd -m netlithic && mkdir /data && chown netlithic /data
USER netlithic
VOLUME /data
EXPOSE 8000
ENV ADVERTISE_URL=""
CMD ["sh", "-c", "exec python node.py 8000 --data-dir /data --host 0.0.0.0 --peer http://137.184.199.238:8000 --peer http://157.230.63.97:8000 ${ADVERTISE_URL:+--advertise-url $ADVERTISE_URL}"]
