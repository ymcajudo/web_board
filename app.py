from flask import Flask, request, redirect, url_for, render_template, flash, send_from_directory, session
import pymysql
import logging
import os
import uuid
import json
from datetime import datetime
import pytz
import threading
import time
from contextlib import contextmanager
from queue import Queue, Empty, Full

app = Flask(__name__)
app.secret_key = 'new1234!'  
app.config['UPLOAD_FOLDER'] = '/mnt/test'

# 로깅 설정
logging.basicConfig(level=logging.DEBUG)

class ConnectionPool:
    def __init__(self, **db_config):
        self.db_config = db_config
        self.pool = Queue(maxsize=db_config.get('max_connections', 10))
        self.min_connections = db_config.get('min_connections', 2)
        self.max_connections = db_config.get('max_connections', 10)
        self.lock = threading.Lock()
        self.created_connections = 0
        
        # 최소 연결 수만큼 미리 생성
        for _ in range(self.min_connections):
            conn = self._create_connection()
            if conn:
                self.pool.put(conn)
    
    def _create_connection(self):
        """새로운 데이터베이스 연결 생성"""
        try:
            conn = pymysql.connect(
                host=self.db_config['host'],
                user=self.db_config['user'],
                password=self.db_config['password'],
                database=self.db_config['database'],
                charset=self.db_config.get('charset', 'utf8mb4'),
                autocommit=self.db_config.get('autocommit', False),
                cursorclass=self.db_config.get('cursorclass', pymysql.cursors.DictCursor),
                connect_timeout=self.db_config.get('connect_timeout', 10),
                read_timeout=self.db_config.get('read_timeout', 30),
                write_timeout=self.db_config.get('write_timeout', 30)
            )
            with self.lock:
                self.created_connections += 1
            app.logger.debug(f"Created new database connection. Total: {self.created_connections}")
            return conn
        except Exception as e:
            app.logger.error(f"Failed to create database connection: {e}")
            return None
    
    def get_connection(self):
        """연결 풀에서 연결 가져오기"""
        try:
            # 풀에서 연결 시도
            conn = self.pool.get(timeout=5)
            
            # 연결 상태 확인
            if self._is_connection_alive(conn):
                return conn
            else:
                # 연결이 끊어진 경우 새로 생성
                app.logger.warning("Connection is dead, creating new one")
                conn.close()
                with self.lock:
                    self.created_connections -= 1
                new_conn = self._create_connection()
                if new_conn:
                    return new_conn
                else:
                    raise Exception("Failed to create new connection")
                    
        except Empty:
            # 풀이 비어있는 경우 새 연결 생성
            if self.created_connections < self.max_connections:
                app.logger.info("Pool empty, creating new connection")
                conn = self._create_connection()
                if conn:
                    return conn
            raise Exception("Connection pool exhausted")
    
    def return_connection(self, conn):
        """연결을 풀에 반환"""
        if conn and self._is_connection_alive(conn):
            try:
                self.pool.put(conn, timeout=1)
            except Full:
                # 풀이 가득 찬 경우 연결 닫기
                conn.close()
                with self.lock:
                    self.created_connections -= 1
        else:
            # 연결이 끊어진 경우 닫기
            if conn:
                conn.close()
                with self.lock:
                    self.created_connections -= 1
    
    def _is_connection_alive(self, conn):
        """연결 상태 확인"""
        try:
            conn.ping(reconnect=False)
            return True
        except:
            return False
    
    def close_all(self):
        """모든 연결 닫기"""
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
                conn.close()
            except Empty:
                break
        with self.lock:
            self.created_connections = 0

# 데이터베이스 연결 풀 전역 변수
db_pool = None

def init_db_pool():
    """데이터베이스 연결 풀 초기화"""
    global db_pool
    try:
        db_config = {
            'host': "dbnas",       # was 서버 host 파일에 10.0.1.30 db 라고 등록시
            #'host': "10.0.1.30",  # DB server
            'user': "board_user",
            'password': "new1234!",
            'database': "board_db",
            'charset': 'utf8mb4',
            'autocommit': False,
            'cursorclass': pymysql.cursors.DictCursor,
            'connect_timeout': 10,
            'read_timeout': 30,
            'write_timeout': 30,
            'max_connections': 10,
            'min_connections': 2
        }
        db_pool = ConnectionPool(**db_config)
        app.logger.info("Database connection pool initialized successfully")
    except Exception as e:
        app.logger.error(f"Database connection pool initialization failed: {e}")
        db_pool = None

