#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Julia 35大壽趴 — 誰最懂壽星？ 本機遊戲伺服器

用法：
  1. 把這個檔案跟 game.html 放在「同一個資料夾」
  2. 在 Terminal / 命令提示字元 打開這個資料夾，執行：
       python3 server.py
  3. 依照畫面印出來的網址，在電腦瀏覽器打開，選「我是主持人」
  4. 大家的手機先連上跟這台電腦「同一個」WiFi 或個人熱點，再掃描遊戲裡的 QR code 加入

不需要安裝任何額外套件，也不需要任何人有 Claude 帳號。
遊戲進行中請保持這個視窗開著、電腦不要進入休眠。按 Ctrl+C 可結束伺服器。
"""

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, 'game.html')
DATA_PATH = os.path.join(HERE, 'game_data.json')

# ---------------- storage backend: in-memory dict, persisted to a json file ----------------
_lock = threading.Lock()
_store = {}


def load_store():
    global _store
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, 'r', encoding='utf-8') as f:
                _store = json.load(f)
            print(f'已讀取先前的遊戲資料（{len(_store)} 筆）')
        except Exception:
            _store = {}


def save_store():
    try:
        with open(DATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(_store, f, ensure_ascii=False)
    except Exception as e:
        print('警告：儲存遊戲資料失敗：', e)


load_store()


# ---------------- HTTP handler ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console output clean during the party

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        try:
            with open(HTML_PATH, 'rb') as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(500)
            self.end_headers()
            self.wfile.write('找不到 game.html，請確認它跟 server.py 放在同一個資料夾。'.encode('utf-8'))
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == '/api/storage/get':
            key = qs.get('key', [None])[0]
            with _lock:
                if key is not None and key in _store:
                    self._send_json({'value': _store[key]})
                else:
                    self._send_json({'error': 'not_found'}, status=404)
        elif path == '/api/storage/list':
            prefix = qs.get('prefix', [''])[0]
            with _lock:
                keys = [k for k in _store.keys() if k.startswith(prefix)]
            self._send_json({'keys': keys})
        elif not path.startswith('/api/'):
            # SPA-style fallback: any non-API path (/, /host, /join, ...) serves
            # the same game.html — the page's own JS decides what to show based
            # on the path/hash. This is what makes a clean URL like /host work.
            self._serve_html()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/storage/set':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b''
            try:
                data = json.loads(raw.decode('utf-8'))
                key = data['key']
                value = data['value']
            except Exception:
                self._send_json({'error': 'bad_request'}, status=400)
                return
            with _lock:
                _store[key] = value
                save_store()
            self._send_json({'ok': True})
        else:
            self.send_response(404)
            self.end_headers()


def get_local_ip():
    """Find this computer's LAN IP without needing real internet access."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))  # doesn't actually send anything
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def main():
    # --- Cloud deployment mode (e.g. Render) ---
    # Render (and most PaaS hosts) tell the app which port to listen on via $PORT,
    # and give it a public https:// URL automatically — no "same WiFi" needed.
    cloud_port = os.environ.get('PORT')
    if cloud_port:
        try:
            port = int(cloud_port)
            server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
        except (ValueError, OSError) as e:
            print('無法啟動伺服器（雲端模式）：', e)
            return
        print('=' * 56)
        print('🎂  遊戲伺服器已啟動（雲端模式）')
        print(f'監聽連接埠 {port}，請用主機平台提供的公開網址開啟遊戲。')
        print('=' * 56)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\n伺服器已關閉。')
        return

    # --- Local network mode (run on your own laptop) ---
    server = None
    chosen_port = None
    for candidate_port in (8787, 8000, 8080, 8888, 5050):
        try:
            server = ThreadingHTTPServer(('0.0.0.0', candidate_port), Handler)
            chosen_port = candidate_port
            break
        except OSError:
            continue

    if server is None:
        print('啟動失敗：常用連接埠都被佔用了，請關掉其他佔用網路的程式後再試一次。')
        return

    ip = get_local_ip()
    url = f'http://{ip}:{chosen_port}/'

    print('=' * 56)
    print('🎂  遊戲伺服器已啟動！')
    print('=' * 56)
    print(f'請在這台電腦的瀏覽器打開下面這個網址，選「我是主持人」：')
    print(f'\n    {url}\n')
    print('大家的手機要先連上「跟這台電腦同一個」WiFi 或個人熱點，')
    print('才連得到伺服器、掃碼加入。')
    print('')
    print('遊戲進行中請不要關閉這個視窗，也不要讓電腦進入休眠。')
    print('要結束伺服器，按 Ctrl+C。')
    print('=' * 56)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n伺服器已關閉，掰啦～')


if __name__ == '__main__':
    main()
