"""テスト用: server.py を子プロセスで起動し /health が返るまで待つ。"""
import os, subprocess, sys, tempfile, time, urllib.request

PORT = int(os.environ.get("SOLUNA_TEST_PORT", "8931"))
ADMIN = "test-admin-token"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SOLUNA_TEST_DATA") or tempfile.mkdtemp(prefix="soluna-test-")


class ServerProc:
    def __init__(self, env=None, port=None):
        self.env = env or {}
        self.port = port or PORT
        self.p = None

    def __enter__(self):
        env = dict(os.environ, PORT=str(self.port), SOLUNA_ADMIN=ADMIN, SOLUNA_DATA_DIR=DATA_DIR,
                   PYTHONUNBUFFERED="1", **self.env)
        # stdout は PIPE にしない: 1000接続の [listen] ログでパイプが詰まりサーバが止まる
        self.log = open(os.path.join(DATA_DIR, f"server-{self.port}.log"), "w+b")
        self.p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                                  stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=0.5)
                return self
            except Exception:
                if self.p.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError("server exited: " + self.log.read().decode())
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.p.terminate()
        try:
            self.p.wait(3)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()
