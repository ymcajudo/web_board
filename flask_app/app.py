# MariaDB with Connection Pool - 연결 안정성 개선
# MariaDB, Flask App with Custom Connection Pool - mysql-connector-python 사용
# 다중 파일 업로드 지원 추가

from flask import Flask, request, redirect, url_for, render_template, flash, send_from_directory, session, jsonify
import mysql.connector
from mysql.connector import pooling
import logging
import os
import uuid
import json
from datetime import datetime
import pytz
from contextlib import contextmanager
import time
import threading
from queue import Queue, Empty
import weakref
import atexit
import signal
import sys

app = Flask(__name__)
app.secret_key = 'new1234!'
app.config['UPLOAD_FOLDER'] = '/mnt/test'

# ZIP 파일 최대 크기 설정 (100MiB)
MAX_ZIP_SIZE = 100 * 1024 * 1024  # 100MiB in bytes

# 로깅 설정
logging.basicConfig(level=logging.DEBUG)

class ConnectionPool:
    """Custom MariaDB/MySQL Connection Pool"""

    def __init__(self, max_connections=10, **connection_kwargs):
        self.max_connections = max_connections
        self.connection_kwargs = connection_kwargs
        self.pool = Queue(maxsize=max_connections)
        self.active_connections = 0
        self.lock = threading.Lock()
        self._closed = False

        # 초기 연결 생성
        self._initialize_pool()

    def _initialize_pool(self):
        """연결 풀 초기화"""
        try:
            for _ in range(self.max_connections):
                conn = self._create_connection()
                if conn:
                    self.pool.put(conn)
                    self.active_connections += 1
            app.logger.info(f"Connection pool initialized with {self.active_connections} connections")
        except Exception as e:
            app.logger.error(f"Failed to initialize connection pool: {e}")

    def _create_connection(self):
        """새로운 데이터베이스 연결 생성"""
        try:
            connection = mysql.connector.connect(**self.connection_kwargs)
            connection.autocommit = False
            return connection
        except Exception as e:
            app.logger.error(f"Failed to create database connection: {e}")
            return None

    def get_connection(self, timeout=30):
        """연결 풀에서 연결 가져오기"""
        if self._closed:
            raise Exception("Connection pool is closed")

        try:
            # 풀에서 연결 가져오기
            conn = self.pool.get(timeout=timeout)

            # 연결 상태 확인
            if not self._is_connection_alive(conn):
                app.logger.warning("Dead connection found, creating new one")
                conn.close()
                conn = self._create_connection()
                if not conn:
                    raise Exception("Failed to create new connection")

            return conn

        except Empty:
            # 풀이 비어있으면 새 연결 생성 시도
            with self.lock:
                if self.active_connections < self.max_connections:
                    conn = self._create_connection()
                    if conn:
                        self.active_connections += 1
                        return conn

            raise Exception("Connection pool exhausted and cannot create new connection")

    def return_connection(self, conn):
        """연결을 풀에 반환"""
        if self._closed:
            try:
                conn.close()
            except:
                pass
            return

        try:
            if self._is_connection_alive(conn):
                # 트랜잭션 롤백 및 초기화
                conn.rollback()
                self.pool.put_nowait(conn)
            else:
                # 죽은 연결은 닫고 새 연결 생성
                conn.close()
                new_conn = self._create_connection()
                if new_conn:
                    self.pool.put_nowait(new_conn)
                else:
                    with self.lock:
                        self.active_connections -= 1

        except Exception as e:
            app.logger.error(f"Error returning connection to pool: {e}")
            try:
                conn.close()
            except:
                pass

    def _is_connection_alive(self, conn):
        """연결이 살아있는지 확인"""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return True
        except:
            return False

    def close_all(self):
        """모든 연결 닫기"""
        self._closed = True
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
                conn.close()
            except:
                pass
        self.active_connections = 0
        app.logger.info("Connection pool closed")

# 데이터베이스 연결 풀 전역 변수
db_pool = None

