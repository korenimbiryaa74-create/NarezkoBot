"""Restored local HTTP CONNECT bridge for authenticated HTTP/SOCKS proxies.
Original module was not retained; ProxyBridge reused from saved chatgpt_profiles.py.
Binds only to loopback. No direct-connection fallback when proxy is configured.
"""
from __future__ import annotations
import atexit
import contextlib
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit
import proxy_setup

parse_proxy = proxy_setup.parse_proxy

class ProxyBridge:
    """Loopback HTTP/CONNECT -> authenticated SOCKS/HTTP proxy, DNS upstream."""
    def __init__(self,url):
        self.spec=parse_proxy(url)
        self.server=None

    def connect(self,host,port):
        import socks
        scheme,proxy_host,proxy_port,user,password=self.spec
        kind={"socks5":socks.SOCKS5,"socks5h":socks.SOCKS5,
              "socks4":socks.SOCKS4,"http":socks.HTTP}[scheme]
        s=socks.socksocket()
        s.settimeout(30)
        s.set_proxy(kind,proxy_host,proxy_port,rdns=True,username=user or None,password=password or None)
        try: s.connect((host,port))
        except Exception: s.close(); raise
        return s

    def start(self):
        bridge=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                client=self.request; upstream=None
                try:
                    client.settimeout(30)
                    data=b""
                    while b"\r\n\r\n" not in data:
                        chunk=client.recv(8192)
                        if not chunk: return
                        data+=chunk
                        if len(data)>65536: raise ValueError("headers too large")
                    header,body=data.split(b"\r\n\r\n",1)
                    first,*headers=header.decode("iso-8859-1").split("\r\n")
                    method,target,version=first.split(" ",2)
                    if method.upper()=="CONNECT":
                        host,port=target.rsplit(":",1)
                        upstream=bridge.connect(host.strip("[]"),int(port))
                        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        if body: upstream.sendall(body)
                    else:
                        p=urlsplit(target)
                        if p.scheme!="http" or not p.hostname: raise ValueError("absolute HTTP URL required")
                        upstream=bridge.connect(p.hostname,p.port or 80)
                        path=(p.path or "/")+("?"+p.query if p.query else "")
                        headers=[h for h in headers if not h.lower().startswith(("proxy-authorization:","proxy-connection:"))]
                        upstream.sendall((f"{method} {path} {version}\r\n"+"\r\n".join(headers)+"\r\n\r\n").encode("iso-8859-1")+body)
                    client.settimeout(None); upstream.settimeout(None)
                    sockets=[client,upstream]
                    while True:
                        ready,_,_=select.select(sockets,[],[],120)
                        if not ready: break
                        for source in ready:
                            block=source.recv(65536)
                            if not block: return
                            (upstream if source is client else client).sendall(block)
                except Exception:
                    # No credentials, URLs or remote errors in log.
                    with contextlib.suppress(OSError):
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                finally:
                    if upstream: upstream.close()
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True
        self.server=Server(("127.0.0.1",0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        if self.server:
            self.server.shutdown(); self.server.server_close()


_bridges = {}
_lock = threading.Lock()

def chrome_args():
    if not proxy_setup.for_chatgpt():
        return []
    # Report missing dependency before opening Chrome, not as a later 502.
    try:
        import socks
    except ImportError as exc:
        raise RuntimeError("Для прокси установи зависимости: pip install -r requirements.txt") from exc
    url = proxy_setup.proxy_url()
    with _lock:
        if url not in _bridges:
            bridge = ProxyBridge(url)
            local = bridge.start()
            _bridges[url] = (bridge, local)
        local = _bridges[url][1]
    return ["--proxy-server=" + local,
            "--proxy-bypass-list=localhost;127.0.0.1;[::1]", "--disable-quic"]

def close_all():
    with _lock:
        bridges = list(_bridges.values())
        _bridges.clear()
    for bridge, _ in bridges:
        with contextlib.suppress(Exception):
            bridge.close()

atexit.register(close_all)
