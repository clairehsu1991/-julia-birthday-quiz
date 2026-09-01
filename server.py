#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Julia 35大壽趴 — 誰最懂壽星？ 本機遊戲伺服器

用法：
  1. 把這個檔案跟 game.html 放在「同一個資料夾」
  2. 在 Terminal / 命令提示字元 打開這個資料夾，執行：
       python3 server.py
  3. 這個視窗會印出兩個網址：
       ★ 主持人專屬網址（含一組金鑰）— 只有你自己用，不要投影、不要外流
       ・ 大家加入用的網址 — 遊戲裡會自動變成 QR code 給大家掃
     用主持人專屬網址在這台電腦的瀏覽器打開，就會直接進主持人頁面。
     建議把它加到書籤，之後每次開場都用同一個網址（金鑰不會變）。
  4. 大家的手機先連上跟這台電腦「同一個」WiFi 或個人熱點，再掃描遊戲裡的 QR code 加入

沒有金鑰的人打開網址只會看到「加入遊戲」，進不了主持人頁面。
金鑰存在同資料夾的 host_key.txt；想換一組就把那個檔案刪掉再重跑一次。

放到雲端（Render 之類）時要注意兩件事：
  ・ 免費方案的檔案系統每次重新部署／重啟／閒置休眠都會清空，host_key.txt 會不見、
    金鑰每次都變。請在平台上設一個名叫 HOST_KEY 的環境變數（值自己取一串英數字），
    程式會優先用它，你的主持人網址就固定了。
  ・ 題庫（存在 game_data.json）同樣會被清空。把遊戲裡「⬇ 匯出題庫檔」下載到的檔案
    改名成 seed_banks.json，跟 server.py 放在一起 commit 進 repo，
    伺服器每次啟動就會自動把那些題庫載回來。

