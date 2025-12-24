# app3_mfa.py - 수정된 app.py에 app_mfa.py의 MFA 기능 통합

# MariaDB with Connection Pool - 연결 안정성 개선
# 2단계 인증(2FA) 기능 추가 (app_mfa.py의 MFA 기능 통합)
# 다중 파일 업로드 지원 추가
# 검색 기능 추가
# 고객사 관리 기능 추가

from flask import Flask, request, redirect, url_for, render_template, flash, send_from_directory, session, jsonify
import mysql.connector
from mysql.connector import pooling
import logging
import os
import uuid
import json
from datetime import datetime, timedelta
import pytz
from contextlib import contextmanager
import time
import threading
from queue import Queue, Empty
import weakref
import atexit
import signal
import sys
from dotenv import load_dotenv
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re

# .env 파일 로드
load_dotenv()

app = Flask(__name__)

# 필수 환경 변수 - 설정되지 않으면 에러 발생
app.secret_key = os.getenv('FLASK_SECRET_KEY')
if not app.secret_key:
    raise ValueError("FLASK_SECRET_KEY 환경 변수가 설정되지 않았습니다.")

app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', '/mnt/test')

# 이메일 설정 (SMTP) - MFA 기능 추가
EMAIL_CONFIG = {
    'smtp_server': os.getenv('MAIL_SERVER'),
    'smtp_port': int(os.getenv('MAIL_PORT', 587)),
    'sender_email': os.getenv('MAIL_USERNAME'),
    'sender_password': os.getenv('MAIL_PASSWORD'),
}

# 필수 이메일 설정 검증 - MFA 기능 추가
if not EMAIL_CONFIG['sender_email'] or not EMAIL_CONFIG['sender_password']:
    app.logger.warning("Email configuration is missing. 2FA emails will not be sent.")

# 2FA 설정
TWO_FA_CODE_EXPIRY = int(os.getenv('TWO_FA_CODE_EXPIRY', '300'))  # 5분 (초 단위)

# 데이터베이스 설정
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'dbnas'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'user': os.getenv('DB_USER', 'board_user'),
    'password': os.getenv('DB_PASSWORD'),  # 필수 - 기본값 제거
    'database': os.getenv('DB_NAME', 'board_db'),
    'connection_timeout': int(os.getenv('DB_CONNECTION_TIMEOUT', '60')),
    'charset': os.getenv('DB_CHARSET', 'utf8mb4'),
    'collation': os.getenv('DB_COLLATION', 'utf8mb4_unicode_ci'),
    'use_unicode': True,
    'autocommit': False
}

# 필수 DB 설정 검증
if not DB_CONFIG['password']:
    raise ValueError("DB_PASSWORD 환경 변수가 설정되지 않았습니다.")

# ZIP 파일 최대 크기 설정 (100MiB)
MAX_ZIP_SIZE = int(os.getenv('MAX_ZIP_SIZE', '104857600'))  # 100MiB in bytes

# 연결 풀 설정
DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '10'))

