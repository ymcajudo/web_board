import pymysql
from pymysql import pooling
import logging
import time
import traceback
from contextlib import contextmanager
from flask import Flask

app = Flask(__name__)

# 로깅 설정
logging.basicConfig(level=logging.DEBUG)

# 데이터베이스 연결 풀 전역 변수
db_pool = None

def init_db_pool():
    """데이터베이스 연결 풀 초기화 - 타임아웃 설정 개선"""
    global db_pool
    try:
        app.logger.info("Initializing database connection pool...")
        
        # 연결 설정 - 타임아웃 관련 설정 강화
        db_config = {
            'pool_name': 'web_pool',
            'pool_size': 10,
            'pool_reset_session': True,
            'host': "dbnas",
            'user': "board_user", 
            'password': "new1234!",
            'database': "board_db",
            'charset': 'utf8mb4',
            'autocommit': False,
            'cursorclass': pymysql.cursors.DictCursor,
            
            # 타임아웃 설정 강화
            'connect_timeout': 60,
            'read_timeout': 30,
            'write_timeout': 30,
            
            # 연결 유지 및 재연결 설정
            'ping_interval': 300,  # 5분마다 ping
            'retry_on_timeout': True,
            
            # MySQL 서버 설정 - 더 긴 타임아웃 설정
            'init_command': """
                SET SESSION wait_timeout=43200,
                    interactive_timeout=43200,
                    net_read_timeout=60,
                    net_write_timeout=60
            """,
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

def validate_connection(connection):
    """연결 유효성 검사 및 재연결"""
    try:
        # 연결 상태 확인
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 as ping_test")
            result = cursor.fetchone()
            app.logger.debug(f"Connection validation successful: {result}")
            return True
    except Exception as e:
        app.logger.warning(f"Connection validation failed: {e}")
        try:
            # 재연결 시도
            connection.ping(reconnect=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 as reconnect_test")
                result = cursor.fetchone()
                app.logger.info(f"Reconnection successful: {result}")
                return True
        except Exception as reconnect_error:
            app.logger.error(f"Reconnection failed: {reconnect_error}")
            return False

@contextmanager
def get_db_connection():
    """개선된 데이터베이스 연결 관리 - 자동 재연결 및 풀 재초기화"""
    connection = None
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            app.logger.debug(f"Attempting database connection (attempt {attempt + 1})")
            
            # 풀이 초기화되지 않은 경우 재초기화
            if db_pool is None:
                app.logger.info("Database pool not initialized, reinitializing...")
                init_db_pool()
                if db_pool is None:
                    raise Exception("Failed to initialize database pool")
            
            # 연결 획득
            connection = db_pool.get_connection()
            app.logger.debug("Connection obtained from pool")
            
            # 연결 유효성 검사
            if validate_connection(connection):
                break
            else:
                # 유효하지 않은 연결인 경우 닫고 재시도
                try:
                    connection.close()
                except:
                    pass
                connection = None
                
                if attempt < max_retries - 1:
                    app.logger.info(f"Invalid connection, retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    # 마지막 시도에서 실패한 경우 풀 재초기화
                    app.logger.error("All connection attempts failed, reinitializing pool...")
                    db_pool = None
                    init_db_pool()
                    if db_pool:
                        connection = db_pool.get_connection()
                        if validate_connection(connection):
                            break
                        else:
                            raise Exception("Failed to establish valid database connection after pool reinitialization")
                    else:
                        raise Exception("Failed to reinitialize database pool")
            
        except Exception as e:
            app.logger.error(f"Database connection error (attempt {attempt + 1}): {e}")
            if connection:
                try:
                    connection.close()
                except:
                    pass
                connection = None
            
            if attempt == max_retries - 1:
                # 마지막 시도에서도 실패한 경우 풀 재초기화 한번 더 시도
                app.logger.error("Final attempt failed, trying pool reinitialization...")
                db_pool = None
                init_db_pool()
                if db_pool:
                    try:
                        connection = db_pool.get_connection()
                        if validate_connection(connection):
                            break
                        else:
                            raise Exception("Connection validation failed after pool reinitialization")
                    except Exception as final_error:
                        raise Exception(f"Failed to establish database connection after all attempts: {final_error}")
                else:
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
        raise
    finally:
        if connection:
            try:
                connection.close()
                app.logger.debug("Database connection closed")
            except Exception as close_error:
                app.logger.warning(f"Error closing connection: {close_error}")

# 주기적으로 연결 풀 상태 확인하는 함수
def check_pool_health():
    """연결 풀 상태 확인 및 필요시 재초기화"""
    global db_pool
    try:
        if db_pool is None:
            app.logger.warning("Database pool is None, reinitializing...")
            init_db_pool()
            return
            
        # 테스트 연결 시도
        test_connection = db_pool.get_connection()
        if not validate_connection(test_connection):
            app.logger.warning("Pool health check failed, reinitializing pool...")
            test_connection.close()
            db_pool = None
            init_db_pool()
        else:
            app.logger.debug("Pool health check passed")
            test_connection.close()
            
    except Exception as e:
        app.logger.error(f"Pool health check error: {e}")
        db_pool = None
        init_db_pool()

# Flask 라우트에서 사용하는 예시
@app.route('/health')
def health_check():
    """개선된 헬스체크 - 연결 풀 상태도 확인"""
    try:
        # 연결 풀 상태 확인
        check_pool_health()
        
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT 1 as health_check, NOW() as current_time")
                result = cursor.fetchone()
                cursor.execute("SELECT COUNT(*) as post_count FROM posts")
                post_count = cursor.fetchone()
                
        return {
            "status": "healthy", 
            "database": "connected",
            "server_time": result['current_time'].isoformat(),
            "post_count": post_count['post_count'],
            "pool_status": "initialized" if db_pool else "not_initialized"
        }, 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy", 
            "database": "disconnected", 
            "error": str(e),
            "pool_status": "error"
        }, 500

# 애플리케이션 시작 시 초기화
if __name__ == '__main__':
    init_db_pool()
    app.run(host='0.0.0.0', port=5000, debug=True)