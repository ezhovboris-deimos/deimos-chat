#!/usr/bin/env python3
"""Deimos Chat v1.2 — мульти-ходовый веб-чат Hermes (через -q --resume)."""
import json, os, sqlite3, uuid, time, re, subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
DB = os.path.join(os.path.dirname(__file__), 'chats.db')
HERMES = '/home/deimos/.hermes/hermes-agent/venv/bin/hermes'
RESUME_LOCK = {}  # {chat_id: threading.Lock}

def get_db():
    db = sqlite3.connect(DB)
    db.execute('''CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY, title TEXT, model TEXT DEFAULT 'flash',
        session_id TEXT, created DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT,
        role TEXT, content TEXT, model TEXT,
        created DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.commit()
    return db

def _clean_ansi(text: str) -> str:
    ESC = chr(27)
    text = re.sub(ESC + r'\[1m(.*?)' + ESC + r'\[0m', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(ESC + r'\[3m(.*?)' + ESC + r'\[0m', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(ESC + r'\[4m(.*?)' + ESC + r'\[0m', r'_\1_', text, flags=re.DOTALL)
    text = re.sub(ESC + r'\[[0-9;]*m', '', text)
    return text

def _extract_session_id(output: str) -> str | None:
    m = re.search(r'hermes --resume (\S+)', output)
    return m.group(1) if m else None

def _extract_answer(text: str) -> str:
    """Извлекает финальный ответ."""
    blocks = re.findall(r'╭─\s*⚕\s*Hermes\s*─+╮\n(.*?)\n╰─', text, re.DOTALL)
    if blocks:
        return _clean_ansi(blocks[-1].strip())
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'Initializing agent...' in line:
            return _clean_ansi('\n'.join(lines[i+1:]).strip())
    return _clean_ansi(text[:1500])

def call_hermes(msg: str, model: str, session_id: str | None = None) -> tuple[str, str | None]:
    """Вызов hermes chat -q --resume. Возвращает (ответ, новый session_id)."""
    env = os.environ.copy()
    cmd = [HERMES, 'chat', '-q', msg, '-m', f'deepseek-v4-{model}', '--no-restore-cwd']
    if session_id:
        cmd.extend(['--resume', session_id])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          cwd='/home/deimos', env={**env, 'HOME': '/home/deimos'})
        out = r.stdout.strip() or r.stderr.strip() or '...'
        new_sid = _extract_session_id(out)
        answer = _extract_answer(out)
        if not answer or len(answer) < 3:
            answer = _clean_ansi(out[:1500])
        return answer[:8000], new_sid
    except subprocess.TimeoutExpired:
        return '⏳ Ответ занимает больше 3 мин. Упростите запрос.', session_id
    except Exception as e:
        return f'Ошибка: {e}', session_id

import threading

def _get_lock(cid):
    if cid not in RESUME_LOCK:
        RESUME_LOCK[cid] = threading.Lock()
    return RESUME_LOCK[cid]

# ── API ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/chats')
def api_chats():
    db = get_db()
    chats = db.execute('SELECT id, title, model, created FROM chats ORDER BY created DESC').fetchall()
    return jsonify([{'id': r[0], 'title': r[1], 'model': r[2], 'created': r[3]} for r in chats])

@app.route('/api/chat/new', methods=['POST'])
def api_chat_new():
    cid = uuid.uuid4().hex[:8]
    title = (request.json or {}).get('title', 'Новый чат')[:50]
    db = get_db()
    db.execute('INSERT INTO chats (id, title) VALUES (?, ?)', (cid, title))
    db.commit()
    return jsonify({'id': cid, 'title': title})

@app.route('/api/chat/<cid>/messages')
def api_messages(cid):
    db = get_db()
    msgs = db.execute('SELECT id, role, content, model, created FROM messages WHERE chat_id=? ORDER BY id', (cid,)).fetchall()
    return jsonify([{'id': r[0], 'role': r[1], 'content': r[2], 'model': r[3], 'created': r[4]} for r in msgs])

SLASH_HELP = """**Быстрые команды Deimos Chat:**
/model flash — быстрая модель
/model pro — мощная модель
/help — эта справка
/status — статус системы
/new — сбросить сессию (новый контекст)"""

def handle_slash(msg: str, cid: str, model: str) -> str | None:
    cmd = msg.strip().lower()
    if cmd.startswith('/help'):
        return SLASH_HELP
    if cmd.startswith('/status'):
        try:
            r = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'], capture_output=True, text=True, timeout=5)
            gw = r.stdout.strip()
        except: gw = 'unknown'
        return f'**Статус:** гейтвей: {gw}'
    if cmd.startswith('/model '):
        new_model = cmd.split()[1]
        if new_model in ('flash', 'pro'):
            return f'Модель переключена на **{new_model}**.'
    if cmd.startswith('/new'):
        db = get_db()
        db.execute('UPDATE chats SET session_id=NULL WHERE id=?', (cid,))
        db.commit()
        return 'Сессия сброшена. Новый контекст.'
    return None

@app.route('/api/chat/<cid>/send', methods=['POST'])
def api_chat_send(cid):
    data = request.json or {}
    msg = data.get('message', '').strip()
    model = data.get('model', 'flash')
    if not msg:
        return jsonify({'error': 'empty'}), 400

    db = get_db()
    db.execute('INSERT INTO messages (chat_id, role, content, model) VALUES (?, ?, ?, ?)',
               (cid, 'user', msg, model))
    db.execute('UPDATE chats SET model=? WHERE id=?', (model, cid))
    db.commit()

    if msg.startswith('/'):
        reply = handle_slash(msg, cid, model)
        if reply:
            db.execute('INSERT INTO messages (chat_id, role, content, model) VALUES (?, ?, ?, ?)',
                       (cid, 'assistant', reply, model))
            db.commit()
            return jsonify({'reply': reply, 'model': model})

    with _get_lock(cid):
        row = db.execute('SELECT session_id FROM chats WHERE id=?', (cid,)).fetchone()
        sid = row[0] if row else None
        reply, new_sid = call_hermes(msg, model, sid)
        if new_sid:
            db.execute('UPDATE chats SET session_id=? WHERE id=?', (new_sid, cid))
            db.commit()

    db.execute('INSERT INTO messages (chat_id, role, content, model) VALUES (?, ?, ?, ?)',
               (cid, 'assistant', reply, model))
    db.commit()
    return jsonify({'reply': reply, 'model': model})

@app.route('/api/chat/<cid>/delete', methods=['POST'])
def api_chat_delete(cid):
    db = get_db()
    db.execute('DELETE FROM messages WHERE chat_id=?', (cid,))
    db.execute('DELETE FROM chats WHERE id=?', (cid,))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/status')
def api_status():
    try:
        r = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'], capture_output=True, text=True, timeout=5)
        gw = r.stdout.strip()
    except: gw = 'unknown'
    return jsonify({'gateway': gw})

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8765, debug=False, threaded=True)
