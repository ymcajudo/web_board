#Mariadb with Connection Pool - 연결 안정성 개선 및 디버깅 강화

from flask import Flask, request, redirect, url_for, render_template, flash, send_from_directory, session
import pymysql
from pymysql import pooling
import logging
import os
import uuid
import json
from datetime import datetime
import pytz
from contextlib import contextmanager
import time
import traceback

app = Flask(__name__)
app.secret_key = 'new1234!'  
app.config['UPLOAD_FOLDER'] = '/mnt/test'

# 로깅 설정 강화
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 데이터베이스 연결 풀 전역 변수
db_pool = None

def init_db_pool():
    """데이터베이스 연결 풀 초기화"""
    global db_pool
    try:
        app.logger.info("Initializing database connection pool...")
        
        # 연결 설정 로깅
        db_config = {
            'pool_name': 'web_pool',
            'pool_size': 10,
            'pool_reset_session': True,
            'host': "dbnas",  # 또는 "10.0.1.30"
            'user': "board_user",
            'password': "new1234!",
            'database': "board_db",
            'charset': 'utf8mb4',
            'autocommit': False,
            'cursorclass': pymysql.cursors.DictCursor,
            'connect_timeout': 60,
            'read_timeout': 30,
            'write_timeout': 30,
            'ping_interval': 300,
            'retry_on_timeout': True,
            'init_command': "SET SESSION wait_timeout=28800",
            'sql_mode': "STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO"
        }
        
        app.logger.info(f"DB Config: host={db_config['host']}, user={db_config['user']}, database={db_config['database']}")
        
        db_pool = pooling.ConnectionPool(**db_config)
        app.logger.info("Database connection pool initialized successfully")
        
        # 연결 테스트
        test_connection = db_pool.get_connection()
        with test_connection.cursor() as cursor:
            cursor.execute("SELECT 1 as test")
            result = cursor.fetchone()
            app.logger.info(f"Database connection test successful: {result}")
        test_connection.close()
        
    except Exception as e:
        app.logger.error(f"Database connection pool initialization failed: {e}")
        app.logger.error(f"Error details: {traceback.format_exc()}")
        db_pool = None