不需要安裝任何額外套件，也不需要任何人有 Claude 帳號。
遊戲進行中請保持這個視窗開著、電腦不要進入休眠。按 Ctrl+C 可結束伺服器。
"""

import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, 'game.html')
DATA_PATH = os.path.join(HERE, 'game_data.json')
HOST_KEY_PATH = os.path.join(HERE, 'host_key.txt')
SEED_BANKS_PATH = os.path.join(HERE, 'seed_banks.json')

# ---------------- storage backend: in-memory dict, persisted to a json file ----------------
_lock = threading.Lock()
_store = {}

# ---------------- change feed: lets clients long-poll instead of fixed-interval poll ----------------
# _rev is a monotonic counter bumped on every successful write (set/delete) to ANY key.
# _cond wraps the SAME lock as _lock (Condition(lock) reuses the given lock rather than
# making its own), so code that only needs mutual exclusion can keep using `with _lock:`
# unchanged, while the handlers below that need to notify/wait use `with _cond:` instead —
# both acquire/release the identical underlying lock, so they compose safely with each other.
# See the /api/storage/wait handler and game.html's sWait() for how this is used: instead of
# every screen re-fetching game:state on a fixed timer (which meant up to ~2x the interval of
# real lag before a projector/spectate screen noticed the host had moved on — reported as very
# visible dropped animations), a client holds one long-poll connection open and the server
# wakes it the instant anything changes, then the client does its normal sGet/sList calls.
_rev = 0
_cond = threading.Condition(_lock)


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


def seed_banks():
    """Load question banks that ship alongside the code.

    On a cloud host like Render the filesystem is wiped on every deploy, restart and
    spin-down, so game_data.json — and with it every saved 題庫 — disappears. Committing
    an exported bank file as seed_banks.json means the banks come back with the code.

    Existing banks win: a bank whose id is already in the store is left alone, so a
    seeded bank never overwrites edits made during the current run.
    """
    if not os.path.exists(SEED_BANKS_PATH):
        return
    try:
        with open(SEED_BANKS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print('警告：seed_banks.json 讀取失敗，跳過：', e)
        return

    banks = data if isinstance(data, list) else (data or {}).get('banks')
    if not isinstance(banks, list):
        print('警告：seed_banks.json 裡找不到題庫資料，跳過。')
        return

    added = 0
    with _lock:
        for i, bank in enumerate(banks):
            if not isinstance(bank, dict) or not isinstance(bank.get('questions'), list):
                continue
            if not bank['questions']:
                continue
            bank_id = bank.get('id') or ('seed_%d' % i)
            bank['id'] = bank_id
            bank.setdefault('name', '題庫 %d' % (i + 1))
            bank.setdefault('timeLimitSeconds', 15)
            bank.setdefault('updatedAt', 0)
            key = 'bank:' + bank_id
            if key in _store:
                continue
            _store[key] = json.dumps(bank, ensure_ascii=False)
            added += 1
        if added:
            save_store()
    if added:
        print(f'已從 seed_banks.json 載入 {added} 個題庫')


load_store()
seed_banks()


# ---------------- host key: the secret that unlocks the 主持人 page ----------------
# Kept OUT of the generic /api/storage/* space on purpose — that space is readable
# by anyone on the network, and this must not be.
def load_or_create_host_key():
    env_key = os.environ.get('HOST_KEY')
    if env_key:
        return env_key.strip()
    if os.path.exists(HOST_KEY_PATH):
        try:
            with open(HOST_KEY_PATH, 'r', encoding='utf-8') as f:
                existing = f.read().strip()
            if existing:
                return existing
        except Exception:
            pass
    key = secrets.token_hex(5)  # 10 hex chars, easy enough to retype if needed
    try:
        with open(HOST_KEY_PATH, 'w', encoding='utf-8') as f:
            f.write(key)
    except Exception as e:
        print('警告：無法寫入 host_key.txt，這次的金鑰重開伺服器後會改變：', e)
    return key


HOST_KEY = load_or_create_host_key()


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

        if path == '/api/host/check':
            # The key itself is never sent to the browser — we only ever answer yes/no.
            given = qs.get('key', [''])[0]
            self._send_json({'ok': secrets.compare_digest(given, HOST_KEY)})
        elif path == '/api/storage/get':
            key = qs.get('key', [None])[0]
            with _lock:
                if key is not None and key in _store:
                    self._send_json({'value': _store[key], 'rev': _rev})
                else:
                    self._send_json({'error': 'not_found', 'rev': _rev}, status=404)
        elif path == '/api/storage/list':
            prefix = qs.get('prefix', [''])[0]
            with _lock:
                keys = [k for k in _store.keys() if k.startswith(prefix)]
                rev = _rev
            self._send_json({'keys': keys, 'rev': rev})
        elif path == '/api/storage/wait':
            # Long-poll: hold the connection open until _rev moves past `since`
            # (i.e. some client somewhere wrote ANY key), or `timeout` elapses —
            # whichever comes first. The caller doesn't learn WHAT changed, just
            # that something did; it then does its normal get/list calls to find
            # out what, same as it always did on its old fixed timer. Clamped
            # timeout keeps a single request from blocking its handler thread
            # (ThreadingHTTPServer gives every connection its own thread, so this
            # is safe) forever, and stays comfortably under typical reverse-proxy
            # idle-connection limits if this ever runs behind one.
            try:
                since = int(qs.get('since', ['0'])[0])
            except (TypeError, ValueError):
                since = 0
            try:
                timeout = float(qs.get('timeout', ['25'])[0])
            except (TypeError, ValueError):
                timeout = 25.0
            timeout = max(1.0, min(timeout, 28.0))
            deadline = time.monotonic() + timeout
            with _cond:
                while _rev <= since:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    _cond.wait(remaining)
                current_rev = _rev
            self._send_json({'rev': current_rev})
        elif not path.startswith('/api/'):
            # SPA-style fallback: any non-API path (/, /host, /join, ...) serves
            # the same game.html — the page's own JS decides what to show based
            # on the path/hash. This is what makes a clean URL like /host work.
            self._serve_html()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global _rev
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
            with _cond:
                _store[key] = value
                save_store()
                _rev += 1
                rev = _rev
                _cond.notify_all()
            self._send_json({'ok': True, 'rev': rev})
        elif parsed.path == '/api/storage/delete':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b''
            try:
                data = json.loads(raw.decode('utf-8'))
                key = data['key']
            except Exception:
                self._send_json({'error': 'bad_request'}, status=400)
                return
            with _cond:
                existed = _store.pop(key, None) is not None
                if existed:
                    save_store()
                    _rev += 1
                    _cond.notify_all()
                rev = _rev
            self._send_json({'ok': True, 'deleted': existed, 'rev': rev})
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
        print('=' * 60)
        print('🎂  遊戲伺服器已啟動（雲端模式）')
        print(f'監聽連接埠 {port}，請用主機平台提供的公開網址開啟遊戲。')
        print('')
        print('主持人專屬網址 = 你的公開網址後面接上：')
        print(f'    #host={HOST_KEY}')
        print('這組金鑰不要外流，也不要投影出來。')
        print('=' * 60)
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

    host_url = f'{url}#host={HOST_KEY}'

    print('=' * 60)
    print('🎂  遊戲伺服器已啟動！')
    print('=' * 60)
    print('')
    print('★ 主持人專屬網址（只有你用，不要投影、不要傳給別人）：')
    print(f'\n    {host_url}\n')
    print('  在這台電腦的瀏覽器打開它，就會直接進主持人頁面。')
    print('  建議加到書籤，金鑰不會變，下次開場用同一個網址就好。')
    print('')
    print('・ 大家加入用的網址（遊戲裡會變成 QR code，可以放心投影）：')
    print(f'\n    {url}\n')
    print('  沒有金鑰的人打開它只會看到「加入遊戲」，進不了主持人頁面。')
    print('')
    print('大家的手機要先連上「跟這台電腦同一個」WiFi 或個人熱點，')
    print('才連得到伺服器、掃碼加入。')
    print('')
    print('遊戲進行中請不要關閉這個視窗，也不要讓電腦進入休眠。')
    print('要結束伺服器，按 Ctrl+C。')
    print('=' * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n伺服器已關閉，掰啦～')


if __name__ == '__main__':
    main()
