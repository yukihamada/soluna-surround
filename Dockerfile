FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir aiohttp
COPY server.py client.html admin.html dj.html mic.html manifest.webmanifest sw.js ./
COPY icons ./icons
RUN mkdir -p assets
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "server.py"]