@contextmanager
def get_db_connection():
    """데이터베이스 연결을 안전하게 가져오고 반환하는 컨텍스트 매니저"""
    connection = None
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            app.logger.debug(f"Attempting database connection (attempt {attempt + 1})")
            
            if db_pool is None:
                app.logger.info("Database pool not initialized, reinitializing...")
                init_db_pool()
                if db_pool is None:
                    raise Exception("Failed to initialize database pool")
            
            connection = db_pool.get_connection()
            app.logger.debug("Connection obtained from pool")
            
            # 연결 상태 확인
            try:
                with connection.cursor() as test_cursor:
                    test_cursor.execute("SELECT 1 as connection_test")
                    result = test_cursor.fetchone()
                    app.logger.debug(f"Connection test result: {result}")
                break
            except Exception as ping_error:
                app.logger.warning(f"Connection test failed (attempt {attempt + 1}): {ping_error}")
                try:
                    connection.ping(reconnect=True)
                    with connection.cursor() as test_cursor:
                        test_cursor.execute("SELECT 1 as reconnect_test")
                        result = test_cursor.fetchone()
                        app.logger.info(f"Reconnection successful: {result}")
                    break
                except Exception as reconnect_error:
                    app.logger.error(f"Reconnection failed (attempt {attempt + 1}): {reconnect_error}")
                    if connection:
                        try:
                            connection.close()
                        except:
                            pass
                    connection = None
                    
                    if attempt < max_retries - 1:
                        app.logger.info(f"Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        app.logger.error("All connection attempts failed, reinitializing pool...")
                        db_pool = None
                        init_db_pool()
                        if db_pool:
                            connection = db_pool.get_connection()
                            connection.ping(reconnect=True)
                            break
                        else:
                            raise Exception("Failed to establish database connection after all retries")
            
        except Exception as e:
            app.logger.error(f"Database connection error (attempt {attempt + 1}): {e}")
            app.logger.error(f"Error traceback: {traceback.format_exc()}")
            if connection:
                try:
                    connection.close()
                except:
                    pass
                connection = None
            
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
                app.logger.info("Database transaction rolled back")
            except Exception as rollback_error:
                app.logger.error(f"Rollback failed: {rollback_error}")
        app.logger.error(f"Database operation error: {e}")
        app.logger.error(f"Operation error traceback: {traceback.format_exc()}")
        raise
    finally:
        if connection:
            try:
                connection.close()
                app.logger.debug("Database connection closed")
            except Exception as close_error:
                app.logger.warning(f"Error closing connection: {close_error}")

def check_database_setup():
    """데이터베이스 테이블 존재 여부 확인 및 생성"""
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                # 테이블 존재 여부 확인
                cursor.execute("SHOW TABLES LIKE 'posts'")
                table_exists = cursor.fetchone()
                
                if not table_exists:
                    app.logger.warning("Posts table does not exist, creating...")
                    cursor.execute("""
                        CREATE TABLE posts (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            title VARCHAR(255) NOT NULL,
                            content TEXT,
                            file_name VARCHAR(255),
                            original_file_name VARCHAR(255),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """)
                    db.commit()
                    app.logger.info("Posts table created successfully")
                else:
                    app.logger.info("Posts table exists")
                    
                # 테이블 구조 확인
                cursor.execute("DESCRIBE posts")
                table_structure = cursor.fetchall()
                app.logger.info(f"Table structure: {table_structure}")
                
                # 게시글 개수 확인
                cursor.execute("SELECT COUNT(*) as count FROM posts")
                post_count = cursor.fetchone()
                app.logger.info(f"Current post count: {post_count['count']}")
                
    except Exception as e:
        app.logger.error(f"Database setup check failed: {e}")
        app.logger.error(f"Setup error traceback: {traceback.format_exc()}")
        raise

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

@app.before_first_request
def initialize_app():
    """애플리케이션 시작 시 연결 풀 초기화"""
    app.logger.info("Application initializing...")
    init_db_pool()
    check_database_setup()
    app.logger.info("Application initialization complete")

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        app.logger.info(f"Login attempt for user: {username}")
        
        # identity.json에서 사용자 정보 확인
        identity_data = load_identity()
        
        if username in identity_data and identity_data[username]['password'] == password:
            session['logged_in'] = True
            session['username'] = username
            flash(f"{username}님, 로그인되었습니다.")
            app.logger.info(f"User {username} logged in successfully")
            return redirect(url_for('index'))
        else:
            flash("아이디 또는 비밀번호를 다시 확인해주세요.")
            app.logger.warning(f"Failed login attempt for user: {username}")
            return render_template('login.html')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    username = session.get('username', 'unknown')
    session.clear()
    flash("로그아웃되었습니다.")
    app.logger.info(f"User {username} logged out")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    try:
        app.logger.info("Loading index page...")
        page = request.args.get('page', 1, type=int)
        per_page = 5
        offset = (page - 1) * per_page

        with get_db_connection() as db:
            with db.cursor() as cursor:
                app.logger.debug("Executing count query...")
                # 게시글 총 개수 조회
                cursor.execute("SELECT COUNT(*) as count FROM posts")
                total_result = cursor.fetchone()
                total_posts = total_result['count'] if total_result else 0
                app.logger.info(f"Total posts in database: {total_posts}")
                
                total_pages = (total_posts + per_page - 1) // per_page if total_posts > 0 else 1

                app.logger.debug(f"Executing posts query with limit {per_page}, offset {offset}...")
                # 게시글 목록 조회
                cursor.execute("""
                    SELECT id, title, content, file_name, original_file_name, created_at 
                    FROM posts 
                    ORDER BY created_at DESC 
                    LIMIT %s OFFSET %s
                """, (per_page, offset))
                posts = cursor.fetchall()
                app.logger.info(f"Retrieved {len(posts)} posts from database")

                # 게시글 번호 및 시간 변환
                for i, post in enumerate(posts):
                    post['created_at'] = convert_to_kst(post['created_at'])
                    post['seq'] = total_posts - (offset + i)
                    app.logger.debug(f"Post {i+1}: ID={post['id']}, Title={post['title']}")

        app.logger.info(f"Successfully loaded {len(posts)} posts for page {page}")
        return render_template('index.html', posts=posts, page=page, total_pages=total_pages)
        
    except Exception as e:
        app.logger.error(f"Error fetching posts: {e}")
        app.logger.error(f"Index error traceback: {traceback.format_exc()}")
        flash("게시글을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return render_template('index.html', posts=[], page=1, total_pages=1)

@app.route('/delete/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    try:
        app.logger.info(f"Deleting post {post_id}")
        with get_db_connection() as db:
            with db.cursor() as cursor:
                # 파일명 조회
                cursor.execute("SELECT file_name FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                
                # 파일 삭제
                if post and post['file_name']:
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], post['file_name'])
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            app.logger.info(f"File deleted: {file_path}")
                        except Exception as file_error:
                            app.logger.warning(f"Failed to delete file {file_path}: {file_error}")

                # 게시글 삭제
                cursor.execute("DELETE FROM posts WHERE id=%s", (post_id,))
                db.commit()
                
        flash("게시글이 삭제되었습니다.")
        app.logger.info(f"Post {post_id} deleted successfully")
        
    except Exception as e:
        app.logger.error(f"Error deleting post {post_id}: {e}")
        app.logger.error(f"Delete error traceback: {traceback.format_exc()}")
        flash("게시글 삭제 중 오류가 발생했습니다.")
        
    return redirect(url_for('index'))

@app.route('/post/<int:post_id>')
@login_required
def post(post_id):
    try:
        app.logger.info(f"Loading post {post_id}")
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                
                if post:
                    post['created_at'] = convert_to_kst(post['created_at'])
                    app.logger.info(f"Post {post_id} loaded successfully")
                    return render_template('post.html', post=post)
                else:
                    flash("게시글을 찾을 수 없습니다.")
                    app.logger.warning(f"Post {post_id} not found")
                    return redirect(url_for('index'))
                    
    except Exception as e:
        app.logger.error(f"Error fetching post {post_id}: {e}")
        app.logger.error(f"Post error traceback: {traceback.format_exc()}")
        flash("게시글을 가져오는 중 오류가 발생했습니다.")
        return redirect(url_for('index'))

@app.route('/new', methods=['GET', 'POST'])
@login_required
def new_post():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        file = request.files['file']
        
        app.logger.info(f"Creating new post: title='{title}'")
        
        file_name = None
        original_file_name = None
        
        if file and allowed_file(file.filename):
            original_file_name = file.filename
            unique_filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
            try:
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                file_name = unique_filename
                app.logger.info(f"File saved: {unique_filename}")
            except Exception as file_error:
                app.logger.error(f"Failed to save file: {file_error}")
                flash("파일 저장 중 오류가 발생했습니다.")
                return redirect(url_for('new_post'))

        try:
            with get_db_connection() as db:
                with db.cursor() as cursor:
                    app.logger.debug("Executing insert query...")
                    cursor.execute("""
                        INSERT INTO posts (title, content, file_name, original_file_name) 
                        VALUES (%s, %s, %s, %s)
                    """, (title, content, file_name, original_file_name))
                    db.commit()
                    app.logger.info(f"New post inserted with ID: {cursor.lastrowid}")
                    
            flash("게시글이 등록되었습니다.")
            app.logger.info("New post created successfully")
            return redirect(url_for('index'))
            
        except Exception as e:
            app.logger.error(f"Error inserting post: {e}")
            app.logger.error(f"Insert error traceback: {traceback.format_exc()}")
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
                
                if post and post['file_name']:
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], post['file_name'])
                    if os.path.exists(file_path):
                        return send_from_directory(
                            app.config['UPLOAD_FOLDER'], 
                            post['file_name'], 
                            as_attachment=True, 
                            download_name=post['original_file_name']
                        )
                    else:
                        flash("파일이 존재하지 않습니다.")
                        return redirect(url_for('index'))
                else:
                    flash("파일을 찾을 수 없습니다.")
                    return redirect(url_for('index'))
                    
    except Exception as e:
        app.logger.error(f"Error downloading file for post {post_id}: {e}")
        flash("파일 다운로드 중 오류가 발생했습니다.")
        return redirect(url_for('index'))