# 서버 설정
SERVER_HOST = os.getenv('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.getenv('SERVER_PORT', '5000'))
DEBUG_MODE = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'

# 로깅 설정
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 2FA 코드 저장소 (메모리 기반 - 실제 운영에서는 Redis 등 사용 권장)
two_fa_codes = {}

class ConnectionPool:
    """Custom MariaDB/MySQL Connection Pool"""

    def __init__(self, max_connections=DB_POOL_SIZE, **connection_kwargs):
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

        db_pool = ConnectionPool(max_connections=DB_POOL_SIZE, **DB_CONFIG)
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
            connection = db_pool.get_connection()
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
        # Flask 앱의 루트 디렉토리 기준으로 경로 설정
        app_root = os.path.dirname(os.path.abspath(__file__))
        identity_file_path = os.path.join(app_root, 'identity.json')
        
        app.logger.info(f"Looking for identity.json at: {identity_file_path}")
        app.logger.info(f"File exists: {os.path.exists(identity_file_path)}")
        
        with open(identity_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            app.logger.info("Successfully loaded identity.json")
            return data
            
    except FileNotFoundError:
        app.logger.error(f"identity.json file not found at: {identity_file_path}")
        return {}
    except json.JSONDecodeError:
        app.logger.error("Invalid JSON format in identity.json")
        return {}
    except Exception as e:
        app.logger.error(f"Unexpected error loading identity.json: {e}")
        return {}

def generate_2fa_code():
    """6자리 랜덤 인증 코드 생성"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def send_2fa_email(recipient_email, code, username):
    """2FA 인증 코드를 이메일로 발송"""
    try:
        # ✅ 항상 콘솔에 인증코드 출력
        print("=" * 60)
        print(f"🔐 2FA VERIFICATION CODE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"👤 User: {username}")
        print(f"📧 Intended Email: {recipient_email}")
        print(f"🔢 Verification Code: {code}")
        print(f"⏰ Expires in: {TWO_FA_CODE_EXPIRY} seconds ({TWO_FA_CODE_EXPIRY // 60} minutes)")
        print("=" * 60)
        
        # ✅ 이메일 설정 확인
        app.logger.info(f"Checking email configuration...")
        app.logger.info(f"MAIL_SERVER: {EMAIL_CONFIG['smtp_server']}")
        app.logger.info(f"MAIL_USERNAME: {EMAIL_CONFIG['sender_email']}")
        
        if not EMAIL_CONFIG['sender_email']:
            app.logger.error("MAIL_USERNAME is not set in .env file")
            return False
            
        if not EMAIL_CONFIG['sender_password']:
            app.logger.error("MAIL_PASSWORD is not set in .env file")
            return False

        # 이메일 발송 시도
        app.logger.info(f"Attempting to send email via {EMAIL_CONFIG['smtp_server']}")
        
        msg = MIMEMultipart('alternative')
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = recipient_email
        msg['Subject'] = '명진이의 자료실 - 로그인 인증 코드'

        # 이메일 본문 (간단하게)
        text_content = f"""
인증 코드: {code}

유효 시간: {TWO_FA_CODE_EXPIRY // 60}분

명진이의 자료실
        """

        part1 = MIMEText(text_content, 'plain', 'utf-8')
        msg.attach(part1)

        # SMTP 연결 및 발송
        with smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
            server.send_message(msg)

        app.logger.info(f"✅ Email successfully sent to {recipient_email}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        app.logger.error(f"❌ SMTP 인증 실패: 이메일 또는 비밀번호가 올바르지 않습니다.")
        app.logger.error(f"   Error details: {e}")
        return False
    except Exception as e:
        app.logger.error(f"❌ 이메일 발송 실패: {e}")
        return False

def cleanup_expired_codes():
    """만료된 2FA 코드 정리"""
    current_time = datetime.now()
    expired_users = [
        username for username, data in two_fa_codes.items()
        if current_time > data['expires_at']
    ]
    for username in expired_users:
        del two_fa_codes[username]
    
    if expired_users:
        app.logger.info(f"Cleaned up {len(expired_users)} expired 2FA codes")

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
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    return size

def format_file_size(size_bytes):
    """바이트를 사람이 읽기 쉬운 형태로 변환"""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes}B"
        
def sanitize_table_name(name):
    """테이블 이름을 안전하게 만들기 (SQL 인젝션 방지)"""
    # 특수문자 제거, 소문자로 변환, 언더스코어로 공백 대체
    sanitized = re.sub(r'[^a-zA-Z0-9가-힣_]', '_', name)
    sanitized = sanitized.lower()
    
    # 연속된 언더스코어를 하나로 줄이기
    sanitized = re.sub(r'_+', '_', sanitized)
    
    # 시작과 끝의 언더스코어 제거
    sanitized = sanitized.strip('_')
    
    # 테이블 이름 접두사 추가
    return f"customer_{sanitized}"

def create_customer_table(customer_name, db):
    """고객사 테이블 생성"""
    table_name = sanitize_table_name(customer_name)
    
    cursor = db.cursor()
    
    # 테이블이 이미 존재하는지 확인
    cursor.execute(f"""
        SELECT COUNT(*)
        FROM information_schema.tables 
        WHERE table_schema = %s 
        AND table_name = %s
    """, (DB_CONFIG['database'], table_name))
    
    if cursor.fetchone()[0] > 0:
        cursor.close()
        return table_name
    
    # 고객사 게시글 테이블 생성
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    
    # 고객사 파일 테이블 생성
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name}_files (
            id INT AUTO_INCREMENT PRIMARY KEY,
            post_id INT NOT NULL,
            file_name VARCHAR(255) NOT NULL,
            original_file_name VARCHAR(255) NOT NULL,
            file_size BIGINT,
            FOREIGN KEY (post_id) REFERENCES {table_name}(id) ON DELETE CASCADE,
            INDEX idx_post_id (post_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    
    # 고객사 정보 테이블에 추가
    cursor.execute("""
        INSERT INTO customers (name, table_name, created_at)
        VALUES (%s, %s, NOW())
    """, (customer_name, table_name))
    
    cursor.close()
    return table_name

def get_customer_table_name(customer_name):
    """고객사 이름으로 테이블 이름 조회"""
    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT table_name FROM customers WHERE name = %s
            """, (customer_name,))
            result = cursor.fetchone()
            cursor.close()
            
            if result:
                return result[0]
            else:
                return None
    except Exception as e:
        app.logger.error(f"Error getting customer table name: {e}")
        return None

def get_customer_files(post_id, customer_name):
    """고객사 게시글의 첨부 파일 목록을 가져옴"""
    table_name = get_customer_table_name(customer_name)
    if not table_name:
        return []
    
    try:
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute(f"""
                SELECT id, file_name, original_file_name, file_size 
                FROM {table_name}_files 
                WHERE post_id = %s 
                ORDER BY id
            """, (post_id,))
            result = cursor.fetchall()
            cursor.close()
            return result
    except Exception as e:
        app.logger.error(f"Error fetching files for customer post {post_id}: {e}")
        return []

def save_customer_files(post_id, customer_name, files, db):
    """고객사 게시글의 첨부 파일들을 저장"""
    table_name = get_customer_table_name(customer_name)
    if not table_name:
        raise ValueError(f"Customer table not found for {customer_name}")
    
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
                
                # 데이터베이스에 파일 정보 저장
                cursor.execute(f"""
                    INSERT INTO {table_name}_files (post_id, file_name, original_file_name, file_size)
                    VALUES (%s, %s, %s, %s)
                """, (post_id, unique_filename, original_filename, file_size))
                
                saved_files.append({
                    'file_name': unique_filename,
                    'original_file_name': original_filename,
                    'file_size': file_size
                })
                
                app.logger.info(f"Customer file saved: {unique_filename} (original: {original_filename})")
                
            except Exception as file_error:
                app.logger.error(f"Failed to save customer file {original_filename}: {file_error}")
                for saved_file in saved_files:
                    try:
                        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], saved_file['file_name']))
                    except:
                        pass
                raise file_error
    
    cursor.close()
    return saved_files

