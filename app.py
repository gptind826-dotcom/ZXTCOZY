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
app.secret_key = os.environ.get('SECRET_KEY', 'ZXTCOZY-super-secret-key-change-in-production-2024')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB for projects

# ── PATHS ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'servers')
LOGS_FOLDER = os.path.join(BASE_DIR, 'logs')
DATA_FILE = os.path.join(BASE_DIR, 'servers_db.json')

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)
os.makedirs("templates", exist_ok=True)

# ── IN-MEMORY STATE ──
running_processes = {}
servers_db = {}

# ── PERSIST / RESTORE DB ──
def save_db():
    try:
        safe = {}
        for sid, s in servers_db.items():
            safe[sid] = {k: v for k, v in s.items() if k not in ['process', 'master_fd', 'lock']}
        with open(DATA_FILE, 'w') as f:
            json.dump(safe, f, indent=2)
    except Exception as e:
        print(f'[DB] Save error: {e}')

def load_db():
    global servers_db
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        for sid, s in data.items():
            s['status'] = 'stopped'
            s['pid'] = None
            servers_db[sid] = s
        print(f'[DB] Loaded {len(servers_db)} servers')
    except Exception as e:
        print(f'[DB] Load error: {e}')

load_db()

# ── AUTH (Your credential system) ──
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── ROUTES ──
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

@app.route('/logout')
def logout():
    # Stop all running servers
    for sid, info in list(running_processes.items()):
        try:
            proc = info.get('process')
            if proc and proc.poll() is None:
                proc.terminate()
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
        except:
            pass
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/filemanager')
@login_required
def filemanager():
    return render_template('filemanager.html')

@app.route('/console/<server_id>')
@login_required
def console(server_id):
    if server_id not in servers_db:
        return redirect(url_for('dashboard'))
    return render_template('console.html', script_id=server_id)

# ══════════════════════════════════════════════════════════════════
#  SERVER UPLOAD - Supports .py and .zip (full projects)
# ══════════════════════════════════════════════════════════════════

def find_main_file(path):
    """Find the main Python file in a directory"""
    common_files = ["main.py", "app.py", "bot.py", "server.py", "index.py", "start.py", "run.py"]
    for filename in common_files:
        if os.path.exists(os.path.join(path, filename)):
            return filename
    
    # If no common file found, look for any Python file with __main__
    for root, dirs, files in os.walk(path):
        for file in files:
            if file.endswith('.py') and not file.startswith('_'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        if '__main__' in content or 'if __name__' in content:
                            return os.path.relpath(filepath, path)
                except:
                    continue
    return None

def install_requirements(path):
    """Install requirements.txt if exists"""
    req_file = os.path.join(path, "requirements.txt")
    if os.path.exists(req_file):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file],
                capture_output=True, text=True, timeout=300
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "Installation timed out"
        except Exception as e:
            return False, "", str(e)
    return True, "", "No requirements.txt found"

@app.route('/api/upload-script', methods=['POST'])
@login_required
def upload_script():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    
    filename = secure_filename(file.filename)
    server_id = uuid.uuid4().hex[:8]
    
    # Create server directory
    server_dir = os.path.join(UPLOAD_FOLDER, server_id)
    os.makedirs(server_dir, exist_ok=True)
    
    file_ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    
    if file_ext == 'zip':
        # Handle ZIP upload - extract entire project
        zip_path = os.path.join(server_dir, filename)
        file.save(zip_path)
        
        # Extract zip
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(server_dir)
            os.remove(zip_path)
            
            # Find main .py file
            main_file = find_main_file(server_dir)
            
            if not main_file:
                main_file = "server.py"
                with open(os.path.join(server_dir, main_file), 'w') as f:
                    f.write('''
from flask import Flask
import os

app = Flask(__name__)

@app.route('/')
def home():
    return '<h1>Server is Running!</h1><p>Upload your project files via File Manager.</p>'

@app.route('/health')
def health():
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
''')
            
            # Install requirements
            install_requirements(server_dir)
            
            project_name = filename.replace('.zip', '')
            server_name = f"{project_name}"
            
        except zipfile.BadZipFile:
            shutil.rmtree(server_dir)
            return jsonify({'error': 'Invalid zip file'}), 400
    else:
        # Single .py file upload
        file.save(os.path.join(server_dir, filename))
        main_file = filename
        server_name = filename
    
    servers_db[server_id] = {
        'id':          server_id,
        'name':        server_name,
        'path':        os.path.join(server_dir, main_file),
        'workspace':   server_dir,
        'status':      'stopped',
        'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'pid':         None,
        'log_file':    os.path.join(LOGS_FOLDER, f'{server_id}.log')
    }
    save_db()
    return jsonify({'success': True, 'script_id': server_id, 'name': server_name})