# 헬스체크 엔드포인트 개선
@app.route('/health')
def health_check():
    """애플리케이션과 데이터베이스 상태 확인"""
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT 1 as health_check")
                result = cursor.fetchone()
                cursor.execute("SELECT COUNT(*) as post_count FROM posts")
                post_count = cursor.fetchone()
                
        return {
            "status": "healthy", 
            "database": "connected",
            "post_count": post_count['post_count'],
            "timestamp": datetime.now().isoformat()
        }, 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy", 
            "database": "disconnected", 
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }, 500

# 데이터베이스 진단 엔드포인트 추가
@app.route('/debug/db')
def debug_db():
    """데이터베이스 상태 진단"""
    try:
        debug_info = {
            "pool_status": "initialized" if db_pool else "not_initialized",
            "timestamp": datetime.now().isoformat()
        }
        
        if db_pool:
            with get_db_connection() as db:
                with db.cursor() as cursor:
                    # 데이터베이스 기본 정보
                    cursor.execute("SELECT VERSION() as version")
                    version = cursor.fetchone()
                    debug_info["database_version"] = version['version']
                    
                    # 현재 데이터베이스
                    cursor.execute("SELECT DATABASE() as current_db")
                    current_db = cursor.fetchone()
                    debug_info["current_database"] = current_db['current_db']
                    
                    # 테이블 목록
                    cursor.execute("SHOW TABLES")
                    tables = cursor.fetchall()
                    debug_info["tables"] = [list(table.values())[0] for table in tables]
                    
                    # posts 테이블 정보
                    if 'posts' in debug_info["tables"]:
                        cursor.execute("SELECT COUNT(*) as count FROM posts")
                        post_count = cursor.fetchone()
                        debug_info["posts_count"] = post_count['count']
                        
                        cursor.execute("DESCRIBE posts")
                        posts_structure = cursor.fetchall()
                        debug_info["posts_structure"] = posts_structure
                    
        return debug_info, 200
        
    except Exception as e:
        app.logger.error(f"Database debug failed: {e}")
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "timestamp": datetime.now().isoformat()
        }, 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)