def delete_customer_files(post_id, customer_name):
    """고객사 게시글의 모든 첨부 파일을 삭제"""
    table_name = get_customer_table_name(customer_name)
    if not table_name:
        return
    
    try:
        files = get_customer_files(post_id, customer_name)
        
        for file_info in files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_info['file_name'])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    app.logger.info(f"Customer file deleted: {file_path}")
                except Exception as file_error:
                    app.logger.warning(f"Failed to delete customer file {file_path}: {file_error}")
        
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute(f"DELETE FROM {table_name}_files WHERE post_id = %s", (post_id,))
            db.commit()
            cursor.close()
                
    except Exception as e:
        app.logger.error(f"Error deleting customer files for post {post_id}: {e}")

def init_database():
    """데이터베이스 초기화 (필요한 테이블 생성)"""
    try:
        ensure_db_pool()
        
        with get_db_connection() as db:
            cursor = db.cursor()
            
            # 기존 테이블 생성 (변경 없음)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS post_files (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    post_id INT NOT NULL,
                    file_name VARCHAR(255) NOT NULL,
                    original_file_name VARCHAR(255) NOT NULL,
                    file_size BIGINT,
                    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                    INDEX idx_post_id (post_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            
            # 고객사 관리 테이블 생성
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    table_name VARCHAR(100) NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            
            db.commit()
            cursor.close()
            
            app.logger.info("Database tables initialized successfully")
            
    except Exception as e:
        app.logger.error(f"Database initialization error: {e}")

# ==================== 라우트 추가 ====================

#@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    """대시보드 페이지"""
    try:
        ensure_db_pool()
        
        # 고객사 목록 가져오기
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT name FROM customers ORDER BY created_at DESC")
            customers = cursor.fetchall()
            
            # 통계 정보
            cursor.execute("SELECT COUNT(*) as total FROM posts")
            total_posts = cursor.fetchone()['total']
            
            cursor.execute("SELECT COUNT(*) as total FROM post_files")
            total_files = cursor.fetchone()['total']
            
            cursor.close()
        
        return render_template('dashboard.html', 
                             customers=customers,
                             total_posts=total_posts,
                             total_files=total_files)
        
    except Exception as e:
        app.logger.error(f"Error loading dashboard: {e}")
        flash("대시보드를 불러오는 중 오류가 발생했습니다.")
        return render_template('dashboard.html', customers=[], total_posts=0, total_files=0)

@app.route('/api/customers', methods=['POST'])
@login_required
def api_add_customer():
    """API: 새 고객사 추가"""
    try:
        data = request.get_json()
        customer_name = data.get('name', '').strip()
        
        if not customer_name:
            return jsonify({'success': False, 'message': '고객사 이름을 입력하세요.'}), 400
        
        if len(customer_name) > 100:
            return jsonify({'success': False, 'message': '고객사 이름은 100자 이내로 입력해주세요.'}), 400
        
        ensure_db_pool()
        
        with get_db_connection() as db:
            # 중복 확인
            cursor = db.cursor()
            cursor.execute("SELECT COUNT(*) FROM customers WHERE name = %s", (customer_name,))
            if cursor.fetchone()[0] > 0:
                cursor.close()
                return jsonify({'success': False, 'message': '이미 존재하는 고객사 이름입니다.'}), 400
            
            # 테이블 생성
            table_name = create_customer_table(customer_name, db)
            db.commit()
            cursor.close()
        
        app.logger.info(f"New customer added: {customer_name} (table: {table_name})")
        return jsonify({'success': True, 'message': '고객사가 성공적으로 추가되었습니다.'})
        
    except Exception as e:
        app.logger.error(f"Error adding customer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/customers/<customer_name>', methods=['PUT'])
@login_required
def api_update_customer(customer_name):
    """API: 고객사 이름 수정"""
    try:
        data = request.get_json()
        new_name = data.get('name', '').strip()
        
        if not new_name:
            return jsonify({'success': False, 'message': '고객사 이름을 입력하세요.'}), 400
        
        if len(new_name) > 100:
            return jsonify({'success': False, 'message': '고객사 이름은 100자 이내로 입력해주세요.'}), 400
        
        if customer_name == new_name:
            return jsonify({'success': True, 'message': '변경사항이 없습니다.'})
        
        ensure_db_pool()
        
        with get_db_connection() as db:
            cursor = db.cursor()
            
            # 기존 고객사 존재 확인
            cursor.execute("SELECT COUNT(*) FROM customers WHERE name = %s", (customer_name,))
            if cursor.fetchone()[0] == 0:
                cursor.close()
                return jsonify({'success': False, 'message': '존재하지 않는 고객사입니다.'}), 404
            
            # 새 이름 중복 확인
            cursor.execute("SELECT COUNT(*) FROM customers WHERE name = %s", (new_name,))
            if cursor.fetchone()[0] > 0:
                cursor.close()
                return jsonify({'success': False, 'message': '이미 존재하는 고객사 이름입니다.'}), 400
            
            # 고객사 이름 변경
            cursor.execute("UPDATE customers SET name = %s WHERE name = %s", (new_name, customer_name))
            db.commit()
            cursor.close()
        
        app.logger.info(f"Customer updated: {customer_name} -> {new_name}")
        return jsonify({'success': True, 'message': '고객사 이름이 수정되었습니다.'})
        
    except Exception as e:
        app.logger.error(f"Error updating customer {customer_name}: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/customers/<customer_name>', methods=['DELETE'])
@login_required
def api_delete_customer(customer_name):
    """API: 고객사 삭제 (게시글 및 파일 일괄 삭제 포함)"""
    try:
        ensure_db_pool()
        
        table_name = get_customer_table_name(customer_name)
        if not table_name:
            return jsonify({'success': False, 'message': '존재하지 않는 고객사입니다.'}), 404
        
        with get_db_connection() as db:
            cursor = db.cursor()
            
            # 1. 모든 게시글 ID를 조회하여 첨부 파일 먼저 삭제
            cursor.execute(f"SELECT id FROM {table_name}")
            post_ids = [row[0] for row in cursor.fetchall()]
            
            app.logger.info(f"Deleting all files for {len(post_ids)} posts in customer {customer_name}...")
            
            # DB 연결을 공유하지 않는 get_db_connection() 밖에서 delete_customer_files를 사용하거나,
            # 현재 연결 내에서 처리합니다. (첨부 파일을 삭제하는 로직은 이미 delete_customer_files에 구현되어 있습니다.)
            
            # delete_customer_files는 내부적으로 자체 DB 연결을 사용하므로,
            # 여기서는 DB 테이블을 삭제하기 전에 모든 게시글의 첨부 파일을 먼저 안전하게 삭제하도록 반복 호출합니다.
            for post_id in post_ids:
                delete_customer_files(post_id, customer_name)

            # 2. 파일 테이블 삭제
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}_files")
            app.logger.info(f"Dropped table {table_name}_files")
            
            # 3. 게시글 테이블 삭제
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
            app.logger.info(f"Dropped table {table_name}")
            
            # 4. 고객사 정보 삭제
            cursor.execute("DELETE FROM customers WHERE name = %s", (customer_name,))
            
            db.commit()
            cursor.close()
        
        app.logger.info(f"Customer deleted: {customer_name} (table: {table_name}) including all posts and files")
        return jsonify({'success': True, 'message': '고객사가 삭제되었습니다. 모든 게시글과 첨부 파일도 삭제되었습니다.'})
        
    except Exception as e:
        app.logger.error(f"Error deleting customer {customer_name}: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/customer/<customer_name>')
@login_required
def customer_board(customer_name):
    """고객사 게시판 페이지"""
    try:
        ensure_db_pool()
        
        # 고객사 존재 확인
        table_name = get_customer_table_name(customer_name)
        if not table_name:
            flash("존재하지 않는 고객사입니다.")
            return redirect(url_for('dashboard'))
        
        page = request.args.get('page', 1, type=int)
        search_keyword = request.args.get('search', '', type=str).strip()
        per_page = 10
        offset = (page - 1) * per_page
        
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            
            if search_keyword:
                search_condition = f" WHERE p.title LIKE %s OR p.content LIKE %s"
                search_params = (f'%{search_keyword}%', f'%{search_keyword}%')
                count_params = search_params
                list_params = search_params + (per_page, offset)
            else:
                search_condition = ""
                search_params = ()
                count_params = ()
                list_params = (per_page, offset)
            
            # 총 게시글 수 조회
            count_query = f"SELECT COUNT(*) as count FROM {table_name} p {search_condition}"
            cursor.execute(count_query, count_params)
            total_posts = cursor.fetchone()['count']
            total_pages = (total_posts + per_page - 1) // per_page if total_posts > 0 else 1
            
            # 게시글 목록 조회
            list_query = f"""
                SELECT p.id, p.title, p.content, p.created_at,
                       COUNT(pf.id) as file_count
                FROM {table_name} p
                LEFT JOIN {table_name}_files pf ON p.id = pf.post_id
                {search_condition}
                GROUP BY p.id, p.title, p.content, p.created_at
                ORDER BY p.created_at DESC
                LIMIT %s OFFSET %s
            """
            cursor.execute(list_query, list_params)
            posts = cursor.fetchall()
            cursor.close()
            
            for i, post in enumerate(posts):
                post['created_at'] = convert_to_kst(post['created_at'])
                post['seq'] = total_posts - (offset + i)
                
                title = post.get('title') or ''
                post['short_title'] = title[:15] + ('...' if len(title) > 15 else '')
                
                content = post.get('content') or ''
                post['short_content'] = content[:25] + ('...' if len(content) > 25 else '')
        
        return render_template('customer_board.html',
                             customer_name=customer_name,
                             posts=posts,
                             page=page,
                             total_pages=total_pages,
                             search_keyword=search_keyword)
        
    except Exception as e:
        app.logger.error(f"Error loading customer board {customer_name}: {e}")
        flash("게시판을 불러오는 중 오류가 발생했습니다.")
        return redirect(url_for('dashboard'))

@app.route('/customer/<customer_name>/new', methods=['GET', 'POST'])
@login_required
def new_customer_post(customer_name):
    """고객사 새 글 작성"""
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        files = request.files.getlist('files[]')
        
        # 고객사 존재 확인
        table_name = get_customer_table_name(customer_name)
        if not table_name:
            flash("존재하지 않는 고객사입니다.")
            return redirect(url_for('dashboard'))
        
        # 파일 유효성 검사
        if files and any(file.filename for file in files):
            is_valid, message = validate_customer_files(files)
            if not is_valid:
                flash(message)
                search_keyword = request.args.get('search', '')
                return render_template('new_customer_post.html', 
                                     customer_name=customer_name,
                                     search_keyword=search_keyword)
        
        try:
            ensure_db_pool()
            
            with get_db_connection() as db:
                cursor = db.cursor()
                
                # 게시글 저장
                cursor.execute(f"""
                    INSERT INTO {table_name} (title, content)
                    VALUES (%s, %s)
                """, (title, content))
                
                post_id = cursor.lastrowid
                
                # 파일 저장
                if files and any(file.filename for file in files):
                    save_customer_files(post_id, customer_name, files, db)
                
                db.commit()
                cursor.close()
            
            flash("게시글이 등록되었습니다.")
            app.logger.info(f"New post created for customer {customer_name}")
            return redirect(url_for('customer_board', customer_name=customer_name))
        
        except Exception as e:
            app.logger.error(f"Error inserting customer post: {e}")
            flash(f"게시글 등록 중 오류가 발생했습니다: {str(e)}")
            search_keyword = request.args.get('search', '')
            return render_template('new_customer_post.html', 
                                 customer_name=customer_name,
                                 search_keyword=search_keyword)
    
    search_keyword = request.args.get('search', '')
    return render_template('new_customer_post.html', 
                         customer_name=customer_name,
                         search_keyword=search_keyword)

@app.route('/customer/<customer_name>/<int:post_id>')
@login_required
def customer_post(customer_name, post_id):
    """고객사 게시글 상세보기"""
    try:
        ensure_db_pool()
        
        # 고객사 존재 확인
        table_name = get_customer_table_name(customer_name)
        if not table_name:
            flash("존재하지 않는 고객사입니다.")
            return redirect(url_for('dashboard'))
        
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute(f"SELECT * FROM {table_name} WHERE id=%s", (post_id,))
            post = cursor.fetchone()
            cursor.close()
            
            if post:
                post['created_at'] = convert_to_kst(post['created_at'])
                post['files'] = get_customer_files(post_id, customer_name)
                
                search_keyword = request.args.get('search', '')
                return render_template('customer_post.html', 
                                     customer_name=customer_name,
                                     post=post,
                                     search_keyword=search_keyword)
            else:
                flash("게시글을 찾을 수 없습니다.")
                return redirect(url_for('customer_board', customer_name=customer_name))
        
    except Exception as e:
        app.logger.error(f"Error fetching customer post {post_id}: {e}")
        flash("게시글을 가져오는 중 오류가 발생했습니다.")
        return redirect(url_for('customer_board', customer_name=customer_name))

@app.route('/customer/<customer_name>/edit/<int:post_id>', methods=['GET', 'POST'])
@login_required
def edit_customer_post(customer_name, post_id):
    table_name = get_customer_table_name(customer_name)
    ensure_db_pool()
    
    # 1. POST: 데이터 업데이트 로직
    if request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '')
            files = request.files.getlist('files[]')
            keep_files = request.form.getlist('keep_files')
            search_keyword = request.form.get('search_keyword', '')

            with get_db_connection() as db:
                cursor = db.cursor(dictionary=True)
                
                # [수정] 기존 파일 목록 가져오기
                existing_files = get_customer_files(post_id, customer_name)
                
                # 체크박스에 선택되지 않은(유지하지 않을) 파일들을 찾아 삭제합니다.
                for f_info in existing_files:
                    if str(f_info['id']) not in keep_files:
                        # 실제 NAS 파일 삭제
                        f_path = os.path.join(app.config['UPLOAD_FOLDER'], f_info['file_name'])
                        if os.path.exists(f_path):
                            os.remove(f_path)
                        # DB 레코드 삭제
                        cursor.execute(f"DELETE FROM {table_name}_files WHERE id = %s", (f_info['id'],))

                # 새 파일 저장
                new_files = [f for f in files if f and f.filename and f.filename.strip()]
                if new_files:
                    save_customer_files(post_id, customer_name, new_files, db)

                # 게시글 정보 업데이트
                cursor.execute(f"UPDATE {table_name} SET title=%s, content=%s WHERE id=%s", (title, content, post_id))
                db.commit()
            
            flash("게시글이 수정되었습니다.")
            return redirect(url_for('customer_post', customer_name=customer_name, post_id=post_id, search=search_keyword))
        except Exception as e:
            app.logger.error(f"Update error: {e}")
            flash("수정 중 오류가 발생했습니다.")
            return redirect(url_for('customer_board', customer_name=customer_name))

    # 2. GET: 수정 페이지 로드 로직
    try:
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute(f"SELECT * FROM {table_name} WHERE id = %s", (post_id,))
            post = cursor.fetchone()
            
        if not post:
            return "Post not found", 404

        # [중요 수정] post 객체에 files 정보 추가
        existing_files = get_customer_files(post_id, customer_name)
        post['files'] = existing_files  # ← post 객체에 files 정보 추가
        
        return render_template('edit_customer_post.html', 
                             customer_name=customer_name, 
                             post=post,  # ← 이제 post.files에 파일 정보가 있음
                             search_keyword=request.args.get('search', ''))
    except Exception as e:
        app.logger.error(f"Load error: {e}")
        return "Internal Server Error", 500

@app.route('/customer/<customer_name>/delete/<int:post_id>', methods=['POST'])
@login_required
def delete_customer_post(customer_name, post_id):
    """고객사 게시글 삭제"""
    try:
        ensure_db_pool()
        
        table_name = get_customer_table_name(customer_name)
        if not table_name:
            flash("존재하지 않는 고객사입니다.")
            return redirect(url_for('dashboard'))
        
        # 게시글 존재 확인
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute(f"SELECT * FROM {table_name} WHERE id=%s", (post_id,))
            post = cursor.fetchone()
            cursor.close()
            
            if not post:
                app.logger.warning(f"Customer post {post_id} not found for deletion")
                flash("삭제할 게시글을 찾을 수 없습니다.")
                return redirect(url_for('customer_board', customer_name=customer_name))
            
            app.logger.info(f"Found customer post {post_id} for deletion: {post['title']}")
        
        # 파일 삭제
        delete_customer_files(post_id, customer_name)
        
        # 게시글 삭제
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute(f"DELETE FROM {table_name} WHERE id=%s", (post_id,))
            deleted_rows = cursor.rowcount
            
            if deleted_rows > 0:
                db.commit()
                app.logger.info(f"Customer post {post_id} deleted successfully")
                flash("게시글이 성공적으로 삭제되었습니다.")
            else:
                db.rollback()
                app.logger.warning(f"Failed to delete customer post {post_id}")
                flash("게시글 삭제에 실패했습니다.")
            
            cursor.close()
        
    except Exception as e:
        app.logger.error(f"Error deleting customer post {post_id}: {e}")
        flash(f"게시글 삭제 중 오류가 발생했습니다: {str(e)}")
    
    return redirect(url_for('customer_board', customer_name=customer_name))

# ✅ 추가: 고객사 파일 다운로드 라우트
@app.route('/customer/<customer_name>/download/<int:file_id>')
@login_required
def download_customer_file(customer_name, file_id):
    """고객사 파일 다운로드"""
    try:
        ensure_db_pool()
        
        table_name = get_customer_table_name(customer_name)
        if not table_name:
            flash("존재하지 않는 고객사입니다.")
            return redirect(url_for('dashboard'))
        
        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute(f"SELECT file_name, original_file_name FROM {table_name}_files WHERE id=%s", (file_id,))
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
                    return redirect(url_for('customer_board', customer_name=customer_name))
            else:
                flash("파일을 찾을 수 없습니다.")
                return redirect(url_for('customer_board', customer_name=customer_name))
    
    except Exception as e:
        app.logger.error(f"Error downloading customer file {file_id}: {e}")
        flash("파일 다운로드 중 오류가 발생했습니다.")
        return redirect(url_for('customer_board', customer_name=customer_name))
          
def validate_customer_files(files):
    """고객사 게시글 파일 유효성 검사"""
    for file in files:
        if file and file.filename:
            filename = file.filename.lower()
            has_extension = '.' in filename and filename.rindex('.') < len(filename) - 1
            
            if not has_extension:
                return False, f"확장자가 없는 파일은 업로드할 수 없습니다: {file.filename}"
            
            # ZIP 파일 크기 체크
            if filename.endswith('.zip'):
                file_size = get_file_size(file)
                if file_size > MAX_ZIP_SIZE:
                    return False, f"ZIP 파일 크기가 100MB를 초과합니다: {file.filename} (현재 크기: {format_file_size(file_size)})"
    
    return True, "파일 검증 완료"


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
        files = get_post_files(post_id)
        
        for file_info in files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_info['file_name'])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    app.logger.info(f"File deleted: {file_path}")
                except Exception as file_error:
                    app.logger.warning(f"Failed to delete file {file_path}: {file_error}")
        
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
        # 만료된 코드 정리
        cleanup_expired_codes()
        
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        two_fa_code = request.form.get('two_fa_code', '').strip()

        # identity.json에서 사용자 정보 확인
        identity_data = load_identity()

        # 1단계: 아이디와 비밀번호 확인
        if not two_fa_code:
            if username in identity_data and identity_data[username]['password'] == password:
                # 2FA 코드 생성
                code = generate_2fa_code()
                expires_at = datetime.now() + timedelta(seconds=TWO_FA_CODE_EXPIRY)
                
                # 코드 저장
                two_fa_codes[username] = {
                    'code': code,
                    'expires_at': expires_at
                }
                
                # 이메일 발송
                user_email = identity_data[username].get('email')
                if user_email:
                    if send_2fa_email(user_email, code, username):
                        #flash(f"인증 코드가 {user_email}로 발송되었습니다.")
                        flash(f"인증 코드가 등록된 이메일로 발송되었습니다.")
                        return render_template('login.html', show_2fa=True, username=username)
                    else:
                        flash("인증 코드 발송에 실패했습니다. 관리자에게 문의하세요.")
                        app.logger.error(f"Failed to send 2FA email for user: {username}")
                        return render_template('login.html')
                else:
                    flash("사용자 이메일이 등록되어 있지 않습니다. 관리자에게 문의하세요.")
                    app.logger.error(f"No email found for user: {username}")
                    return render_template('login.html')
            else:
                flash("아이디 또는 비밀번호를 다시 확인해주세요.")
                return render_template('login.html')
        
        # 2단계: 2FA 코드 확인
        else:
            if username in two_fa_codes:
                stored_data = two_fa_codes[username]
                
                # 코드 만료 확인
                if datetime.now() > stored_data['expires_at']:
                    del two_fa_codes[username]
                    flash("인증 코드가 만료되었습니다. 다시 로그인해주세요.")
                    return render_template('login.html')
                
                # 코드 일치 확인
                if two_fa_code.upper() == stored_data['code']:
                    # 로그인 성공
                    del two_fa_codes[username]
                    session['logged_in'] = True
                    session['username'] = username
                    flash(f"{username}님, 로그인되었습니다.")
                    return redirect(url_for('dashboard'))
                else:
                    flash("인증 코드가 올바르지 않습니다.")
                    return render_template('login.html', show_2fa=True, username=username)
            else:
                flash("세션이 만료되었습니다. 다시 로그인해주세요.")
                return render_template('login.html')

    return render_template('login.html')

@app.route('/resend_2fa_code', methods=['POST'])
def resend_2fa_code():
    """2FA 코드 재발송"""
    username = request.form.get('username', '').strip()
    
    if not username:
        return jsonify({'success': False, 'message': '사용자명이 필요합니다.'}), 400
    
    identity_data = load_identity()
    
    if username not in identity_data:
        return jsonify({'success': False, 'message': '유효하지 않은 사용자입니다.'}), 400
    
    # 새 코드 생성
    code = generate_2fa_code()
    expires_at = datetime.now() + timedelta(seconds=TWO_FA_CODE_EXPIRY)
    
    two_fa_codes[username] = {
        'code': code,
        'expires_at': expires_at
    }
    
    # 이메일 발송
    user_email = identity_data[username].get('email')
    if user_email:
        if send_2fa_email(user_email, code, username):
            return jsonify({'success': True, 'message': '인증 코드가 재발송되었습니다.'})
        else:
            return jsonify({'success': False, 'message': '이메일 발송에 실패했습니다.'}), 500
    else:
        return jsonify({'success': False, 'message': '사용자 이메일이 등록되어 있지 않습니다.'}), 400

@app.route('/logout')
def logout():
    username = session.get('username')
    session.clear()
    
    # 로그아웃 시 해당 사용자의 2FA 코드도 삭제
    if username and username in two_fa_codes:
        del two_fa_codes[username]
    
    flash("로그아웃되었습니다.")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    try:
        ensure_db_pool()

        page = request.args.get('page', 1, type=int)
        search_keyword = request.args.get('search', '', type=str).strip()
        per_page = 10
        offset = (page - 1) * per_page

        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            
            if search_keyword:
                search_condition = """
                    WHERE p.title LIKE %s OR p.content LIKE %s
                """
                search_params = (f'%{search_keyword}%', f'%{search_keyword}%')
                count_params = search_params
                list_params = search_params + (per_page, offset)
            else:
                search_condition = ""
                search_params = ()
                count_params = ()
                list_params = (per_page, offset)
            
            count_query = f"SELECT COUNT(*) as count FROM posts p {search_condition}"
            cursor.execute(count_query, count_params)
            total_posts = cursor.fetchone()['count']
            total_pages = (total_posts + per_page - 1) // per_page if total_posts > 0 else 1

            list_query = f"""
                SELECT p.id, p.title, p.content, p.created_at,
                       COUNT(pf.id) as file_count
                FROM posts p
                LEFT JOIN post_files pf ON p.id = pf.post_id
                {search_condition}
                GROUP BY p.id, p.title, p.content, p.created_at
                ORDER BY p.created_at DESC
                LIMIT %s OFFSET %s
            """
            cursor.execute(list_query, list_params)
            posts = cursor.fetchall()
            cursor.close()

            for i, post in enumerate(posts):
                post['created_at'] = convert_to_kst(post['created_at'])
                post['seq'] = total_posts - (offset + i)

                title = post.get('title') or ''
                post['short_title'] = title[:15] + ('...' if len(title) > 15 else '')

                content = post.get('content') or ''
                post['short_content'] = content[:25] + ('...' if len(content) > 25 else '')

        app.logger.info(f"Successfully loaded {len(posts)} posts for page {page}" + 
                       (f" with search keyword '{search_keyword}'" if search_keyword else ""))
        return render_template('index.html', posts=posts, page=page, total_pages=total_pages, 
                             search_keyword=search_keyword)

    except Exception as e:
        app.logger.error(f"Error fetching posts: {e}")
        flash("게시글을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        return render_template('index.html', posts=[], page=1, total_pages=1, search_keyword='')

@app.route('/delete/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    try:
        ensure_db_pool()
        
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
        
        delete_post_files(post_id)
        
        with get_db_connection() as db:
            cursor = db.cursor()
            
            cursor.execute("SELECT COUNT(*) as count FROM posts WHERE id=%s", (post_id,))
            before_count = cursor.fetchone()[0]
            app.logger.info(f"Posts count before deletion: {before_count}")
            
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
        ensure_db_pool()

        with get_db_connection() as db:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
            post = cursor.fetchone()
            cursor.close()

            if post:
                post['created_at'] = convert_to_kst(post['created_at'])
                post['files'] = get_post_files(post_id)
                
                search_keyword = request.args.get('search', '')
                return render_template('post.html', post=post, search_keyword=search_keyword)
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
        try:
            ensure_db_pool()

            with get_db_connection() as db:
                cursor = db.cursor(dictionary=True)
                cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                post = cursor.fetchone()
                cursor.close()

                if post:
                    post['files'] = get_post_files(post_id)
                    search_keyword = request.args.get('search', '')
                    return render_template('edit_post.html', post=post, search_keyword=search_keyword)
                else:
                    flash("게시글을 찾을 수 없습니다.")
                    return redirect(url_for('index'))

        except Exception as e:
            app.logger.error(f"Error fetching post for edit {post_id}: {e}")
            flash("게시글을 가져오는 중 오류가 발생했습니다.")
            return redirect(url_for('index'))

    elif request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '')
            files = request.files.getlist('files[]')
            keep_files = request.form.getlist('keep_files')
            search_keyword = request.form.get('search_keyword', '')

            if not title:
                flash("제목을 입력하세요.")
                with get_db_connection() as db:
                    cursor = db.cursor(dictionary=True)
                    cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                    post = cursor.fetchone()
                    cursor.close()
                    
                    if post:
                        post['files'] = get_post_files(post_id)
                        return render_template('edit_post.html', post=post, search_keyword=search_keyword)
                    else:
                        flash("게시글을 찾을 수 없습니다.")
                        return redirect(url_for('index'))

            ensure_db_pool()

            with get_db_connection() as db:
                try:
                    cursor = db.cursor(dictionary=True)
                    
                    cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                    existing_post = cursor.fetchone()

                    if not existing_post:
                        cursor.close()
                        flash("게시글을 찾을 수 없습니다.")
                        return redirect(url_for('index'))

                    existing_files = get_post_files(post_id)
                    
                    files_to_delete = []
                    for file_info in existing_files:
                        if str(file_info['id']) not in keep_files:
                            files_to_delete.append(file_info)
                    
                    for file_info in files_to_delete:
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_info['file_name'])
                        if os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                app.logger.info(f"Old file deleted: {file_path}")
                            except Exception as file_error:
                                app.logger.warning(f"Failed to delete old file {file_path}: {file_error}")
                        
                        cursor.execute("DELETE FROM post_files WHERE id = %s", (file_info['id'],))

                    new_files = [f for f in files if f and f.filename and f.filename.strip()]
                    if new_files:
                        try:
                            saved_files = save_post_files(post_id, new_files, db)
                        except Exception as file_error:
                            app.logger.error(f"Failed to save new files: {file_error}")
                            flash(f"파일 저장 중 오류가 발생했습니다: {str(file_error)}")
                            db.rollback()
                            cursor.close()
                            existing_post['files'] = get_post_files(post_id)
                            return render_template('edit_post.html', post=existing_post, search_keyword=search_keyword)

                    cursor.execute("""
                        UPDATE posts 
                        SET title=%s, content=%s
                        WHERE id=%s
                    """, (title, content, post_id))
                    
                    affected_rows = cursor.rowcount
                    
                    if affected_rows >= 0:
                        db.commit()
                        flash("게시글이 성공적으로 수정되었습니다.")
                        cursor.close()
                        
                        if search_keyword:
                            return redirect(url_for('post', post_id=post_id, search=search_keyword))
                        else:
                            return redirect(url_for('post', post_id=post_id))
                    else:
                        db.rollback()
                        flash("게시글 수정에 실패했습니다.")
                        cursor.close()
                        existing_post['files'] = get_post_files(post_id)
                        return render_template('edit_post.html', post=existing_post, search_keyword=search_keyword)

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
            
            try:
                with get_db_connection() as db:
                    cursor = db.cursor(dictionary=True)
                    cursor.execute("SELECT * FROM posts WHERE id=%s", (post_id,))
                    post = cursor.fetchone()
                    cursor.close()
                    
                    if post:
                        post['files'] = get_post_files(post_id)
                        search_keyword = request.form.get('search_keyword', '')
                        return render_template('edit_post.html', post=post, search_keyword=search_keyword)
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
            ensure_db_pool()
            
            with get_db_connection() as db:
                cursor = db.cursor()
                
                cursor.execute("""
                    INSERT INTO posts (title, content)
                    VALUES (%s, %s)
                """, (title, content))
                
                post_id = cursor.lastrowid
                
                if files and any(file.filename for file in files):
                    save_post_files(post_id, files, db)
                
                db.commit()
                cursor.close()
                
            flash("게시글이 등록되었습니다.")
            app.logger.info("New post created successfully")
            return redirect(url_for('index'))
        
        except Exception as e:
            app.logger.error(f"Error inserting post: {e}")
            flash(f"게시글 등록 중 오류가 발생했습니다: {str(e)}")
            return redirect(url_for('new_post'))
    
    search_keyword = request.args.get('search', '')
    return render_template('new_post.html', search_keyword=search_keyword)

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/download/<int:file_id>')
@login_required
def download_file(file_id):
    try:
        ensure_db_pool()

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

@app.route('/health')
def health_check():
    """애플리케이션과 데이터베이스 상태 확인"""
    try:
        ensure_db_pool()

        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
        return {"status": "healthy", "database": "connected"}, 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}, 500