# ══════════════════════════════════════════════════════════════════
#  SERVER-SCOPED FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def get_server_workspace(server_id):
    if server_id not in servers_db:
        return None
    return servers_db[server_id]['workspace']

def safe_server_path(server_id, rel_path):
    workspace = get_server_workspace(server_id)
    if not workspace:
        raise ValueError('Server not found')
    if not rel_path:
        return workspace
    full = os.path.realpath(os.path.join(workspace, rel_path))
    if not full.startswith(os.path.realpath(workspace)):
        raise ValueError('Path traversal detected')
    return full

@app.route('/api/scripts/<server_id>/files/list')
@login_required
def server_list_files(server_id):
    path = request.args.get('path', '')
    try:
        base_path = safe_server_path(server_id, path)
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

@app.route('/api/scripts/<server_id>/files/upload', methods=['POST'])
@login_required
def server_upload_file(server_id):
    path = request.form.get('path', '')
    try:
        target = safe_server_path(server_id, path)
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

@app.route('/api/scripts/<server_id>/files/delete', methods=['POST'])
@login_required
def server_delete_file(server_id):
    data = request.json or {}
    path = data.get('path', '')
    try:
        full = safe_server_path(server_id, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(full):
        return jsonify({'error': 'Not found'}), 404
    try:
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<server_id>/files/rename', methods=['POST'])
@login_required
def server_rename_file(server_id):
    data = request.json or {}
    old_path = data.get('old_path', '')
    new_name = secure_filename(data.get('new_name', ''))
    if not new_name:
        return jsonify({'error': 'Invalid name'}), 400
    try:
        old_full = safe_server_path(server_id, old_path)
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

@app.route('/api/scripts/<server_id>/files/move', methods=['POST'])
@login_required
def server_move_file(server_id):
    data = request.json or {}
    try:
        src = safe_server_path(server_id, data.get('source', ''))
        dest = safe_server_path(server_id, os.path.join(data.get('destination', ''), os.path.basename(src)))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(src):
        return jsonify({'error': 'Source not found'}), 404
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        shutil.move(src, dest)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<server_id>/files/copy', methods=['POST'])
@login_required
def server_copy_file(server_id):
    data = request.json or {}
    try:
        src = safe_server_path(server_id, data.get('source', ''))
        dest = safe_server_path(server_id, os.path.join(data.get('destination', ''), os.path.basename(src)))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(src):
        return jsonify({'error': 'Source not found'}), 404
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        shutil.copytree(src, dest) if os.path.isdir(src) else shutil.copy2(src, dest)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<server_id>/files/create-folder', methods=['POST'])
@login_required
def server_create_folder(server_id):
    data = request.json or {}
    folder_name = secure_filename(data.get('folder_name', ''))
    if not folder_name:
        return jsonify({'error': 'Invalid folder name'}), 400
    try:
        full = safe_server_path(server_id, os.path.join(data.get('path', ''), folder_name))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    try:
        os.makedirs(full, exist_ok=True)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<server_id>/files/create-file', methods=['POST'])
@login_required
def server_create_file(server_id):
    data = request.json or {}
    filename = secure_filename(data.get('filename', ''))
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    try:
        full = safe_server_path(server_id, os.path.join(data.get('path', ''), filename))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    try:
        with open(full, 'w') as f:
            f.write(data.get('content', ''))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<server_id>/files/edit', methods=['GET', 'POST'])
@login_required
def server_edit_file(server_id):
    if request.method == 'GET':
        path = request.args.get('path', '')
        try:
            full = safe_server_path(server_id, path)
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
            full = safe_server_path(server_id, path)
        except ValueError:
            return jsonify({'error': 'Invalid path'}), 400
        try:
            with open(full, 'w', encoding='utf-8') as f:
                f.write(data.get('content', ''))
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@app.route('/api/scripts/<server_id>/files/zip', methods=['POST'])
@login_required
def server_zip_files(server_id):
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': 'No files selected'}), 400
    
    workspace = get_server_workspace(server_id)
    zip_name = f'archive_{uuid.uuid4().hex[:8]}.zip'
    zip_path = os.path.join(workspace, zip_name)
    
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                full = safe_server_path(server_id, p)
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
#  SERVER MANAGEMENT — PTY-BASED
# ══════════════════════════════════════════════════════════════════

def _pty_reader(server_id: str, master_fd: int):
    info = running_processes.get(server_id)
    server = servers_db.get(server_id)
    if not info or not server:
        return
    log_path = server['log_file']
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
                        if len(info['buffer']) > 200_000:
                            info['buffer'] = info['buffer'][-200_000:]
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
        print(f'[PTY reader {server_id}] error: {e}')
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if server_id in running_processes:
            running_processes[server_id]['master_fd'] = None
        if server_id in servers_db:
            servers_db[server_id]['status'] = 'stopped'
            servers_db[server_id]['pid'] = None
        save_db()

@app.route('/api/start/<server_id>', methods=['POST'])
@login_required
def start_server(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404
    server = servers_db[server_id]

    if not os.path.exists(server['path']):
        return jsonify({'error': 'Server file missing from disk'}), 404

    # Kill existing if any
    info = running_processes.get(server_id)
    if info:
        proc = info.get('process')
        if proc and proc.poll() is None:
            return jsonify({'error': 'Server is already running'}), 400
        else:
            running_processes.pop(server_id, None)

    with open(server['log_file'], 'ab') as f:
        header = f"\n{'='*60}\nStarted: {datetime.now()}\nServer: {server['name']}\n{'='*60}\n\n"
        f.write(header.encode())

    try:
        master_fd, slave_fd = pty.openpty()
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        winsize = struct.pack('HHHH', 24, 80, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        cwd = server['workspace']
        
        process = subprocess.Popen(
            [sys.executable, '-u', server['path']],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            close_fds=True,
            start_new_session=True
        )
        os.close(slave_fd)

        info = {
            'process': process,
            'master_fd': master_fd,
            'buffer': '',
            'lock': threading.Lock(),
        }
        running_processes[server_id] = info

        t = threading.Thread(target=_pty_reader, args=(server_id, master_fd), daemon=True)
        t.start()

        server['status'] = 'running'
        server['pid'] = process.pid
        server['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_db()

        return jsonify({'success': True, 'pid': process.pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stop/<server_id>', methods=['POST'])
@login_required
def stop_server_route(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404
    
    info = running_processes.pop(server_id, None)
    if info:
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
    
    if server_id in servers_db:
        servers_db[server_id]['status'] = 'stopped'
        servers_db[server_id]['pid'] = None
    
    with open(servers_db[server_id]['log_file'], 'ab') as f:
        f.write(f"\n{'='*60}\nStopped: {datetime.now()}\n{'='*60}\n\n".encode())
    
    save_db()
    return jsonify({'success': True})

@app.route('/api/delete-script/<server_id>', methods=['DELETE'])
@login_required
def delete_server_route(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404
    
    info = running_processes.pop(server_id, None)
    if info:
        proc = info.get('process')
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except:
                pass
    
    server = servers_db.pop(server_id)
    workspace = server.get('workspace')
    if workspace and os.path.exists(workspace):
        try:
            shutil.rmtree(workspace)
        except Exception as e:
            print(f"Error deleting workspace: {e}")
    log_file = server.get('log_file')
    if log_file and os.path.exists(log_file):
        try:
            os.remove(log_file)
        except Exception:
            pass
    save_db()
    return jsonify({'success': True})

@app.route('/api/scripts')
@login_required
def get_servers():
    for sid, server in servers_db.items():
        info = running_processes.get(sid)
        if info:
            proc = info.get('process')
            if proc and proc.poll() is None:
                server['status'] = 'running'
            else:
                server['status'] = 'stopped'
                server['pid'] = None
                running_processes.pop(sid, None)
        else:
            server['status'] = 'stopped'
            server['pid'] = None
    return jsonify(list(servers_db.values()))

# ── LOGS ──
@app.route('/api/logs/<server_id>')
@login_required
def get_logs(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404

    info = running_processes.get(server_id)
    if info:
        with info['lock']:
            buf = info['buffer']
        return jsonify({'logs': buf, 'live': True})

    log_file = servers_db[server_id]['log_file']
    if not os.path.exists(log_file):
        return jsonify({'logs': '', 'live': False})
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        if len(content) > 100_000:
            content = '...(truncated)...\n' + content[-100_000:]
        return jsonify({'logs': content, 'live': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs/stream/<server_id>')
@login_required
def stream_logs(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404

    def generate():
        sent = 0
        yield 'retry: 1000\n\n'
        while True:
            info = running_processes.get(server_id)
            if info:
                with info['lock']:
                    buf = info['buffer']
                if len(buf) > sent:
                    chunk = buf[sent:]
                    sent = len(buf)
                    for line in chunk.splitlines():
                        yield f'data: {json.dumps(line)}\n\n'
            server = servers_db.get(server_id)
            if server and server['status'] == 'stopped' and server_id not in running_processes:
                yield 'event: stopped\ndata: {}\n\n'
                break
            time.sleep(0.3)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ── STDIN ──
@app.route('/api/stdin/<server_id>', methods=['POST'])
@login_required
def send_stdin(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404
    info = running_processes.get(server_id)
    if not info:
        return jsonify({'error': 'Server is not running'}), 400
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
@app.route('/api/install-requirements/<server_id>', methods=['POST'])
@login_required
def install_requirements_route(server_id):
    if server_id not in servers_db:
        return jsonify({'error': 'Server not found'}), 404
    server = servers_db[server_id]
    workspace = server['workspace']
    
    success, stdout, stderr = install_requirements(workspace)
    
    with open(server['log_file'], 'a') as f:
        f.write(f'\n--- Installing Requirements ---\n')
        f.write(stdout or '')
        if stderr:
            f.write(stderr)
        f.write(f'\n--- Done ---\n\n')
    
    return jsonify({'success': success, 'output': stdout, 'error': stderr})

# ══════════════════════════════════════════════════════════════════
#  TERMINAL COMMAND API
# ══════════════════════════════════════════════════════════════════

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

    cwd = servers_db[sid]['workspace'] if sid and sid in servers_db else UPLOAD_FOLDER

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
            [sys.executable, '-m', 'pip', 'install', package],
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
        result = subprocess.run([sys.executable, '-m', 'pip', 'list', '--format=columns'],
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

# ── GLOBAL FILE MANAGEMENT (for dashboard stats) ──
def safe_path(base, rel):
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(os.path.realpath(base)):
        raise ValueError('Path traversal detected')
    return full

@app.route('/api/files/list')
@login_required
def list_files_global():
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

# ── HEALTH CHECK for Railway ──
@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy',
        'timestamp': str(datetime.now()),
        'servers': len(servers_db),
        'running': len(running_processes)
    })

if __name__ == '__main__':
    # Create templates folder if needed
    os.makedirs("templates", exist_ok=True)
    
    # Get port from environment (Railway sets PORT automatically)
    port = int(os.environ.get('PORT', 5000))
    
    print('\n' + '='*60)
    print('🚀  ZXTCOZY  Server Hosting Platform Started!')
    print('='*60)
    print(f'   URL      : http://0.0.0.0:{port}')
    print(f'   Password : admin123')
    print(f'   Servers  : {UPLOAD_FOLDER}')
    print(f'   Logs     : {LOGS_FOLDER}')
    print('='*60 + '\n')
    
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
