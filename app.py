#Mariadb with Connection Pool - 연결 안정성 개선

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

app = Flask(__name__)
app.secret_key = 'new1234!'  
app.config['UPLOAD_FOLDER'] = '/mnt/test'

# 로깅 설정
logging.basicConfig(level=logging.DEBUG)

# 데이터베이스 연결 풀 전역 변수
db_pool = None

def init_db_pool():
    """데이터베이스 연결 풀 초기화"""
    global db_pool
    try:
        db_pool = pooling.ConnectionPool(
            pool_name='web_pool',
            pool_size=10,  # 풀의 최대 연결 수
            pool_reset_session=True,  # 연결 재사용 시 세션 리셋
            host="dbnas",  # was 서버 host 파일에 10.0.1.30 db 라고 등록시
            #host="10.0.1.30",  # DB server
            user="board_user",
            password="new1234!",
            database="board_db",
            charset='utf8mb4',
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            # 연결 유지 관련 설정 강화
            connect_timeout=60,
            read_timeout=30,
            write_timeout=30,
            # 연결 재시도 설정
            ping_interval=300,  # 5분마다 ping
            retry_on_timeout=True,
            # 추가 연결 안정성 설정
            init_command="SET SESSION wait_timeout=28800",  # 8시간
            sql_mode="STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO"
        )
        app.logger.info("Database connection pool initialized successfully")
    except Exception as e:
        app.logger.error(f"Database connection pool initialization failed: {e}")
        db_pool = None

@contextmanager
def get_db_connection():
    """데이터베이스 연결을 안전하게 가져오고 반환하는 컨텍스트 매니저"""
    connection = None
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            if db_pool is None:
                app.logger.info("Reinitializing database pool...")
                init_db_pool()
                if db_pool is None:
                    raise Exception("Failed to initialize database pool")
            
            connection = db_pool.get_connection()
            
            # 연결 상태 확인 및 재연결 로직 강화
            try:
                # 먼저 간단한 쿼리로 연결 상태 확인
                with connection.cursor() as test_cursor:
                    test_cursor.execute("SELECT 1")
                    test_cursor.fetchone()
                app.logger.debug(f"Database connection verified (attempt {attempt + 1})")
                break
            except Exception as ping_error:
                app.logger.warning(f"Connection ping failed (attempt {attempt + 1}): {ping_error}")
                try:
                    connection.ping(reconnect=True)
                    # 재연결 후 다시 테스트
                    with connection.cursor() as test_cursor:
                        test_cursor.execute("SELECT 1")
                        test_cursor.fetchone()
                    app.logger.info(f"Database reconnected successfully (attempt {attempt + 1})")
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
                        retry_delay *= 2  # 지수 백오프
                    else:
                        # 마지막 시도에서 실패하면 풀을 재초기화
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
            except:
                pass
        app.logger.error(f"Database operation error: {e}")
        raise
    finally:
        if connection:
            try:
                connection.close()
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

@app.before_first_request
def initialize_app():
    """애플리케이션 시작 시 연결 풀 초기화"""
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
                # 게시글 총 개수 조회
                cursor.execute("SELECT COUNT(*) as count FROM posts")
                total_posts = cursor.fetchone()['count']
                total_pages = (total_posts + per_page - 1) // per_page if total_posts > 0 else 1

                # 게시글 목록 조회
                cursor.execute("""
                    SELECT id, title, content, file_name, original_file_name, created_at 
                    FROM posts 
                    ORDER BY created_at DESC 
                    LIMIT %s OFFSET %s
                """, (per_page, offset))
                posts = cursor.fetchall()

                # 게시글 번호 및 시간 변환
                for i, post in enumerate(posts):
                    post['created_at'] = convert_to_kst(post['created_at'])
                    post['seq'] = total_posts - (offset + i)

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
        app.logger.error(f"Error fetching post {post_id}: {e}")
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
                    cursor.execute("""
                        INSERT INTO posts (title, content, file_name, original_file_name) 
                        VALUES (%s, %s, %s, %s)
                    """, (title, content, file_name, original_file_name))
                    db.commit()
                    
            flash("게시글이 등록되었습니다.")
            app.logger.info("New post created successfully")
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

# 헬스체크 엔드포인트 추가
@app.route('/health')
def health_check():
    """애플리케이션과 데이터베이스 상태 확인"""
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return {"status": "healthy", "database": "connected"}, 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}, 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)