def cleanup_db_pool():
    """애플리케이션 종료 시 데이터베이스 연결 풀 정리"""
    global db_pool
    if db_pool:
        app.logger.info("Cleaning up database connection pool")
        db_pool.close_all()
        db_pool = None

def signal_handler(signum, frame):
    """시그널 핸들러 - 우아한 종료"""
    app.logger.info(f"Received signal {signum}, shutting down gracefully...")
    cleanup_db_pool()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

atexit.register(cleanup_db_pool)

if __name__ == '__main__':
    try:
        app.logger.info(f"Server starting with configuration:")
        app.logger.info(f"  Host: {SERVER_HOST}:{SERVER_PORT}")
        app.logger.info(f"  Debug: {DEBUG_MODE}")
        app.logger.info(f"  Database: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
        app.logger.info(f"  Upload Folder: {app.config['UPLOAD_FOLDER']}")
        app.logger.info(f"  Max ZIP Size: {MAX_ZIP_SIZE} bytes")
        app.logger.info(f"  DB Pool Size: {DB_POOL_SIZE}")
        app.logger.info(f"  2FA Code Expiry: {TWO_FA_CODE_EXPIRY} seconds ({TWO_FA_CODE_EXPIRY // 60} minutes)")
        
        init_db_pool()
        init_database()  # 데이터베이스 테이블 초기화 추가
        app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG_MODE)
    except KeyboardInterrupt:
        app.logger.info("Application interrupted by user")
    except Exception as e:
        app.logger.error(f"Application error: {e}")
    finally:
        cleanup_db_pool()