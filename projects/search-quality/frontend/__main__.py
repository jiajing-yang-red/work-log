import argparse
import fcntl
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from quality_input.common import InputError
from .service import PROJECT, Conflict, Service


STATIC = Path(__file__).resolve().parent / "static"


def make_server(service, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8", filename=None):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trusted(self, mutation=False):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "仅允许本机访问。"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in hosts}:
                self.send(403, {"error": "不接受其他网站发起的请求。"})
                return False
            if mutation and not secrets.compare_digest(self.headers.get("X-Eval-Token", ""), service.token):
                self.send(403, {"error": "页面会话已失效，请刷新后重试。"})
                return False
            return True

        def error(self, exc):
            if isinstance(exc, Conflict):
                self.send(409, {"error": str(exc)})
            elif isinstance(exc, FileNotFoundError):
                self.send(404, {"error": "文件或评测记录不存在。"})
            elif isinstance(exc, (InputError, ValueError)):
                self.send(400, {"error": str(exc)})
            else:
                self.send(500, {"error": "本地服务读取失败，请检查数据文件或重启服务。"})

        def do_GET(self):
            if not self.trusted():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                static = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/timing.js": ("timing.js", "text/javascript; charset=utf-8"),
                    "/ranking.js": ("ranking.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }
                if path in static:
                    name, mime = static[path]
                    return self.send(200, (STATIC / name).read_bytes(), mime)
                if path == "/api/bootstrap":
                    return self.send(200, service.catalog())
                if path == "/api/jobs":
                    return self.send(200, {"jobs": service.jobs()})
                if path == "/api/prompts":
                    filters = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                    return self.send(200, service.prompt_templates(filters))
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
                    job_id = parts[2]
                    if len(parts) == 3:
                        return self.send(200, service.detail(job_id))
                    if len(parts) == 4 and parts[3] == "ranking":
                        from quality_metrics.ranking import summary
                        return self.send(200, summary(service.ranking(job_id)))
                    if len(parts) == 4 and parts[3] == "ranking-comparison":
                        filters = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                        return self.send(200, service.ranking_comparison(job_id, filters))
                    if len(parts) == 4 and parts[3] == "results":
                        filters = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                        return self.send(200, service.results(job_id, filters))
                    if len(parts) == 4 and parts[3] == "prompts":
                        return self.send(200, service.prompt_tasks(job_id))
                    if len(parts) == 5 and parts[3] == "prompts":
                        return self.send(200, service.task_prompt(job_id, parts[4]))
                    if len(parts) == 5 and parts[3] == "responses":
                        return self.send(200, service.task_response(job_id, parts[4]))
                    if len(parts) == 5 and parts[3] == "download":
                        data = service.download(job_id, parts[4])
                        return self.send(200, data, "application/octet-stream", filename=parts[4])
                self.send(404, {"error": "找不到该页面。"})
            except Exception as exc:
                self.error(exc)

        def do_POST(self):
            if not self.trusted(mutation=True):
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise InputError("请求必须使用 JSON。")
                length = int(self.headers.get("Content-Length", "0"))
                max_length = 1048576 if self.path == "/api/prompt-versions" else 16384
                if length < 0 or length > max_length:
                    raise InputError("请求内容过大。")
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/datasets/preview":
                    return self.send(200, service.preview_scope(body))
                if self.path == "/api/datasets/references":
                    return self.send(200, service.calibration_samples(body))
                if self.path == "/api/datasets/browse":
                    return self.send(200, service.dataset_store.browse(body))
                if self.path == "/api/datasets":
                    return self.send(200, service.dataset_store.add(body))
                if self.path == "/api/models/discover":
                    return self.send(200, service.discover_models(body))
                if self.path == "/api/prompt-versions":
                    return self.send(201, service.prompt_store.save(body))
                if self.path == "/api/jobs":
                    return self.send(201, service.start(body))
                if self.path == "/api/jobs/plan":
                    return self.send(200, service.start(body, dry_run=True))
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "resume":
                    if not isinstance(body, dict) or set(body) - {"retry_failed"}:
                        raise InputError("恢复操作不接受配置更改。")
                    return self.send(200, service.resume(parts[2], retry_failed=body.get("retry_failed", False)))
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                    return self.send(200, service.cancel(parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("delete", "restore"):
                    if body != {}:
                        raise InputError("该操作不接受附加参数。")
                    action = service.delete if parts[3] == "delete" else service.restore
                    return self.send(200, action(parts[2]))
                self.send(404, {"error": "找不到该操作。"})
            except Exception as exc:
                self.error(exc)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Local search quality evaluation dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", help="Directory containing normalized dataset folders")
    parser.add_argument("--runs-root", help="Directory for new browser evaluation runs")
    args = parser.parse_args()
    runs_root = Path(args.runs_root or Path(args.data_root or PROJECT.parent / "output") / "frontend-runs")
    runs_root.mkdir(parents=True, exist_ok=True)
    lock = (runs_root / ".service.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        parser.exit(1, "该历史目录已有前端服务运行，请打开现有服务。\n")
    service = Service(data_root=args.data_root, runs_root=args.runs_root)
    try:
        server = make_server(service, args.port)
    except OSError:
        lock.close()
        parser.exit(1, f"端口 {args.port} 无法使用；请选择其他端口。\n")
    print(f"搜索质量评测：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        service.close()
        server.server_close()
        lock.close()


if __name__ == "__main__":
    main()
