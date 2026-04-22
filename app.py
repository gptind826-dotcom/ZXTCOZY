from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, Response
from datetime import datetime
import subprocess
import os
import uuid
import json
import signal
import time
import shutil
import zipfile
import threading
import sys
import re
import pty
import select
import fcntl
import termios
import struct
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'ZXTCOZY-super-secret-key-2024')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# ── PATHS ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOGS_FOLDER = os.path.join(BASE_DIR, 'logs')
DATA_FILE = os.path.join(BASE_DIR, 'scripts_db.json')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# ── RAILWAY PORT ───────────────────────────────────────────────────
PORT = int(os.environ.get('PORT', 5000))

# ── PYTHON VERSION DETECTION FOR TELEGRAM BOT COMPATIBILITY ────────
PYTHON_CMD = None
# Prefer Python 3.11 or 3.10 for telegram bot compatibility
for version in ['3.11', '3.10', '3.9']:
    cmd = f'python{version}'
    try:
        result = subprocess.run([cmd, '--version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            PYTHON_CMD = cmd
            print(f"[Python] Using {cmd} for bot execution")
            break
    except:
        continue

if not PYTHON_CMD:
    # Try to find any python3
    try:
        result = subprocess.run(['python3', '--version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            PYTHON_CMD = 'python3'
            print(f"[Python] Using python3 for bot execution")
    except:
        PYTHON_CMD = sys.executable
        print(f"[Python] Using default: {PYTHON_CMD}")

# ── IN-MEMORY STATE ──
running_processes: dict = {}
scripts_db: dict = {}

# ── PERSIST / RESTORE DB ──
def save_db():
    try:
        safe = {}
        for sid, s in scripts_db.items():
            safe[sid] = {k: v for k, v in s.items() if k not in ['process', 'master_fd', 'lock']}
        with open(DATA_FILE, 'w') as f:
            json.dump(safe, f, indent=2)
    except Exception as e:
        print(f'[DB] Save error: {e}')

def load_db():
    global scripts_db
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        for sid, s in data.items():
            s['status'] = 'stopped'
            s['pid'] = None
            scripts_db[sid] = s
        print(f'[DB] Loaded {len(scripts_db)} scripts')
    except Exception as e:
        print(f'[DB] Load error: {e}')

load_db()

# ── AUTH ──
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'py', 'txt', 'json', 'yml', 'yaml', 'md', 'html', 'css', 'js', 'sh', 'csv', 'ini', 'cfg', 'toml', 'zip'}

# ══════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return redirect(url_for('dashboard') if session.get('logged_in') else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == 'admin123':
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid access key')
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/filemanager')
@login_required
def filemanager():
    return render_template('filemanager.html')

@app.route('/console/<script_id>')
@login_required
def console(script_id):
    if script_id not in scripts_db:
        return redirect(url_for('dashboard'))
    return render_template('console.html', script_id=script_id)

# ══════════════════════════════════════════════════════════════════
#  SCRIPT UPLOAD API (Supports Telegram Bots)
# ══════════════════════════════════════════════════════════════════

@app.route('/api/upload-script', methods=['POST'])
@login_required
def upload_script():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    
    filename = secure_filename(file.filename)
    script_id = uuid.uuid4().hex[:8]
    
    script_dir = os.path.join(UPLOAD_FOLDER, script_id)
    os.makedirs(script_dir, exist_ok=True)
    
    file_ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    
    if file_ext == 'zip':
        zip_path = os.path.join(script_dir, filename)
        file.save(zip_path)
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(script_dir)
            os.remove(zip_path)
            
            main_file = None
            for root, dirs, files in os.walk(script_dir):
                for f in files:
                    if f.endswith('.py'):
                        if f in ['main.py', 'app.py', 'run.py', 'server.py', 'bot.py']:
                            main_file = os.path.join(root, f)
                            break
                        if not main_file:
                            main_file = os.path.join(root, f)
                if main_file:
                    break
            
            if not main_file:
                shutil.rmtree(script_dir)
                return jsonify({'error': 'No .py file found in zip'}), 400
            
            # Check for requirements.txt and install if found
            req_file = os.path.join(script_dir, 'requirements.txt')
            if os.path.exists(req_file):
                print(f"[Bot] Found requirements.txt in {script_id}")
            
            project_name = filename.replace('.zip', '')
            script_name = f"{project_name}/{os.path.basename(main_file)}"
            
        except zipfile.BadZipFile:
            shutil.rmtree(script_dir)
            return jsonify({'error': 'Invalid zip file'}), 400
    else:
        script_path = os.path.join(script_dir, filename)
        file.save(script_path)
        main_file = script_path
        script_name = filename
    
    scripts_db[script_id] = {
        'id': script_id,
        'name': script_name,
        'path': main_file,
        'workspace': script_dir,
        'status': 'stopped',
        'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'pid': None,
        'log_file': os.path.join(LOGS_FOLDER, f'{script_id}.log')
    }
    save_db()
    return jsonify({'success': True, 'script_id': script_id, 'name': script_name})

# ══════════════════════════════════════════════════════════════════
#  SCRIPT WORKSPACE FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def get_script_workspace(script_id):
    if script_id not in scripts_db:
        return None
    script = scripts_db[script_id]
    workspace = script.get('workspace')
    if not workspace or not os.path.exists(workspace):
        workspace = os.path.dirname(script['path'])
        scripts_db[script_id]['workspace'] = workspace
    return workspace

def safe_script_path(script_id, rel_path):
    workspace = get_script_workspace(script_id)
    if not workspace:
        raise ValueError('Script not found')
    if not rel_path:
        return workspace
    full = os.path.realpath(os.path.join(workspace, rel_path))
    if not full.startswith(os.path.realpath(workspace)):
        raise ValueError('Path traversal detected')
    return full

@app.route('/api/scripts/<script_id>/files/list')
@login_required
def script_list_files(script_id):
    path = request.args.get('path', '')
    try:
        base_path = safe_script_path(script_id, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(base_path):
        os.makedirs(base_path, exist_ok=True)

    try:
        items = []
        for item in sorted(os.listdir(base_path)):
            item_path = os.path.join(base_path, item)
            is_dir = os.path.isdir(item_path)
            stat = os.stat(item_path)
            items.append({
                'name': item,
                'path': os.path.join(path, item).lstrip('/') if path else item,
                'is_dir': is_dir,
                'size': stat.st_size if not is_dir else 0,
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                'extension': os.path.splitext(item)[1].lower() if not is_dir else ''
            })
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        return jsonify({'success': True, 'current_path': path, 'files': items})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<script_id>/files/upload', methods=['POST'])
@login_required
def script_upload_file(script_id):
    path = request.form.get('path', '')
    try:
        target = safe_script_path(script_id, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    os.makedirs(target, exist_ok=True)

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    uploaded = []
    for file in request.files.getlist('file'):
        if not file.filename:
            continue
        filename = secure_filename(file.filename)
        if not filename:
            continue
        file.save(os.path.join(target, filename))
        uploaded.append(filename)

    return jsonify({'success': True, 'uploaded': uploaded})

@app.route('/api/scripts/<script_id>/files/delete', methods=['POST'])
@login_required
def script_delete_file(script_id):
    data = request.json or {}
    path = data.get('path', '')
    try:
        full = safe_script_path(script_id, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(full):
        return jsonify({'error': 'Not found'}), 404
    try:
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<script_id>/files/rename', methods=['POST'])
@login_required
def script_rename_file(script_id):
    data = request.json or {}
    old_path = data.get('old_path', '')
    new_name = secure_filename(data.get('new_name', ''))
    if not new_name:
        return jsonify({'error': 'Invalid name'}), 400
    try:
        old_full = safe_script_path(script_id, old_path)
        new_full = os.path.join(os.path.dirname(old_full), new_name)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(old_full):
        return jsonify({'error': 'Not found'}), 404
    try:
        os.rename(old_full, new_full)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<script_id>/files/create-folder', methods=['POST'])
@login_required
def script_create_folder(script_id):
    data = request.json or {}
    folder_name = secure_filename(data.get('folder_name', ''))
    if not folder_name:
        return jsonify({'error': 'Invalid folder name'}), 400
    try:
        full = safe_script_path(script_id, os.path.join(data.get('path', ''), folder_name))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    try:
        os.makedirs(full, exist_ok=True)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<script_id>/files/create-file', methods=['POST'])
@login_required
def script_create_file(script_id):
    data = request.json or {}
    filename = secure_filename(data.get('filename', ''))
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    try:
        full = safe_script_path(script_id, os.path.join(data.get('path', ''), filename))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    try:
        with open(full, 'w') as f:
            f.write(data.get('content', ''))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<script_id>/files/edit', methods=['GET', 'POST'])
@login_required
def script_edit_file(script_id):
    if request.method == 'GET':
        path = request.args.get('path', '')
        try:
            full = safe_script_path(script_id, path)
        except ValueError:
            return jsonify({'error': 'Invalid path'}), 400
        if not os.path.isfile(full):
            return jsonify({'error': 'Not found'}), 404
        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                return jsonify({'success': True, 'content': f.read()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        data = request.json or {}
        path = data.get('path', '')
        try:
            full = safe_script_path(script_id, path)
        except ValueError:
            return jsonify({'error': 'Invalid path'}), 400
        try:
            with open(full, 'w', encoding='utf-8') as f:
                f.write(data.get('content', ''))
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<script_id>/files/zip', methods=['POST'])
@login_required
def script_zip_files(script_id):
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': 'No files selected'}), 400
    
    workspace = get_script_workspace(script_id)
    zip_name = f'archive_{uuid.uuid4().hex[:8]}.zip'
    zip_path = os.path.join(workspace, zip_name)
    
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                full = safe_script_path(script_id, p)
                if os.path.isdir(full):
                    for root, _, files in os.walk(full):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            zf.write(fp, os.path.relpath(fp, workspace))
                elif os.path.isfile(full):
                    zf.write(full, p)
        return jsonify({'success': True, 'zip_file': zip_name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════
#  TELEGRAM BOT SPECIFIC HANDLING
# ══════════════════════════════════════════════════════════════════

def install_bot_dependencies(workspace):
    """Install requirements for Telegram bot"""
    req_file = os.path.join(workspace, 'requirements.txt')
    if os.path.exists(req_file):
        try:
            # Install specific versions that work together
            subprocess.run([PYTHON_CMD, '-m', 'pip', 'install', '--upgrade', 'pip'], 
                          capture_output=True, timeout=60)
            result = subprocess.run(
                [PYTHON_CMD, '-m', 'pip', 'install', '-r', req_file, '--no-cache-dir', '--prefer-binary'],
                capture_output=True, text=True, timeout=300, cwd=workspace
            )
            print(f"[Bot] Dependencies installed: {result.returncode}")
            return result.returncode == 0
        except Exception as e:
            print(f"[Bot] Dependency install error: {e}")
            return False
    return True

# ══════════════════════════════════════════════════════════════════
#  SCRIPT MANAGEMENT (PTY-BASED FOR TELEGRAM BOTS)
# ══════════════════════════════════════════════════════════════════

def _pty_reader(script_id: str, master_fd: int):
    info = running_processes.get(script_id)
    script = scripts_db.get(script_id)
    if not info or not script:
        return
    log_path = script['log_file']
    try:
        with open(log_path, 'ab') as log:
            while True:
                try:
                    r, _, _ = select.select([master_fd], [], [], 0.1)
                except (ValueError, OSError):
                    break
                if r:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    log.write(data)
                    log.flush()
                    with info['lock']:
                        info['buffer'] += data.decode('utf-8', errors='replace')
                        if len(info['buffer']) > 200000:
                            info['buffer'] = info['buffer'][-200000:]
                proc = info.get('process')
                if proc and proc.poll() is not None:
                    try:
                        while True:
                            r2, _, _ = select.select([master_fd], [], [], 0.05)
                            if not r2:
                                break
                            tail = os.read(master_fd, 4096)
                            if not tail:
                                break
                            log.write(tail)
                            log.flush()
                            with info['lock']:
                                info['buffer'] += tail.decode('utf-8', errors='replace')
                    except OSError:
                        pass
                    break
    except Exception as e:
        print(f'[PTY reader {script_id}] error: {e}')
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if script_id in running_processes:
            running_processes[script_id]['master_fd'] = None
        if script_id in scripts_db:
            scripts_db[script_id]['status'] = 'stopped'
            scripts_db[script_id]['pid'] = None
        save_db()

@app.route('/api/start/<script_id>', methods=['POST'])
@login_required
def start_script(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404
    script = scripts_db[script_id]

    if not os.path.exists(script['path']):
        return jsonify({'error': 'Script file missing from disk'}), 404

    _kill_process(script_id)
    
    # Install dependencies before starting
    install_bot_dependencies(script['workspace'])

    with open(script['log_file'], 'ab') as f:
        header = f"\n{'='*60}\nStarted: {datetime.now()}\nScript: {script['name']}\nPython: {PYTHON_CMD}\n{'='*60}\n\n"
        f.write(header.encode())

    try:
        master_fd, slave_fd = pty.openpty()
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        winsize = struct.pack('HHHH', 24, 80, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        # Set environment variables for Telegram bot
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        
        process = subprocess.Popen(
            [PYTHON_CMD, '-u', script['path']],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=os.path.dirname(script['path']),
            close_fds=True,
            start_new_session=True,
            env=env
        )
        os.close(slave_fd)

        info = {
            'process': process,
            'master_fd': master_fd,
            'buffer': '',
            'lock': threading.Lock(),
        }
        running_processes[script_id] = info

        t = threading.Thread(target=_pty_reader, args=(script_id, master_fd), daemon=True)
        t.start()

        script['status'] = 'running'
        script['pid'] = process.pid
        script['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_db()

        return jsonify({'success': True, 'pid': process.pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _kill_process(script_id: str):
    info = running_processes.pop(script_id, None)
    if not info:
        return
    proc = info.get('process')
    if proc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    mfd = info.get('master_fd')
    if mfd:
        try:
            os.close(mfd)
        except OSError:
            pass
    if script_id in scripts_db:
        scripts_db[script_id]['status'] = 'stopped'
        scripts_db[script_id]['pid'] = None

@app.route('/api/stop/<script_id>', methods=['POST'])
@login_required
def stop_script(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404
    _kill_process(script_id)
    with open(scripts_db[script_id]['log_file'], 'ab') as f:
        f.write(f"\n{'='*60}\nStopped: {datetime.now()}\n{'='*60}\n\n".encode())
    save_db()
    return jsonify({'success': True})

@app.route('/api/delete-script/<script_id>', methods=['DELETE'])
@login_required
def delete_script(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404
    _kill_process(script_id)
    script = scripts_db.pop(script_id)
    workspace = script.get('workspace')
    if workspace and os.path.exists(workspace):
        try:
            shutil.rmtree(workspace)
        except Exception:
            pass
    log_file = script.get('log_file')
    if log_file and os.path.exists(log_file):
        try:
            os.remove(log_file)
        except Exception:
            pass
    save_db()
    return jsonify({'success': True})

@app.route('/api/scripts')
@login_required
def get_scripts():
    for sid, script in scripts_db.items():
        info = running_processes.get(sid)
        if info:
            proc = info.get('process')
            if proc and proc.poll() is None:
                script['status'] = 'running'
            else:
                script['status'] = 'stopped'
                script['pid'] = None
                running_processes.pop(sid, None)
        else:
            script['status'] = 'stopped'
            script['pid'] = None
    return jsonify(list(scripts_db.values()))

# ── LOGS ──
@app.route('/api/logs/<script_id>')
@login_required
def get_logs(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404

    info = running_processes.get(script_id)
    if info:
        with info['lock']:
            buf = info['buffer']
        return jsonify({'logs': buf, 'live': True})

    log_file = scripts_db[script_id]['log_file']
    if not os.path.exists(log_file):
        return jsonify({'logs': '', 'live': False})
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        if len(content) > 100000:
            content = '...(truncated)...\n' + content[-100000:]
        return jsonify({'logs': content, 'live': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs/stream/<script_id>')
@login_required
def stream_logs(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404

    def generate():
        sent = 0
        yield 'retry: 1000\n\n'
        while True:
            info = running_processes.get(script_id)
            if info:
                with info['lock']:
                    buf = info['buffer']
                if len(buf) > sent:
                    chunk = buf[sent:]
                    sent = len(buf)
                    for line in chunk.splitlines():
                        yield f'data: {json.dumps(line)}\n\n'
            script = scripts_db.get(script_id)
            if script and script['status'] == 'stopped' and script_id not in running_processes:
                yield 'event: stopped\ndata: {}\n\n'
                break
            time.sleep(0.3)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ── STDIN FOR TELEGRAM BOT INTERACTION ──
@app.route('/api/stdin/<script_id>', methods=['POST'])
@login_required
def send_stdin(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404
    info = running_processes.get(script_id)
    if not info:
        return jsonify({'error': 'Script is not running'}), 400
    mfd = info.get('master_fd')
    if mfd is None:
        return jsonify({'error': 'PTY not available'}), 500

    data = request.json or {}
    text = data.get('text', '')
    if not text.endswith('\n'):
        text += '\n'
    try:
        os.write(mfd, text.encode('utf-8'))
        return jsonify({'success': True})
    except OSError as e:
        return jsonify({'error': str(e)}), 500

# ── INSTALL REQUIREMENTS ──
@app.route('/api/install-requirements/<script_id>', methods=['POST'])
@login_required
def install_requirements(script_id):
    if script_id not in scripts_db:
        return jsonify({'error': 'Script not found'}), 404
    script = scripts_db[script_id]
    workspace = get_script_workspace(script_id)
    req_file = os.path.join(workspace, 'requirements.txt')

    if not os.path.exists(req_file):
        return jsonify({'error': 'No requirements.txt found in project'}), 404

    try:
        subprocess.run([PYTHON_CMD, '-m', 'pip', 'install', '--upgrade', 'pip'], 
                      capture_output=True, text=True, timeout=60, cwd=workspace)
        
        result = subprocess.run(
            [PYTHON_CMD, '-m', 'pip', 'install', '-r', req_file, '--no-cache-dir', '--prefer-binary'],
            capture_output=True, text=True, timeout=300, cwd=workspace
        )
        with open(script['log_file'], 'a') as f:
            f.write(f'\n--- Installing Requirements ---\n')
            f.write(result.stdout or '')
            if result.stderr:
                f.write(result.stderr)
            f.write(f'\n--- Done (exit {result.returncode}) ---\n\n')
        return jsonify({'success': result.returncode == 0,
                        'output': result.stdout,
                        'error': result.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'pip install timed out (5 min)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── TERMINAL COMMANDS ──
BLOCKED_PATTERNS = [
    r'\brm\s+-rf\s+/', r'\bsudo\b', r'\bmkfs\b',
    r'\bdd\s+if=', r':\(\)\s*\{', r'\bpasswd\b',
    r'\bchmod\s+777\s+/', r'\bshutdown\b', r'\breboot\b',
]

def is_dangerous(cmd: str) -> bool:
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return True
    return False

@app.route('/api/terminal/command', methods=['POST'])
@login_required
def run_command():
    data = request.json or {}
    command = data.get('command', '').strip()
    sid = data.get('script_id', '')

    if not command:
        return jsonify({'error': 'No command provided'}), 400
    if is_dangerous(command):
        return jsonify({'error': 'Command blocked for security reasons'}), 403

    cwd = get_script_workspace(sid) if sid and sid in scripts_db else UPLOAD_FOLDER

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60, cwd=cwd
        )
        return jsonify({
            'success': True,
            'output': result.stdout,
            'error': result.stderr,
            'return_code': result.returncode
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Command timed out (60s limit)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/terminal/install-package', methods=['POST'])
@login_required
def install_package():
    data = request.json or {}
    package = data.get('package', '').strip()
    if not package or re.search(r'[;&|`$]', package):
        return jsonify({'error': 'Invalid package name'}), 400
    try:
        result = subprocess.run(
            [PYTHON_CMD, '-m', 'pip', 'install', package, '--prefer-binary'],
            capture_output=True, text=True, timeout=180
        )
        return jsonify({'success': result.returncode == 0,
                        'output': result.stdout, 'error': result.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Installation timed out'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/terminal/list-packages')
@login_required
def list_packages():
    try:
        result = subprocess.run([PYTHON_CMD, '-m', 'pip', 'list', '--format=columns'],
                                capture_output=True, text=True, timeout=15)
        return jsonify({'success': True, 'packages': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── SYSTEM INFO ──
@app.route('/api/system/info')
@login_required
def system_info():
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(UPLOAD_FOLDER)
        return jsonify({
            'cpu_percent': cpu,
            'mem_percent': mem.percent,
            'mem_used_mb': round(mem.used / 1024 / 1024),
            'mem_total_mb': round(mem.total / 1024 / 1024),
            'disk_used_gb': round(disk.used / 1024**3, 2),
            'disk_total_gb': round(disk.total / 1024**3, 2),
            'disk_percent': disk.percent,
        })
    except ImportError:
        return jsonify({'error': 'psutil not installed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── FILE MANAGEMENT (global uploads) ──
def safe_path(base, rel):
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(os.path.realpath(base)):
        raise ValueError('Path traversal detected')
    return full

@app.route('/api/files/list')
@login_required
def list_files():
    path = request.args.get('path', '')
    try:
        base_path = safe_path(UPLOAD_FOLDER, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(base_path):
        return jsonify({'error': 'Path not found'}), 404

    try:
        items = []
        for item in sorted(os.listdir(base_path)):
            item_path = os.path.join(base_path, item)
            is_dir = os.path.isdir(item_path)
            stat = os.stat(item_path)
            items.append({
                'name': item,
                'path': os.path.join(path, item).lstrip('/') if path else item,
                'is_dir': is_dir,
                'size': stat.st_size if not is_dir else 0,
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                'extension': os.path.splitext(item)[1].lower() if not is_dir else ''
            })
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        return jsonify({'success': True, 'current_path': path, 'files': items})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/upload', methods=['POST'])
@login_required
def upload_file_global():
    path = request.form.get('path', '')
    try:
        target = safe_path(UPLOAD_FOLDER, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    os.makedirs(target, exist_ok=True)

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    uploaded = []
    for file in request.files.getlist('file'):
        if not file.filename:
            continue
        filename = secure_filename(file.filename)
        if not filename:
            continue
        file.save(os.path.join(target, filename))
        uploaded.append(filename)

    return jsonify({'success': True, 'uploaded': uploaded})

@app.route('/api/files/delete', methods=['POST'])
@login_required
def delete_file_global():
    data = request.json or {}
    path = data.get('path', '')
    try:
        full = safe_path(UPLOAD_FOLDER, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(full):
        return jsonify({'error': 'Not found'}), 404
    try:
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/rename', methods=['POST'])
@login_required
def rename_file_global():
    data = request.json or {}
    old_path = data.get('old_path', '')
    new_name = secure_filename(data.get('new_name', ''))
    if not new_name:
        return jsonify({'error': 'Invalid name'}), 400
    try:
        old_full = safe_path(UPLOAD_FOLDER, old_path)
        new_full = os.path.join(os.path.dirname(old_full), new_name)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(old_full):
        return jsonify({'error': 'Not found'}), 404
    try:
        os.rename(old_full, new_full)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════
#  MAIN - RAILWAY READY
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print('\n' + '='*60)
    print('🚀 ZXTCOZY Started on Railway!')
    print('='*60)
    print(f'   Port    : {PORT}')
    print(f'   Password: admin123')
    print(f'   Uploads : {UPLOAD_FOLDER}')
    print(f'   Logs    : {LOGS_FOLDER}')
    print(f'   Python  : {PYTHON_CMD}')
    print('='*60 + '\n')
    app.run(host='0.0.0.0', port=PORT, debug=False)