@contextmanager
def get_db_connection():
    """데이터베이스 연결을 안전하게 가져오고 반환하는 컨텍스트 매니저"""
    connection = None
    try:
        if db_pool is None:
            init_db_pool()
        
        connection = db_pool.get_connection()
        yield connection
    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except:
                pass
        app.logger.error(f"Database connection error: {e}")
        raise
    finally:
        if connection and db_pool:
            db_pool.return_connection(connection)

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

@app.before_request
def before_request():
    """모든 요청 전에 연결 풀 초기화 확인"""
    if db_pool is None:
        init_db_pool()

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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
        page = request.args.get('page', 1, type=int)
        per_page = 5
        offset = (page - 1) * per_page

        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) as count FROM posts")
                total_posts = cursor.fetchone()['count']
                total_pages = (total_posts + per_page - 1) // per_page

                cursor.execute("SELECT id, title, content, file_name, created_at FROM posts ORDER BY created_at DESC LIMIT %s OFFSET %s", (per_page, offset))
                posts = cursor.fetchall()

                for i, post in enumerate(posts, start=1):
                    post['created_at'] = convert_to_kst(post['created_at'])
                    post['seq'] = total_posts - (offset + i) + 1

        return render_template('index.html', posts=posts, page=page, total_pages=total_pages)
    except Exception as e:
        app.logger.error(f"Error fetching posts: {e}")
        flash("게시글을 가져오는 중 오류가 발생했습니다.")
        return render_template('index.html', posts=[], page=1, total_pages=1)

@app.route('/delete/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT file_name FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                if post and post['file_name']:
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], post['file_name'])
                    if os.path.exists(file_path):
                        os.remove(file_path)

                cursor.execute("DELETE FROM posts WHERE id=%s", (post_id,))
                db.commit()
        flash("게시글이 삭제되었습니다.")
    except Exception as e:
        app.logger.error(f"Error deleting post: {e}")
        flash("게시글 삭제 중 오류가 발생했습니다.")
    return redirect(url_for('index'))

@app.route('/post/<int:post_id>')
@login_required
def post(post_id):
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                if post:
                    post['created_at'] = convert_to_kst(post['created_at'])
                    return render_template('post.html', post=post)
                else:
                    flash("게시글을 찾을 수 없습니다.")
                    return redirect(url_for('index'))
    except Exception as e:
        app.logger.error(f"Error fetching post: {e}")
        flash("게시글을 가져오는 중 오류가 발생했습니다.")
        return redirect(url_for('index'))

@app.route('/new', methods=['GET', 'POST'])
@login_required
def new_post():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        file = request.files['file']
        
        file_name = None
        original_file_name = None
        
        if file and allowed_file(file.filename):
            original_file_name = file.filename  # 원래 파일명 저장
            unique_filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
            file_name = unique_filename

        try:
            with get_db_connection() as db:
                with db.cursor() as cursor:
                    cursor.execute("INSERT INTO posts (title, content, file_name, original_file_name) VALUES (%s, %s, %s, %s)", (title, content, file_name, original_file_name))
                    db.commit()
            return redirect(url_for('index'))
        except Exception as e:
            app.logger.error(f"Error inserting post: {e}")
            flash("게시글 등록 중 오류가 발생했습니다.")
            return redirect(url_for('new_post'))
    return render_template('new_post.html')

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/download/<int:post_id>')
@login_required
def download_file(post_id):
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT file_name, original_file_name FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                if post:
                    return send_from_directory(app.config['UPLOAD_FOLDER'], post['file_name'], as_attachment=True, download_name=post['original_file_name'])
                else:
                    flash("파일을 찾을 수 없습니다.")
                    return redirect(url_for('index'))
    except Exception as e:
        app.logger.error(f"Error downloading file: {e}")
        flash("파일 다운로드 중 오류가 발생했습니다.")
        return redirect(url_for('index'))

# 애플리케이션 종료 시 연결 풀 정리
@app.teardown_appcontext
def close_db(error):
    pass

if __name__ == '__main__':
    try:
        app.run(host='0.0.0.0', port=5000)
    finally:
        # 애플리케이션 종료 시 연결 풀 정리
        if db_pool:
            db_pool.close_all()