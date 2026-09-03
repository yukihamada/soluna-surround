FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir aiohttp qrcode
COPY server.py showctl.py discover.py boxctl.py setup.html welcome.html about.html connect.html client.html admin.html dj.html mic.html manifest.webmanifest sw.js ./
COPY icons ./icons
COPY ui ./ui
COPY site ./site
RUN mkdir -p assets
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "server.py"]
