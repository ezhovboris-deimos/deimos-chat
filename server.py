#!/usr/bin/env python3
"""Deimos Chat v1.0 — локальный веб-чат Hermes."""
import json, os, sqlite3, uuid, time, re, subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
DB = os.path.join(os.path.dirname(__file__), 'chats.db')

def get_db():
    db = sqlite3.connect(DB)
    db.execute('''CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY, title TEXT, model TEXT DEFAULT 'flash',
        created DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT,
        role TEXT, content TEXT, model TEXT,
        created DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    db.commit()
    return db

def call_hermes(msg: str, model: str = 'flash') -> str:
    """Вызов Hermes через CLI (полный агент: скиллы, память, инструменты)."""
    env = os.environ.copy()
    try:
        r = subprocess.run(
            ['/home/deimos/.hermes/hermes-agent/venv/bin/hermes', 'chat',
             '-q', msg, '-m', f'deepseek-v4-{model}',
             '--no-restore-cwd', '--safe-mode'],
            capture_output=True, text=True, timeout=300, cwd='/home/deimos',
            env={**env, 'HOME': '/home/deimos'}
        )
        out = r.stdout.strip() or r.stderr.strip() or '...'
        return out[:8000]
    except subprocess.TimeoutExpired:
        return '⏳ Ответ занимает больше 5 минут. Упростите запрос или переключитесь на flash.'
    except Exception as e:
        return f'Ошибка: {e}'

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
/clear — удалить чат (кнопка ✕)"""

def handle_slash(msg: str, cid: str, model: str) -> str:
    """Обработка / команд без вызова ИИ."""
    cmd = msg.strip().lower()
    if cmd.startswith('/help'):
        return SLASH_HELP
    if cmd.startswith('/status'):
        try:
            r = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'],
                              capture_output=True, text=True, timeout=5)
            gw = r.stdout.strip()
        except:
            gw = 'unknown'
        try:
            r = subprocess.run(['pgrep', '-f', 'sentinel.py'], capture_output=True, text=True, timeout=5)
            sentinel = '✅' if r.stdout.strip() else '❌'
        except:
            sentinel = '?'
        return f'**Статус:** гейтвей: {gw}, sentinel: {sentinel}'
    if cmd.startswith('/model '):
        new_model = cmd.split()[1]
        if new_model in ('flash', 'pro'):
            return f'Модель переключена на **{new_model}**.'
        return 'Доступные модели: **flash**, **pro**.'
    # Остальные / команды — передаём Hermes
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
    # Быстрые / команды — без вызова ИИ
    if msg.startswith('/'):
        reply = handle_slash(msg, cid, model)
        if reply:
            db.execute('INSERT INTO messages (chat_id, role, content, model) VALUES (?, ?, ?, ?)',
                       (cid, 'assistant', reply, model))
            db.commit()
            return jsonify({'reply': reply, 'model': model})
    reply = call_hermes(msg, model)
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
        r = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'],
                          capture_output=True, text=True, timeout=5)
        gw = r.stdout.strip()
    except:
        gw = 'unknown'
    try:
        r = subprocess.run(['pgrep', '-f', 'sentinel.py'], capture_output=True, text=True, timeout=5)
        sentinel = 'active' if r.stdout.strip() else 'inactive'
    except:
        sentinel = 'unknown'
    return jsonify({'gateway': gw, 'sentinel': sentinel})

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8765, debug=False, threaded=True)
