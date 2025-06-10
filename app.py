from flask import Flask, request, redirect, url_for, render_template, flash, send_from_directory
import pymysql
import logging
import os
import uuid
from datetime import datetime
import pytz

app = Flask(__name__)
app.secret_key = 'new1234!'  
app.config['UPLOAD_FOLDER'] = '/mnt/test'

# 로깅 설정
logging.basicConfig(level=logging.DEBUG)

# 데이터베이스 연결 전역 변수
db = None

def init_db():
    global db
    try:
        db = pymysql.connect(
            host="192.168.0.102",
            user="board_user",
            password="new1234!",
            database="board_db",
            cursorclass=pymysql.cursors.DictCursor
        )
    except pymysql.MySQLError as e:
        app.logger.error(f"Database connection failed: {e}")
        db = None

def convert_to_kst(utc_time):
    utc = pytz.utc
    kst = pytz.timezone('Asia/Seoul')
    utc_dt = utc.localize(utc_time)
    kst_dt = utc_dt.astimezone(kst)
    return kst_dt.strftime('%Y-%m-%d %H:%M:%S')

@app.before_request
def before_request():
    if db is None:
        init_db()

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 5
        offset = (page - 1) * per_page

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
def delete_post(post_id):
    try:
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
def post(post_id):
    try:
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
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/download/<int:post_id>')
def download_file(post_id):
    try:
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