def init_db_pool():
    """데이터베이스 연결 풀 초기화"""
    global db_pool
    try:
        # 기존 풀이 있다면 닫기
        if db_pool is not None:
            db_pool.close_all()

        connection_config = {
            'host': "dbnas",  # MariaDB 서버
            'port': 3306,
            'user': "board_user",
            'password': "new1234!",
            'database': "board_db",
            'connection_timeout': 60,
            'charset': 'utf8mb4',
            'collation': 'utf8mb4_unicode_ci',
            'use_unicode': True,
            'autocommit': False
        }

        db_pool = ConnectionPool(max_connections=10, **connection_config)
        app.logger.info("Database connection pool initialized successfully")

    except Exception as e:
        app.logger.error(f"Database connection pool initialization failed: {e}")
        db_pool = None

@contextmanager
def get_db_connection():
    """표준 MySQL 커넥션 풀 사용"""
    connection = None
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            connection = db_pool.get_connection()  # mysql.connector.pooling 사용
            app.logger.debug(f"Database connection acquired (attempt {attempt + 1})")
            break
        except Exception as e:
            app.logger.error(f"Database connection error (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                raise Exception(f"Failed to establish database connection after {max_retries} attempts: {e}")
            time.sleep(retry_delay)
            retry_delay *= 2
    
    try:
        yield connection
    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except:
                pass
        app.logger.error(f"Database operation error: {e}")
        raise
    finally:
        if connection:
            try:
                db_pool.return_connection(connection)
            except Exception as close_error:
                app.logger.warning(f"Error closing connection: {close_error}")

def load_identity():
    """identity.json 파일에서 사용자 정보를 읽어옴"""
    try:
        with open('identity.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        app.logger.error("identity.json file not found")
        return {}
    except json.JSONDecodeError:
        app.logger.error("Invalid JSON format in identity.json")
        return {}

def is_logged_in():
    """사용자가 로그인되어 있는지 확인"""
    return 'logged_in' in session and session['logged_in']

def login_required(f):
    """로그인이 필요한 페이지에 사용할 데코레이터"""
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            flash("로그인이 필요합니다.")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def convert_to_kst(utc_time):
    utc = pytz.utc
    kst = pytz.timezone('Asia/Seoul')
    utc_dt = utc.localize(utc_time)
    kst_dt = utc_dt.astimezone(kst)
    return kst_dt.strftime('%Y-%m-%d %H:%M:%S')

# 데이터베이스 연결 풀 초기화 함수 (앱 시작 시 한 번만 실행)
def ensure_db_pool():
    """데이터베이스 연결 풀이 초기화되었는지 확인하고 필요시 초기화"""
    global db_pool
    if db_pool is None or db_pool._closed:
        app.logger.info("Initializing database connection pool...")
        init_db_pool()

def allowed_file(filename):
    return filename and '.' in filename and filename != '.'

def get_file_size(file_obj):
    """파일 객체의 크기를 바이트 단위로 반환"""
    file_obj.seek(0, 2)  # 파일 끝으로 이동
    size = file_obj.tell()  # 현재 위치(파일 크기) 반환
    file_obj.seek(0)  # 파일 시작으로 돌아가기
    return size

def format_file_size(size_bytes):
    """바이트를 사람이 읽기 쉬운 형태로 변환"""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes}B"

@app.route('/check_file_size', methods=['POST'])
@login_required
def check_file_size():
    """파일 크기 확인 API"""
    files = request.files.getlist('files[]')
    
    if not files or all(file.filename == '' for file in files):
        return jsonify({'error': 'No files selected'}), 400

    for file in files:
        if file.filename == '':
            continue
            
        if not allowed_file(file.filename):
            return jsonify({'error': f'File type not allowed: {file.filename}'}), 400

        # ZIP 파일인 경우 크기 확인
        if file.filename.lower().endswith('.zip'):
            file_size = get_file_size(file)
            if file_size > MAX_ZIP_SIZE:
                return jsonify({
                    'error': 'File too large',
                    'message': f'ZIP 파일 크기가 100MB를 초과합니다: {file.filename} (현재 크기: {format_file_size(file_size)})',
                    'max_size': format_file_size(MAX_ZIP_SIZE),
                    'current_size': format_file_size(file_size)
                }), 413

    return jsonify({'success': True}), 200

def get_post_files(post_id):
    """게시글의 첨부 파일 목록을 가져옴"""
    try:
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, file_name, original_file_name, file_size 
                FROM post_files 
                WHERE post_id = %s 
                ORDER BY id
            """, (post_id,))
            result = cursor.fetchall()
            cursor.close()
            return result
    except Exception as e:
        app.logger.error(f"Error fetching files for post {post_id}: {e}")
        return []

def save_post_files(post_id, files, db):
    """게시글의 첨부 파일들을 저장 (db.commit은 하지 않음)"""
    saved_files = []

    cursor = db.cursor()
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            original_filename = file.filename

            # ZIP 파일인 경우 크기 확인
            if file.filename.lower().endswith('.zip'):
                file_size = get_file_size(file)
                if file_size > MAX_ZIP_SIZE:
                    raise ValueError(f"ZIP 파일 크기가 100MB를 초과합니다: {file.filename}")
            else:
                file_size = get_file_size(file)

            # 고유한 파일명 생성
            unique_filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]

            try:
                # 파일 저장
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(file_path)

                # 데이터베이스에 파일 정보 저장 (commit은 하지 않음)
                cursor.execute("""
                    INSERT INTO post_files (post_id, file_name, original_file_name, file_size)
                    VALUES (%s, %s, %s, %s)
                """, (post_id, unique_filename, original_filename, file_size))

                saved_files.append({
                    'file_name': unique_filename,
                    'original_file_name': original_filename,
                    'file_size': file_size
                })

                app.logger.info(f"File saved: {unique_filename} (original: {original_filename})")

            except Exception as file_error:
                app.logger.error(f"Failed to save file {original_filename}: {file_error}")
                # 실패한 파일이 있으면 이미 저장된 파일들 정리
                for saved_file in saved_files:
                    try:
                        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], saved_file['file_name']))
                    except:
                        pass
                raise file_error

    cursor.close()
    return saved_files

def delete_post_files(post_id):
    """게시글의 모든 첨부 파일을 삭제"""
    try:
        # 먼저 파일 목록을 가져옴
        files = get_post_files(post_id)
        
        # 파일 시스템에서 파일 삭제
        for file_info in files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_info['file_name'])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    app.logger.info(f"File deleted: {file_path}")
                except Exception as file_error:
                    app.logger.warning(f"Failed to delete file {file_path}: {file_error}")
        
        # 데이터베이스에서 파일 정보 삭제
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("DELETE FROM post_files WHERE post_id = %s", (post_id,))
            db.commit()
            cursor.close()
                
    except Exception as e:
        app.logger.error(f"Error deleting files for post {post_id}: {e}")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # identity.json에서 사용자 정보 확인
        identity_data = load_identity()

        if username in identity_data and identity_data[username]['password'] == password:
            session['logged_in'] = True
            session['username'] = username
            flash(f"{username}님, 로그인되었습니다.")
            return redirect(url_for('index'))
        else:
            flash("아이디 또는 비밀번호를 다시 확인해주세요.")
            return render_template('login.html')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("로그아웃되었습니다.")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    try:
        ensure_db_pool()  # 데이터베이스 연결 풀 확인

        page = request.args.get('page', 1, type=int)
        per_page = 10
        offset = (page - 1) * per_page

        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            
            # 게시글 총 개수 조회
            cursor.execute("SELECT COUNT(*) as count FROM posts")
            total_posts = cursor.fetchone()['count']
            total_pages = (total_posts + per_page - 1) // per_page if total_posts > 0 else 1

            # 게시글 목록 조회 (파일 개수 포함)
            cursor.execute("""
                SELECT p.id, p.title, p.content, p.created_at,
                       COUNT(pf.id) as file_count
                FROM posts p
                LEFT JOIN post_files pf ON p.id = pf.post_id
                GROUP BY p.id, p.title, p.content, p.created_at
                ORDER BY p.created_at DESC
                LIMIT %s OFFSET %s
            """, (per_page, offset))
            posts = cursor.fetchall()
            cursor.close()

            # 게시글 번호 및 시간 변환
            for i, post in enumerate(posts):
                post['created_at'] = convert_to_kst(post['created_at'])
                post['seq'] = total_posts - (offset + i)

                # 제목 일부만 표시 (앞 20글자)
                title = post.get('title') or ''
                post['short_title'] = title[:15] + ('...' if len(title) > 15 else '')

                 # 내용 앞 20자만 표시
                content = post.get('content') or ''
                post['short_content'] = content[:25] + ('...' if len(content) > 25 else '')

        app.logger.info(f"Successfully loaded {len(posts)} posts for page {page}")
        return render_template('index.html', posts=posts, page=page, total_pages=total_pages)

    except Exception as e:
        app.logger.error(f"Error fetching posts: {e}")
        flash("게시글을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return render_template('index.html', posts=[], page=1, total_pages=1)

@app.route('/delete/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    try:
        ensure_db_pool()  # 데이터베이스 연결 풀 확인
        
        # 삭제 전 게시글 존재 확인
        post = None
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
            post = cursor.fetchone()
            cursor.close()
                
            if not post:
                app.logger.warning(f"Post {post_id} not found for deletion")
                flash("삭제할 게시글을 찾을 수 없습니다.")
                return redirect(url_for('index'))
                
            app.logger.info(f"Found post {post_id} for deletion: {post['title']}")
        
        # 첨부 파일들 삭제
        delete_post_files(post_id)
        
        # 데이터베이스에서 게시글 삭제
        with get_db_connection() as db:
            cursor = db.cursor()
            
            # 삭제 전 카운트 확인
            cursor.execute("SELECT COUNT(*) as count FROM posts WHERE id=%s", (post_id,))
            before_count = cursor.fetchone()[0]
            app.logger.info(f"Posts count before deletion: {before_count}")
            
            # 삭제 실행
            cursor.execute("DELETE FROM posts WHERE id=%s", (post_id,))
            deleted_rows = cursor.rowcount
            app.logger.info(f"DELETE query executed, affected rows: {deleted_rows}")
            
            if deleted_rows > 0:
                db.commit()
                app.logger.info(f"Transaction committed for post {post_id}")
                flash("게시글이 성공적으로 삭제되었습니다.")
            else:
                db.rollback()
                app.logger.warning(f"No rows affected, rolling back transaction for post {post_id}")
                flash("게시글 삭제에 실패했습니다.")
            
            cursor.close()
        
    except Exception as e:
        app.logger.error(f"Error deleting post {post_id}: {e}")
        flash(f"게시글 삭제 중 오류가 발생했습니다: {str(e)}")
    
    return redirect(url_for('index'))

@app.route('/post/<int:post_id>')
@login_required
def post(post_id):
    try:
        ensure_db_pool()  # 데이터베이스 연결 풀 확인

        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
            post = cursor.fetchone()
            cursor.close()

            if post:
                post['created_at'] = convert_to_kst(post['created_at'])
                # 첨부 파일 목록 가져오기
                post['files'] = get_post_files(post_id)
                return render_template('post.html', post=post)
            else:
                flash("게시글을 찾을 수 없습니다.")
                return redirect(url_for('index'))

    except Exception as e:
        app.logger.error(f"Error fetching post {post_id}: {e}")
        flash("게시글을 가져오는 중 오류가 발생했습니다.")
        return redirect(url_for('index'))

@app.route('/edit/<int:post_id>', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    """게시글 수정 기능"""
    if request.method == 'GET':
        # 수정 페이지 표시
        try:
            ensure_db_pool()

            with get_db_connection() as db:
                cursor = db.cursor(dictionary=True)
                cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                cursor.close()

                if post:
                    # 첨부 파일 목록 가져오기
                    post['files'] = get_post_files(post_id)
                    return render_template('edit_post.html', post=post)
                else:
                    flash("게시글을 찾을 수 없습니다.")
                    return redirect(url_for('index'))

        except Exception as e:
            app.logger.error(f"Error fetching post for edit {post_id}: {e}")
            flash("게시글을 가져오는 중 오류가 발생했습니다.")
            return redirect(url_for('index'))

    elif request.method == 'POST':
        # 게시글 수정 처리
        try:
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '')
            files = request.files.getlist('files[]')
            keep_files = request.form.getlist('keep_files')  # 유지할 기존 파일들의 ID

            app.logger.info(f"Edit post {post_id} - title: {title}")
            app.logger.info(f"Keep files: {keep_files}")
            app.logger.info(f"New files count: {len([f for f in files if f.filename])}")

            # 제목 유효성 검사
            if not title:
                flash("제목을 입력하세요.")
                # 기존 게시글 정보를 다시 가져와서 폼에 표시
                with get_db_connection() as db:
                    cursor = db.cursor(dictionary=True)
                    cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                    post = cursor.fetchone()
                    cursor.close()
                    
                    if post:
                        post['files'] = get_post_files(post_id)
                        return render_template('edit_post.html', post=post)
                    else:
                        flash("게시글을 찾을 수 없습니다.")
                        return redirect(url_for('index'))

            ensure_db_pool()

            with get_db_connection() as db:
                try:
                    cursor = db.cursor(dictionary=True)
                    
                    # 기존 게시글 정보 조회
                    cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                    existing_post = cursor.fetchone()

                    if not existing_post:
                        cursor.close()
                        flash("게시글을 찾을 수 없습니다.")
                        return redirect(url_for('index'))

                    # 기존 파일들 중 삭제할 파일들 처리
                    existing_files = get_post_files(post_id)
                    app.logger.info(f"Existing files: {[f['id'] for f in existing_files]}")
                    
                    files_to_delete = []
                    for file_info in existing_files:
                        if str(file_info['id']) not in keep_files:
                            files_to_delete.append(file_info)
                    
                    app.logger.info(f"Files to delete: {[f['id'] for f in files_to_delete]}")
                    
                    # 삭제할 파일들 처리
                    for file_info in files_to_delete:
                        # 파일 시스템에서 파일 삭제
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_info['file_name'])
                        if os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                app.logger.info(f"Old file deleted: {file_path}")
                            except Exception as file_error:
                                app.logger.warning(f"Failed to delete old file {file_path}: {file_error}")
                        
                        # 데이터베이스에서 파일 정보 삭제
                        cursor.execute("DELETE FROM post_files WHERE id = %s", (file_info['id'],))
                        app.logger.info(f"File record deleted from DB: {file_info['id']}")

                    # 새 파일들 저장 (파일명이 있는 파일만)
                    new_files = [f for f in files if f and f.filename and f.filename.strip()]
                    if new_files:
                        app.logger.info(f"Saving {len(new_files)} new files")
                        try:
                            saved_files = save_post_files(post_id, new_files, db)
                            app.logger.info(f"Successfully saved {len(saved_files)} new files")
                        except Exception as file_error:
                            app.logger.error(f"Failed to save new files: {file_error}")
                            flash(f"파일 저장 중 오류가 발생했습니다: {str(file_error)}")
                            db.rollback()
                            cursor.close()
                            # 오류 시 기존 데이터 다시 로드
                            existing_post['files'] = get_post_files(post_id)
                            return render_template('edit_post.html', post=existing_post)

                    # 게시글 업데이트
                    cursor.execute("""
                        UPDATE posts 
                        SET title=%s, content=%s
                        WHERE id=%s
                    """, (title, content, post_id))
                    
                    affected_rows = cursor.rowcount
                    app.logger.info(f"Post update affected rows: {affected_rows}")
                    
                    if affected_rows >= 0:  # 0도 정상임 (내용이 동일할 경우)
                        db.commit()
                        flash("게시글이 성공적으로 수정되었습니다.")
                        app.logger.info(f"Post {post_id} updated successfully")
                        cursor.close()
                        return redirect(url_for('post', post_id=post_id))
                    else:
                        db.rollback()
                        flash("게시글 수정에 실패했습니다.")
                        cursor.close()
                        existing_post['files'] = get_post_files(post_id)
                        return render_template('edit_post.html', post=existing_post)

                except Exception as db_error:
                    app.logger.error(f"Database error during post update: {db_error}")
                    try:
                        db.rollback()
                        cursor.close()
                    except:
                        pass
                    raise db_error

        except Exception as e:
            app.logger.error(f"Error updating post {post_id}: {e}")
            flash(f"게시글 수정 중 오류가 발생했습니다: {str(e)}")
            
            # 오류 발생 시 기존 게시글 정보를 다시 가져와서 폼에 표시
            try:
                with get_db_connection() as db:
                    cursor = db.cursor(dictionary=True)
                    cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                    post = cursor.fetchone()
                    cursor.close()
                    
                    if post:
                        post['files'] = get_post_files(post_id)
                        return render_template('edit_post.html', post=post)
            except:
                pass
            
            return redirect(url_for('index'))

@app.route('/new', methods=['GET', 'POST'])
@login_required
def new_post():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        files = request.files.getlist('files[]')
        
        try:
            ensure_db_pool() # 데이터베이스 연결 풀 확인
            
            with get_db_connection() as db:
                cursor = db.cursor()
                
                # 게시글 먼저 저장
                cursor.execute("""
                    INSERT INTO posts (title, content)
                    VALUES (%s, %s)
                """, (title, content))
                
                # 방금 생성한 게시글의 ID 가져오기
                post_id = cursor.lastrowid
                
                # 파일 저장 (commit은 여기서 하지 않음)
                if files and any(file.filename for file in files):
                    save_post_files(post_id, files, db)
                
                # 최종적으로 한 번만 commit
                db.commit()
                cursor.close()
                
            flash("게시글이 등록되었습니다.")
            app.logger.info("New post created successfully")
            return redirect(url_for('index'))
        
        except Exception as e:
            app.logger.error(f"Error inserting post: {e}")
            flash(f"게시글 등록 중 오류가 발생했습니다: {str(e)}")
            return redirect(url_for('new_post'))
        
    return render_template('new_post.html')

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/download/<int:file_id>')
@login_required
def download_file(file_id):
    try:
        ensure_db_pool()  # 데이터베이스 연결 풀 확인

        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT file_name, original_file_name FROM post_files WHERE id=%s", (file_id,))
            file_info = cursor.fetchone()
            cursor.close()

            if file_info and file_info['file_name']:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_info['file_name'])
                if os.path.exists(file_path):
                    return send_from_directory(
                        app.config['UPLOAD_FOLDER'],
                        file_info['file_name'],
                        as_attachment=True,
                        download_name=file_info['original_file_name']
                    )
                else:
                    flash("파일이 존재하지 않습니다.")
                    return redirect(url_for('index'))
            else:
                flash("파일을 찾을 수 없습니다.")
                return redirect(url_for('index'))

    except Exception as e:
        app.logger.error(f"Error downloading file {file_id}: {e}")
        flash("파일 다운로드 중 오류가 발생했습니다.")
        return redirect(url_for('index'))

# 헬스체크 엔드포인트 추가
@app.route('/health')
def health_check():
    """애플리케이션과 데이터베이스 상태 확인"""
    try:
        ensure_db_pool()  # 데이터베이스 연결 풀 확인

        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
        return {"status": "healthy", "database": "connected"}, 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}, 500

# 애플리케이션 종료 시 연결 풀 정리를 위한 핸들러
def cleanup_db_pool():
    """애플리케이션 종료 시 데이터베이스 연결 풀 정리"""
    global db_pool
    if db_pool:
        app.logger.info("Cleaning up database connection pool")
        db_pool.close_all()
        db_pool = None

# 시그널 핸들러 등록
def signal_handler(signum, frame):
    """시그널 핸들러 - 우아한 종료"""
    app.logger.info(f"Received signal {signum}, shutting down gracefully...")
    cleanup_db_pool()
    sys.exit(0)

# 시그널 핸들러 등록
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# atexit 핸들러 등록
atexit.register(cleanup_db_pool)

if __name__ == '__main__':
    try:
        # 애플리케이션 시작 시 연결 풀 초기화
        init_db_pool()
        app.run(host='0.0.0.0', port=5000, debug=True)
    except KeyboardInterrupt:
        app.logger.info("Application interrupted by user")
    except Exception as e:
        app.logger.error(f"Application error: {e}")
    finally:
        # 프로그램 종료 시 연결 풀 정리
        cleanup_db_pool()