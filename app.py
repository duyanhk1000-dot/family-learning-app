import streamlit as st
import sqlite3
import json
import os
import datetime
import pypdf
import tempfile
import time
import hashlib
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# Cấu hình trang Streamlit
st.set_page_config(
    page_title="Hệ thống Học tập Gia đình",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

DB_FILE = 'he_thong_hoc_tap.db'

# --- ĐỊNH NGHĨA CẤU TRÚC DỮ LIỆU PYDANTIC CHO AI ---

class Question(BaseModel):
    question_number: int = Field(..., description="Số thứ tự câu hỏi từ 1 đến 15")
    question_type: Literal["multiple_choice", "essay"] = Field(..., description="Loại câu hỏi: 'multiple_choice' (trắc nghiệm) hoặc 'essay' (tự luận)")
    prompt: str = Field(..., description="Nội dung câu hỏi. Sử dụng LaTeX $...$ hoặc $$...$$ cho các công thức Toán/Lý/Hóa nếu có.")
    options: Optional[List[str]] = Field(None, description="Danh sách 4 lựa chọn cho trắc nghiệm (ví dụ: ['A. ...', 'B. ...', 'C. ...', 'D. ...']). Để None cho tự luận.")
    correct_answer: str = Field(..., description="Đáp án đúng. Trắc nghiệm: Ghi rõ chữ cái 'A', 'B', 'C', hoặc 'D'. Tự luận: Lời giải mẫu chi tiết.")

class FlashcardItem(BaseModel):
    front: str = Field(..., description="Mặt trước: Câu hỏi nhanh, công thức viết tắt, thuật ngữ khoa học hoặc sự kiện cần nhớ")
    back: str = Field(..., description="Mặt sau: Lời giải thích chi tiết, công thức đầy đủ, định nghĩa khoa học hoặc câu trả lời")

class LessonPayload(BaseModel):
    title: str = Field(..., description="Tiêu đề buổi học")
    lecture_content: str = Field(..., description="Nội dung bài giảng chi tiết, dễ hiểu, theo đúng cấu trúc 5 phần, sử dụng Markdown và LaTeX cho công thức toán học.")
    duration_minutes: int = Field(..., description="Thời gian làm bài thi (phút), từ 30 đến 60 phút.")
    flashcards: List[FlashcardItem] = Field(..., description="Danh sách ĐÚNG 15 thẻ Flashcard ôn tập các kiến thức cốt lõi, từ vựng hoặc sự kiện quan trọng nhất cần nhớ của buổi học này.")
    questions: List[Question] = Field(..., description="Danh sách ĐÚNG 15 câu hỏi kiểm tra (10 câu trắc nghiệm, 5 câu tự luận).")

class QuestionFeedback(BaseModel):
    question_number: int = Field(..., description="Số thứ tự câu hỏi từ 1 đến 15")
    student_answer: str = Field(..., description="Câu trả lời của học sinh")
    is_correct: bool = Field(..., description="Đúng (True) hoặc Sai (False)")
    score_awarded: float = Field(..., description="Điểm số đạt được cho câu này (ví dụ: trắc nghiệm đúng được 1 điểm, tự luận đúng hoàn toàn được 1 điểm hoặc thang điểm thành phần)")
    correct_explanation: str = Field(..., description="Giải thích chi tiết lời giải đúng, phân tích lỗi sai và cách khắc phục cho học sinh.")

class GradePayload(BaseModel):
    total_score: float = Field(..., description="Tổng điểm quy đổi của học sinh trên thang điểm 10. Chấm điểm chính xác và nghiêm khắc.")
    overall_feedback: str = Field(..., description="Nhận xét chung về bài làm, ưu điểm, nhược điểm và lời khuyên ôn tập.")
    detailed_feedback: List[QuestionFeedback] = Field(..., description="Chi tiết chấm điểm cho từng câu trong số 15 câu hỏi.")


# --- CÁC HÀM TRUY VẤN CƠ SỞ DỮ LIỆU ---

# --- CÁU HÌNH VÀ HÀM KẾT NỐI DATABASE CHUNG (SQLite & Supabase Postgres) ---

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

def init_postgres_tables(conn):
    try:
        with conn.cursor() as cursor:
            # Kiểm tra nếu bảng Syllabus kiểu cũ tồn tại (không có cột content)
            cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns 
                WHERE table_name = 'syllabus' AND column_name = 'mon_hoc'
            );
            """)
            has_old_schema = cursor.fetchone()[0]
            
            if has_old_schema:
                # Đang có bảng kiểu cũ -> Drop sạch để tạo mới chuẩn
                cursor.execute("DROP TABLE IF EXISTS Grades CASCADE;")
                cursor.execute("DROP TABLE IF EXISTS Lessons CASCADE;")
                cursor.execute("DROP TABLE IF EXISTS Syllabus CASCADE;")
                cursor.execute("DROP TABLE IF EXISTS Users CASCADE;")
                conn.commit()
                
            # Tạo bảng mới chuẩn đồng bộ với app.py
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS Users (
                username VARCHAR(100) PRIMARY KEY,
                password VARCHAR(100) NOT NULL,
                role VARCHAR(50) NOT NULL CHECK (role IN ('parent', 'student'))
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS Syllabus (
                id SERIAL PRIMARY KEY,
                subject VARCHAR(200) UNIQUE NOT NULL,
                content TEXT NOT NULL,
                textbook_content TEXT,
                pdf_file_path TEXT,
                total_lessons INTEGER DEFAULT 30,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS Lessons (
                id SERIAL PRIMARY KEY,
                subject VARCHAR(200) NOT NULL,
                lesson_number INTEGER NOT NULL,
                title VARCHAR(200) NOT NULL,
                lecture_content TEXT NOT NULL,
                questions TEXT NOT NULL,
                duration INTEGER NOT NULL,
                flashcards TEXT,
                UNIQUE(subject, lesson_number)
            );
            """)
            try:
                cursor.execute("ALTER TABLE Lessons ADD COLUMN IF NOT EXISTS flashcards TEXT;")
            except Exception:
                pass
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS Grades (
                id SERIAL PRIMARY KEY,
                student_username VARCHAR(100) NOT NULL,
                lesson_id INTEGER NOT NULL REFERENCES Lessons(id) ON DELETE CASCADE,
                answers TEXT NOT NULL,
                score REAL NOT NULL,
                ai_feedback TEXT NOT NULL,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS Messages (
                id SERIAL PRIMARY KEY,
                sender VARCHAR(100) NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            
            # Gieo dữ liệu tài khoản mặc định
            cursor.execute("""
            INSERT INTO Users (username, password, role)
            VALUES ('phuhuynh', '123456', 'parent')
            ON CONFLICT (username) DO NOTHING;
            """)
            cursor.execute("""
            INSERT INTO Users (username, password, role)
            VALUES ('hocsinh', '123456', 'student')
            ON CONFLICT (username) DO NOTHING;
            """)
            # Tự động dọn dẹp các dòng trùng lặp trong Lessons nếu có
            try:
                cursor.execute("""
                DELETE FROM Lessons 
                WHERE id NOT IN (
                    SELECT MAX(id) 
                    FROM Lessons 
                    GROUP BY subject, lesson_number
                );
                """)
            except Exception:
                pass
        conn.commit()
    except Exception as e:
        print(f"Lỗi khởi tạo Supabase PostgreSQL: {e}")

def init_sqlite_tables(conn):
    try:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS Users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('parent', 'student'))
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS Syllabus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            textbook_content TEXT,
            pdf_file_path TEXT,
            total_lessons INTEGER DEFAULT 30,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS Lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            lesson_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            lecture_content TEXT NOT NULL,
            questions TEXT NOT NULL,
            duration INTEGER NOT NULL,
            flashcards TEXT,
            UNIQUE(subject, lesson_number)
        );
        """)
        try:
            cursor.execute("ALTER TABLE Lessons ADD COLUMN flashcards TEXT;")
        except Exception:
            pass
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS Grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_username TEXT NOT NULL,
            lesson_id INTEGER NOT NULL,
            answers TEXT NOT NULL,
            score REAL NOT NULL,
            ai_feedback TEXT NOT NULL,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_username) REFERENCES Users(username),
            FOREIGN KEY (lesson_id) REFERENCES Lessons(id)
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS Messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("INSERT OR IGNORE INTO Users (username, password, role) VALUES ('phuhuynh', '123456', 'parent');")
        cursor.execute("INSERT OR IGNORE INTO Users (username, password, role) VALUES ('hocsinh', '123456', 'student');")
        
        # Tự động dọn dẹp các dòng trùng lặp trong Lessons nếu có
        try:
            cursor.execute("""
            DELETE FROM Lessons 
            WHERE id NOT IN (
                SELECT MAX(id) 
                FROM Lessons 
                GROUP BY subject, lesson_number
            );
            """)
        except Exception:
            pass
            
        conn.commit()
    except Exception as e:
        print(f"Lỗi khởi tạo SQLite: {e}")

class PostgresWrapper:
    def __init__(self, conn):
        self.conn = conn
        self.cursor = conn.cursor(cursor_factory=RealDictCursor)
        
    def execute(self, query, params=None):
        if params is None:
            params = ()
        query = query.replace("?", "%s")
        if "INSERT OR REPLACE INTO Syllabus" in query:
            query = """
            INSERT INTO Syllabus (subject, content, textbook_content, pdf_file_path, total_lessons)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (subject) DO UPDATE SET
                content = EXCLUDED.content,
                textbook_content = EXCLUDED.textbook_content,
                pdf_file_path = EXCLUDED.pdf_file_path,
                total_lessons = EXCLUDED.total_lessons
            """
        elif "INSERT OR REPLACE INTO Lessons" in query:
            query = """
            INSERT INTO Lessons (subject, lesson_number, title, lecture_content, questions, duration, flashcards)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (subject, lesson_number) DO UPDATE SET
                title = EXCLUDED.title,
                lecture_content = EXCLUDED.lecture_content,
                questions = EXCLUDED.questions,
                duration = EXCLUDED.duration,
                flashcards = EXCLUDED.flashcards
            """
        elif "INSERT OR REPLACE INTO Users" in query:
            query = """
            INSERT INTO Users (username, password, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                password = EXCLUDED.password,
                role = EXCLUDED.role
            """
        self.cursor.execute(query, params)
        return self
        
    def fetchone(self):
        row = self.cursor.fetchone()
        if row:
            return dict(row)
        return None
        
    def fetchall(self):
        rows = self.cursor.fetchall()
        return [dict(r) for r in rows]
        
    def commit(self):
        self.conn.commit()
        
    def rollback(self):
        self.conn.rollback()
        
    def close(self):
        self.cursor.close()
        self.conn.close()

    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.cursor.close()
        self.conn.close()

class SQLiteWrapper:
    def __init__(self, conn):
        self.conn = conn
        self.cursor = conn.cursor()
        
    def execute(self, query, params=None):
        if params is None:
            params = ()
        self.cursor.execute(query, params)
        return self
        
    def fetchone(self):
        row = self.cursor.fetchone()
        if row:
            return dict(row)
        return None
        
    def fetchall(self):
        rows = self.cursor.fetchall()
        return [dict(r) for r in rows]
        
    def commit(self):
        self.conn.commit()
        
    def rollback(self):
        self.conn.rollback()
        
    def close(self):
        self.cursor.close()
        self.conn.close()

    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.cursor.close()
        self.conn.close()

def get_db():
    db_url = None
    if st.secrets and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    elif "DATABASE_URL" in os.environ:
        db_url = os.environ["DATABASE_URL"]
        
    # Gợi ý/Cảnh báo nếu cấu hình nhầm API URL/KEY thay vì PostgreSQL URL
    if not db_url:
        supabase_url_exists = False
        if st.secrets:
            supabase_url_exists = "SUPABASE_URL" in st.secrets or "SUPABASE_KEY" in st.secrets
        if "SUPABASE_URL" in os.environ or "SUPABASE_KEY" in os.environ:
            supabase_url_exists = True
            
        if supabase_url_exists:
            st.error("⚠️ **Lỗi cấu hình Secrets:** Bạn đang điền `SUPABASE_URL` và `SUPABASE_KEY`. Tuy nhiên, ứng dụng này kết nối trực tiếp cơ sở dữ liệu qua PostgreSQL! Bạn cần lấy chuỗi kết nối **Connection String (URI)** từ Supabase Dashboard (mục Settings -> Database) và lưu vào Streamlit Secrets với tên là **`DATABASE_URL`** (dạng `postgresql://postgres:...`).")
        else:
            st.error("⚠️ **Chưa kết nối dữ liệu:** Không tìm thấy biến **`DATABASE_URL`** trong Streamlit Secrets hoặc biến môi trường!")
        st.stop()
        
    if not HAS_POSTGRES:
        st.error("⚠️ **Thiếu thư viện:** Thư viện kết nối Postgres (`psycopg2-binary`) chưa được cài đặt hoặc import thất bại!")
        st.stop()
        
    try:
        conn = psycopg2.connect(db_url)
        init_postgres_tables(conn)
        return PostgresWrapper(conn)
    except Exception as e:
        st.error(f"❌ **Lỗi kết nối Supabase PostgreSQL:** {e}")
        st.stop()

def reset_active_database():
    try:
        db_url = None
        if st.secrets and "DATABASE_URL" in st.secrets:
            db_url = st.secrets["DATABASE_URL"]
        elif "DATABASE_URL" in os.environ:
            db_url = os.environ["DATABASE_URL"]
            
        if db_url and HAS_POSTGRES:
            with psycopg2.connect(db_url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DROP TABLE IF EXISTS Grades CASCADE;")
                    cursor.execute("DROP TABLE IF EXISTS Lessons CASCADE;")
                    cursor.execute("DROP TABLE IF EXISTS Syllabus CASCADE;")
                    cursor.execute("DROP TABLE IF EXISTS Users CASCADE;")
                    cursor.execute("DROP TABLE IF EXISTS Messages CASCADE;")
                conn.commit()
                # Re-initialize
                init_postgres_tables(conn)
            return True
            
        # SQLite reset
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
        conn = sqlite3.connect(DB_FILE)
        init_sqlite_tables(conn)
        conn.close()
        return True
    except Exception as e:
        st.error(f"Lỗi khi reset cơ sở dữ liệu: {e}")
        return False

def verify_user(username, password):
    try:
        with get_db() as conn:
            user = conn.execute(
                "SELECT username, role FROM Users WHERE username = ? AND password = ?",
                (username, password)
            ).fetchone()
            if user:
                return dict(user)
    except Exception as e:
        st.error(f"Lỗi truy vấn người dùng: {e}")
    return None

def get_subjects():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT subject FROM Syllabus").fetchall()
            return [r['subject'] for r in rows]
    except Exception as e:
        st.error(f"Lỗi lấy danh sách môn học: {e}")
    return []

def get_syllabus(subject):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT content FROM Syllabus WHERE subject = ?", (subject,)).fetchone()
            if row:
                return row['content']
    except Exception as e:
        st.error(f"Lỗi lấy lộ trình học: {e}")
    return None

def get_syllabus_with_textbook(subject):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT content, textbook_content, pdf_file_path, total_lessons FROM Syllabus WHERE subject = ?", (subject,)).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        st.error(f"Lỗi lấy lộ trình và sách giáo khoa: {e}")
    return None

def save_syllabus(subject, content, textbook_content, pdf_file_path, total_lessons):
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO Syllabus (subject, content, textbook_content, pdf_file_path, total_lessons) 
                VALUES (?, ?, ?, ?, ?)
                """,
                (subject, content, textbook_content, pdf_file_path, total_lessons)
            )
            conn.commit()
            return True
    except Exception as e:
        st.error(f"Lỗi lưu lộ trình học: {e}")
    return False

def get_lessons_for_subject(subject):
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, lesson_number, title, duration FROM Lessons WHERE subject = ? ORDER BY lesson_number ASC",
                (subject,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        st.error(f"Lỗi lấy danh sách bài học: {e}")
    return []

def get_lesson_detail(subject, lesson_number):
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, lesson_number, title, lecture_content, questions, duration, flashcards FROM Lessons WHERE subject = ? AND lesson_number = ?",
                (subject, lesson_number)
            ).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        st.error(f"Lỗi lấy thông tin bài học chi tiết: {e}")
    return None

def save_lesson(subject, lesson_number, title, lecture_content, questions_json, duration, flashcards_json):
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO Lessons (subject, lesson_number, title, lecture_content, questions, duration, flashcards)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subject, lesson_number, title, lecture_content, questions_json, duration, flashcards_json)
            )
            conn.commit()
            return True
    except Exception as e:
        st.error(f"Lỗi lưu bài giảng & đề thi: {e}")
    return False

def get_grades_for_parent():
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT g.id, g.student_username, g.score, g.submitted_at, g.answers, g.ai_feedback,
                       l.subject, l.lesson_number, l.title as lesson_title, l.questions as original_questions
                FROM Grades g
                JOIN Lessons l ON g.lesson_id = l.id
                ORDER BY g.submitted_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        st.error(f"Lỗi truy vấn bảng điểm phụ huynh: {e}")
    return []

def get_grade_for_student(student_username, lesson_id):
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, score, answers, ai_feedback, submitted_at FROM Grades WHERE student_username = ? AND lesson_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (student_username, lesson_id)
            ).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        st.error(f"Lỗi lấy điểm số học sinh: {e}")
    return None

def save_grade(student_username, lesson_id, answers_json, score, ai_feedback_json):
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO Grades (student_username, lesson_id, answers, score, ai_feedback)
                VALUES (?, ?, ?, ?, ?)
                """,
                (student_username, lesson_id, answers_json, score, ai_feedback_json)
            )
            conn.commit()
            return True
    except Exception as e:
        st.error(f"Lỗi lưu kết quả bài làm: {e}")
    return False

def save_message(sender, message):
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO Messages (sender, message)
                VALUES (?, ?)
                """,
                (sender, message)
            )
            conn.commit()
            return True
    except Exception as e:
        st.error(f"Lỗi gửi tin nhắn: {e}")
    return False

def get_messages(limit=25):
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT sender, message, created_at FROM Messages ORDER BY id DESC LIMIT {limit}"
            ).fetchall()
            return list(reversed([dict(r) for r in rows]))
    except Exception as e:
        st.error(f"Lỗi tải tin nhắn: {e}")
    return []


# --- TRÍCH XUẤT VĂN BẢN TỪ FILE PDF ---

def extract_text_from_pdf(uploaded_file):
    try:
        reader = pypdf.PdfReader(uploaded_file)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        return text
    except Exception as e:
        st.error(f"Lỗi trích xuất văn bản từ tệp PDF: {e}")
        return ""


# --- LƯU TRỮ FILE PDF LÊN MÂY (SUPABASE STORAGE) ---

def save_pdf_bytes(pdf_bytes, subject):
    try:
        import requests
        import hashlib
        import os
        
        # 1. Lấy Supabase URL từ Secrets hoặc Env
        supabase_url = None
        if st.secrets and "SUPABASE_URL" in st.secrets:
            supabase_url = st.secrets["SUPABASE_URL"].strip().rstrip('/')
        elif "SUPABASE_URL" in os.environ:
            supabase_url = os.environ["SUPABASE_URL"].strip().rstrip('/')
            
        if not supabase_url:
            supabase_url = "https://ubaupchqavybpjpxjmle.supabase.co"
            
        # 2. Định dạng tên file sạch bằng MD5 tránh ký tự tiếng Việt
        hash_name = hashlib.md5(subject.encode('utf-8')).hexdigest()
        file_name = f"{hash_name}.pdf"
        
        # 3. Lấy API Key từ Secrets
        supabase_key = None
        if st.secrets and "SUPABASE_KEY" in st.secrets:
            supabase_key = st.secrets["SUPABASE_KEY"]
        elif "SUPABASE_KEY" in os.environ:
            supabase_key = os.environ["SUPABASE_KEY"]
            
        if not supabase_key:
            supabase_key = "sb_publishable_zbNs1LxLDkgdHLJ4RE6JYA_K9uJ3aFP"
            
        # Đường dẫn API upload của Supabase Storage
        upload_url = f"{supabase_url}/storage/v1/object/textbooks/{file_name}"
        
        headers = {
            "Authorization": f"Bearer {supabase_key}",
            "apikey": supabase_key,
            "Content-Type": "application/pdf"
        }
        
        # 4. Gửi yêu cầu HTTP PUT để tải file trực tiếp lên Supabase Storage textbooks bucket
        response = requests.put(upload_url, headers=headers, data=pdf_bytes)
        
        if response.status_code in [200, 201] or "Duplicate" in response.text:
            # Trả về đường link Public URL của cuốn sách
            public_url = f"{supabase_url}/storage/v1/object/public/textbooks/{file_name}"
            return public_url
        else:
            st.error(f"Lỗi phản hồi từ Supabase Storage: {response.text} (Mã: {response.status_code})")
            return ""
    except Exception as e:
        st.error(f"Lỗi khi lưu tệp PDF lên Supabase Cloud: {e}")
        return ""


# --- THIẾT LẬP GIAO DIỆN & TỔNG QUAN ---

def inject_custom_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
        
        /* Thay đổi màu nền toàn trang sang tươi sáng */
        html, body, [data-testid="stAppViewContainer"] {
            font-family: 'Inter', sans-serif !important;
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%) !important;
            color: #1e293b !important;
        }
        
        /* Thanh sidebar sáng màu và chuyên nghiệp */
        [data-testid="stSidebar"] {
            background-color: #ffffff !important;
            border-right: 1px solid #e2e8f0 !important;
        }
        
        /* Font chữ và tiêu đề tối slate */
        h1, h2, h3, h4, h5, h6 {
            font-family: 'Inter', sans-serif !important;
            font-weight: 700 !important;
            color: #0f172a !important;
        }
        
        /* ÉP BUỘC MÀU CHỮ TỐI (DARK SLATE) CHO TẤT CẢ VĂN BẢN ĐỂ DỄ ĐỌC TRÊN NỀN SÁNG */
        div[data-testid="stMarkdownContainer"] p,
        div[data-testid="stMarkdownContainer"] span,
        div[data-testid="stMarkdownContainer"] li,
        div[data-testid="stMarkdownContainer"] h1,
        div[data-testid="stMarkdownContainer"] h2,
        div[data-testid="stMarkdownContainer"] h3,
        div[data-testid="stMarkdownContainer"] h4,
        div[data-testid="stMarkdownContainer"] h5,
        div[data-testid="stMarkdownContainer"] h6,
        div[data-testid="stMarkdownContainer"] ol,
        div[data-testid="stMarkdownContainer"] ul,
        div[data-testid="stMarkdownContainer"] code,
        div[data-testid="stMarkdownContainer"] label,
        .stMarkdown p,
        .stMarkdown span,
        .stMarkdown li,
        .stMarkdown div,
        .stMarkdown h1,
        .stMarkdown h2,
        .stMarkdown h3,
        .stMarkdown h4,
        .stMarkdown h5,
        .stMarkdown h6,
        label,
        .stWidgetFormLabel {
            color: #1e293b !important;
        }

        /* Đảm bảo các công thức Toán học KaTeX hiển thị màu tối */
        .katex, .katex-html, .katex * {
            color: #1e293b !important;
        }
        
        /* Chữ gradient sư phạm cho Phụ huynh (tím-indigo) */
        .parent-gradient-text {
            background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%) !important;
            -webkit-background-clip: text !important;
            -webkit-text-fill-color: transparent !important;
            font-weight: 800;
        }

        /* Chữ gradient sư phạm cho Học sinh (xanh dương) */
        .student-gradient-text {
            background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%) !important;
            -webkit-background-clip: text !important;
            -webkit-text-fill-color: transparent !important;
            font-weight: 800;
        }
        
        /* Card trắng trang nhã, đổ bóng nhẹ */
        .custom-card {
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 12px !important;
            padding: 24px !important;
            margin-bottom: 20px !important;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05) !important;
            color: #1e293b !important;
        }
        
        /* Huy hiệu bắt mắt */
        .badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        .badge-parent {
            background-color: rgba(124, 58, 237, 0.1) !important;
            color: #7c3aed !important;
            border: 1px solid rgba(124, 58, 237, 0.2) !important;
        }
        
        .badge-student {
            background-color: rgba(2, 132, 199, 0.1) !important;
            color: #0284c7 !important;
            border: 1px solid rgba(2, 132, 199, 0.2) !important;
        }
        
        .score-display {
            font-size: 3rem;
            font-weight: 800;
            text-align: center;
            margin: 15px 0;
        }
        
        .score-good {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
            -webkit-background-clip: text !important;
            -webkit-text-fill-color: transparent !important;
        }
        
        .score-average {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%) !important;
            -webkit-background-clip: text !important;
            -webkit-text-fill-color: transparent !important;
        }
        
        .score-poor {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%) !important;
            -webkit-background-clip: text !important;
            -webkit-text-fill-color: transparent !important;
        }
        
        /* Sửa lại nền Form */
        div[data-testid="stForm"] {
            background-color: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 12px !important;
            padding: 24px !important;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05) !important;
        }
        
        .stButton>button {
            border-radius: 8px !important;
            font-weight: 600 !important;
            transition: all 0.2s ease !important;
        }
        
        /* Làm nổi bật các tab trong Streamlit */
        div[data-baseweb="tab-list"] {
            background-color: #f1f5f9 !important;
            border-radius: 8px !important;
            padding: 4px !important;
        }
        
        button[data-baseweb="tab"] {
            border-radius: 6px !important;
            color: #475569 !important;
        }
        
        button[data-baseweb="tab"][aria-selected="true"] {
            background-color: #ffffff !important;
            color: #0f172a !important;
            box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1) !important;
        }
        /* CSS phong cách Flashcard lật 3D */
        .flashcard-container {
            display: flex;
            justify-content: center;
            align-items: center;
            margin: 20px auto;
            perspective: 1000px;
        }
        
        .flip-card {
            background-color: transparent;
            width: 480px;
            height: 280px;
            perspective: 1000px;
            cursor: pointer;
        }
        
        .flip-card-inner {
            position: relative;
            width: 100%;
            height: 100%;
            text-align: center;
            transition: transform 0.6s cubic-bezier(0.4, 0, 0.2, 1);
            transform-style: preserve-3d;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1);
            border-radius: 16px;
        }
        
        /* Khi hover thì xoay lật */
        .flip-card:hover .flip-card-inner {
            transform: rotateY(180deg);
        }
        
        .flip-card-front, .flip-card-back {
            position: absolute;
            width: 100%;
            height: 100%;
            -webkit-backface-visibility: hidden;
            backface-visibility: hidden;
            border-radius: 16px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 30px;
            box-sizing: border-box;
        }
        
        .flip-card-front {
            background: linear-gradient(135deg, #0284c7 0%, #3b82f6 100%);
            color: #ffffff;
        }
        
        .flip-card-back {
            background-color: #ffffff;
            color: #1e293b;
            border: 2px solid #e2e8f0;
            transform: rotateY(180deg);
        }
        
        .flashcard-title {
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-bottom: 12px;
            opacity: 0.8;
            font-weight: 700;
        }
        
        .flashcard-content {
            font-size: 1.45rem;
            font-weight: 600;
            line-height: 1.4;
        }
        
        .flashcard-hint {
            font-size: 0.8rem;
            margin-top: 20px;
            opacity: 0.7;
            font-style: italic;
        }
        
        /* CSS bong bóng và cửa sổ Chat nổi lơ lửng */
        div[data-testid="stVerticalBlock"]:has(> div.element-container > div.floating-chat-collapsed) {
            position: fixed !important;
            bottom: 30px !important;
            right: 30px !important;
            z-index: 99999 !important;
            width: 60px !important;
            height: 60px !important;
            border-radius: 50% !important;
            background: linear-gradient(135deg, #0284c7 0%, #3b82f6 100%) !important;
            box-shadow: 0 10px 25px rgba(59, 130, 246, 0.4) !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            border: none !important;
            padding: 0 !important;
        }

        div[data-testid="stVerticalBlock"]:has(> div.element-container > div.floating-chat-collapsed) button {
            background: transparent !important;
            color: white !important;
            font-size: 26px !important;
            border: none !important;
            width: 100% !important;
            height: 100% !important;
            border-radius: 50% !important;
            padding: 0 !important;
            box-shadow: none !important;
        }

        div[data-testid="stVerticalBlock"]:has(> div.element-container > div.floating-chat-expanded) {
            position: fixed !important;
            bottom: 30px !important;
            right: 30px !important;
            z-index: 99999 !important;
            width: 360px !important;
            height: 520px !important;
            background-color: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 16px !important;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04) !important;
            padding: 15px !important;
            display: flex !important;
            flex-direction: column !important;
            overflow: hidden !important;
            box-sizing: border-box !important;
        }

        .chat-history-container {
            display: flex;
            flex-direction: column;
            flex-grow: 1;
            overflow-y: auto;
            margin-bottom: 10px;
            padding-right: 5px;
            height: 380px;
            border: 1px solid #f1f5f9;
            border-radius: 8px;
            padding: 8px;
            background-color: #fafafa;
        }

        .chat-bubble {
            margin-bottom: 10px;
            padding: 8px 12px;
            border-radius: 12px;
            font-size: 0.85rem;
            max-width: 85%;
            line-height: 1.4;
            display: inline-block;
        }

        .chat-bubble-self {
            background: linear-gradient(135deg, #0284c7 0%, #3b82f6 100%);
            color: #ffffff;
            align-self: flex-end;
            margin-left: auto;
            border-bottom-right-radius: 2px;
        }

        .chat-bubble-other {
            background-color: #e2e8f0;
            color: #1e293b;
            align-self: flex-start;
            margin-right: auto;
            border-bottom-left-radius: 2px;
        }

        .chat-meta {
            font-size: 0.65rem;
            margin-top: 4px;
            opacity: 0.85;
        }
        
        </style>
        """,
        unsafe_allow_html=True
    )

def inject_selection_speak_js():
    # Sử dụng iframe Streamlit Component ẩn (height=0) để nhúng JavaScript lên trang cha
    # Đoạn script này bắt sự kiện bôi đen (mouseup) để hiện nút loa đọc phát âm
    st.components.v1.html(
        """
        <script>
            (function() {
                const parentDoc = window.parent.document;
                
                // Tránh tạo trùng lặp sự kiện khi Streamlit tự động chạy lại code (rerun)
                if (window.parent.__selectionSpeakInitialized) return;
                window.parent.__selectionSpeakInitialized = true;
                
                parentDoc.addEventListener('mouseup', function(e) {
                    const selection = window.parent.getSelection().toString().trim();
                    let button = parentDoc.getElementById('floating-speak-btn');
                    
                    if (selection.length > 0) {
                        if (!button) {
                            button = parentDoc.createElement('div');
                            button.id = 'floating-speak-btn';
                            button.innerHTML = '🔊 Đọc phát âm';
                            button.style.position = 'absolute';
                            button.style.background = 'linear-gradient(135deg, #0284c7 0%, #3b82f6 100%)';
                            button.style.color = '#ffffff';
                            button.style.padding = '6px 12px';
                            button.style.borderRadius = '20px';
                            button.style.cursor = 'pointer';
                            button.style.fontSize = '12px';
                            button.style.fontWeight = 'bold';
                            button.style.boxShadow = '0 4px 10px rgba(0,0,0,0.15)';
                            button.style.zIndex = '99999';
                            button.style.transition = 'opacity 0.2s';
                            parentDoc.body.appendChild(button);
                        }
                        
                        // Đặt vị trí bong bóng nổi phía trên con trỏ chuột bôi đen
                        button.style.left = (e.pageX - 20) + 'px';
                        button.style.top = (e.pageY - 40) + 'px';
                        button.style.display = 'block';
                        button.style.opacity = '1';
                        
                        // Xử lý sự kiện click nút đọc phát âm
                        button.onclick = function(event) {
                            event.stopPropagation();
                            
                            const utterance = new window.parent.SpeechSynthesisUtterance(selection);
                            
                            // Kiểm tra tiếng Việt có dấu để điều chỉnh giọng đọc vi-VN, ngược lại chọn giọng Anh en-US
                            const containsVietnamese = /[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]/i.test(selection);
                            if (containsVietnamese) {
                                utterance.lang = 'vi-VN';
                            } else {
                                utterance.lang = 'en-US';
                            }
                            
                            window.parent.speechSynthesis.speak(utterance);
                            
                            // Ẩn nút sau khi phát âm xong
                            button.style.display = 'none';
                            window.parent.getSelection().removeAllRanges();
                        };
                    } else {
                        if (button) {
                            button.style.display = 'none';
                        }
                    }
                });
                
                // Ẩn nút khi click chuột xuống
                parentDoc.addEventListener('mousedown', function(e) {
                    const button = parentDoc.getElementById('floating-speak-btn');
                    if (button && e.target !== button) {
                        button.style.display = 'none';
                    }
                });
            })();
        </script>
        """,
        height=0,
        width=0
    )

def show_sidebar_chat():
    current_user = st.session_state.get('username')
    if not current_user:
        return
        
    st.sidebar.markdown("---")
    
    col_t, col_ref = st.sidebar.columns([4, 1.2])
    with col_t:
        st.markdown("💬 **Trò Chuyện Gia Đình**")
    with col_ref:
        if st.button("🔄", key="chat_manual_refresh", help="Cập nhật tin nhắn", use_container_width=True):
            st.rerun()
            
    # Tự động click nút làm mới mỗi 10 giây dưới nền để đồng bộ tin nhắn tự động
    st.components.v1.html(
        """
        <script>
            (function() {
                if (window.parent.__chatTimerInitialized) return;
                window.parent.__chatTimerInitialized = true;
                
                setInterval(function() {
                    const parentDoc = window.parent.document;
                    
                    // Kiểm tra xem người dùng có đang gõ chữ hoặc chọn/tương tác với bất kỳ ô nhập liệu nào không
                    const activeEl = parentDoc.activeElement;
                    const isInteracting = activeEl && (
                        activeEl.tagName === 'INPUT' || 
                        activeEl.tagName === 'TEXTAREA' || 
                        activeEl.tagName === 'SELECT' ||
                        activeEl.getAttribute('role') === 'combobox' ||
                        activeEl.getAttribute('role') === 'textbox' ||
                        activeEl.getAttribute('contenteditable') === 'true'
                    );
                    
                    if (isInteracting) {
                        return; // Bỏ qua tự động làm mới để tránh mất tiêu điểm và mất chữ khi đang gõ/chọn
                    }
                    
                    const buttons = parentDoc.querySelectorAll('button');
                    for (let btn of buttons) {
                        if (btn.title === "Cập nhật tin nhắn" || (btn.innerText && btn.innerText.includes("🔄"))) {
                            btn.click();
                            break;
                        }
                    }
                }, 10000);
            })();
        </script>
        """,
        height=0,
        width=0
    )
    
    # Lấy 15 tin nhắn gần nhất
    messages = get_messages(15)
    
    # Thiết kế HTML hộp chat cuộn trong sidebar
    chat_html = '<div class="sidebar-chat-container" style="height: 220px; overflow-y: auto; border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px; background-color: #f8fafc; margin-bottom: 10px; display: flex; flex-direction: column;">'
    if not messages:
        chat_html += '<div style="color:#94a3b8; font-size:0.75rem; text-align:center; margin-top:90px;">Chưa có tin nhắn.</div>'
    else:
        for msg in messages:
            sender_label = "Bạn" if msg['sender'] == current_user else ("Phụ huynh" if msg['sender'] == 'phuhuynh' else "Con")
            
            # Người đăng nhập: chữ đen, nền xanh nhạt (#9fc5e8)
            # Người khác: chữ đen, nền trắng (#ffffff)
            if msg['sender'] == current_user:
                bubble_style = "background-color: #9fc5e8; color: #1e293b; align-self: flex-end; margin-left: auto; border-bottom-right-radius: 2px; border: 1px solid #7ea6cd;"
            else:
                bubble_style = "background-color: #ffffff; color: #1e293b; align-self: flex-start; margin-right: auto; border-bottom-left-radius: 2px; border: 1px solid #cbd5e1;"
            
            try:
                if isinstance(msg['created_at'], str):
                    time_part = msg['created_at'].split()
                    time_str = time_part[1][:5] if len(time_part) > 1 else msg['created_at'][:5]
                else:
                    time_str = msg['created_at'].strftime("%H:%M")
            except Exception:
                time_str = ""
                
            # Tên người gửi màu da cam (#e69138)
            chat_html += f'<div style="margin-bottom: 8px; padding: 6px 10px; border-radius: 10px; font-size: 0.75rem; max-width: 85%; line-height: 1.3; {bubble_style}">'
            chat_html += f'<div style="font-size:0.6rem; font-weight:700; color: #e69138; margin-bottom:2px;">{sender_label}</div>'
            chat_html += f'<div style="word-break: break-word;">{msg["message"]}</div>'
            chat_html += f'<div style="font-size:0.55rem; text-align:right; margin-top:2px; opacity:0.7;">{time_str}</div>'
            chat_html += '</div>'
            
    chat_html += '</div>'
    
    # Loại bỏ các khoảng trắng thừa đầu dòng và xuống dòng để Streamlit không biến thành thẻ pre code
    chat_html = chat_html.replace('\n', '').replace('    ', '')
    
    st.sidebar.markdown(chat_html, unsafe_allow_html=True)
    
    # Form nhập tin nhắn trong sidebar (điều chỉnh cột [4, 1.5] để chữ "Gửi" không bị tràn xuống dòng)
    with st.sidebar.form("sidebar_chat_form", clear_on_submit=True):
        col_inp, col_send = st.columns([4, 1.5])
        with col_inp:
            new_msg = st.text_input("Nhắn tin...", label_visibility="collapsed", placeholder="Nhập tin nhắn...", key="sidebar_chat_input")
        with col_send:
            btn_send = st.form_submit_button("Gửi")
            
        if btn_send and new_msg.strip():
            save_message(current_user, new_msg.strip())
            st.rerun()

def get_gemini_client():
    # Khởi tạo giá trị mặc định từ biến môi trường nếu session state chưa có
    if "gemini_api_key" not in st.session_state:
        st.session_state["gemini_api_key"] = os.environ.get("GEMINI_API_KEY", "")
        
    # Chỉ hiển thị hộp cấu hình API Key đối với tài khoản vai trò là Phụ huynh
    if st.session_state.get('role') == 'parent':
        with st.sidebar.expander("🔑 Cấu hình Gemini API Key", expanded=False):
            entered_key = st.text_input(
                "Nhập API Key (nhiều key cách nhau bằng dấu phẩy):", 
                value=st.session_state["gemini_api_key"], 
                type="password",
                help="Ví dụ: Key1, Key2, Key3..."
            )
            if entered_key != st.session_state["gemini_api_key"]:
                st.session_state["gemini_api_key"] = entered_key
                st.rerun()
            
    api_key = st.session_state["gemini_api_key"]
    
    if not api_key:
        if st.session_state.get('role') == 'parent':
            st.sidebar.warning("⚠️ Chưa cấu hình Gemini API Key! Vui lòng mở rộng hộp '🔑 Cấu hình Gemini API Key' ở trên để nhập key.")
        return None
        
    try:
        # Hỗ trợ danh sách API key cách nhau bởi dấu phẩy
        keys = [k.strip() for k in api_key.split(",") if k.strip()]
        if keys:
            st.session_state['gemini_api_keys_pool'] = keys
            if 'active_key_idx' not in st.session_state:
                st.session_state['active_key_idx'] = 0
            idx = st.session_state['active_key_idx'] % len(keys)
            active_key = keys[idx]
            return genai.Client(api_key=active_key)
    except Exception as e:
        st.sidebar.error(f"Lỗi khởi tạo Gemini Client: {e}")
    return None

def generate_content_with_retry(client, model, contents, config=None, max_retries=5, initial_delay=3):
    delay = initial_delay
    last_error = None
    for attempt in range(max_retries):
        try:
            if config is not None:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config
                )
            else:
                response = client.models.generate_content(
                    model=model,
                    contents=contents
                )
            return response
        except Exception as e:
            last_error = e
            err_msg = str(e).upper()
            
            # Nếu hết hạn mức (429 / LIMIT / EXHAUSTED) và có nhiều API key dự phòng, tự động đổi key
            if any(x in err_msg for x in ["429", "LIMIT", "EXHAUSTED"]) and 'gemini_api_keys_pool' in st.session_state and len(st.session_state['gemini_api_keys_pool']) > 1:
                st.session_state['active_key_idx'] = (st.session_state.get('active_key_idx', 0) + 1) % len(st.session_state['gemini_api_keys_pool'])
                new_key = st.session_state['gemini_api_keys_pool'][st.session_state['active_key_idx']]
                st.toast("🔄 Khóa hiện tại hết hạn mức. Đang chuyển sang API Key dự phòng...", icon="🔄")
                # Khởi tạo lại client mới với key dự phòng
                client = genai.Client(api_key=new_key)
                time.sleep(1)
                continue
                
            if any(x in err_msg for x in ["503", "429", "UNAVAILABLE", "HIGH DEMAND", "BUSY", "LIMIT", "TEMPORARY"]):
                st.toast(f"⏳ Máy chủ Gemini bận (Lần thử {attempt + 1}/{max_retries}). Thử lại sau {delay}s...", icon="⚠️")
                time.sleep(delay)
                delay *= 2
            else:
                raise e
    raise last_error


# --- GIAO DIỆN PHỤ HUYNH ---

def show_parent_interface(client):
    st.markdown("<h1>Không Gian <span class='parent-gradient-text'>Phụ Huynh</span> <span class='badge badge-parent'>Parent Portal</span></h1>", unsafe_allow_html=True)
    
    tab1, tab2, tab3, tab4 = st.tabs(["🗺️ Lộ Trình Học Tập", "📝 Soạn Giáo Án & Đề Thi", "📊 Giám Sát Kết Quả", "⚙️ Quản Lý Dữ Liệu"])
    
    # TAB 1: QUẢN LÝ LỘ TRÌNH VÀ TẢI PDF
    with tab1:
        st.subheader("Quản lý Chương trình học & Tạo Lộ trình từ PDF")
        
        col1, col2 = st.columns([1, 2])
        
        with col1:
            st.write("### Nhập thông tin môn học")
            existing_subjects = get_subjects()
            
            subject_options = existing_subjects + ["+ Thêm môn học mới..."] if existing_subjects else ["+ Thêm môn học mới..."]
            
            selected_option = st.selectbox(
                "Chọn môn học:", 
                options=subject_options, 
                key="parent_subject_select"
            )
            
            if selected_option == "+ Thêm môn học mới...":
                selected_subject = st.text_input(
                    "Nhập tên môn học mới:", 
                    placeholder="Ví dụ: Toán lớp 6, Tiếng Anh lớp 6...", 
                    key="parent_new_subject_input"
                )
            else:
                selected_subject = selected_option
            
            uploaded_pdf = st.file_uploader("Tải tệp sách giáo khoa lên (PDF):", type=["pdf"])
            
            btn_create_syllabus = st.button("AI Tạo Lộ Trình Học 🚀", use_container_width=True)
            
        with col2:
            st.write("### Lộ trình học chi tiết")
            
            # Khởi tạo trạng thái duyệt
            if 'show_approval' not in st.session_state:
                st.session_state['show_approval'] = False
                
            if btn_create_syllabus:
                if not selected_subject:
                    st.error("Vui lòng điền hoặc chọn môn học!")
                elif not uploaded_pdf:
                    st.error("Vui lòng tải lên file sách giáo khoa PDF!")
                else:
                    # Lưu tạm bytes của PDF để ghi file khi phụ huynh phê duyệt
                    pdf_bytes = uploaded_pdf.getvalue()
                    st.session_state['temp_pdf_bytes'] = pdf_bytes
                    st.session_state['temp_syllabus_subject'] = selected_subject
                    
                    # Tạo file tạm cục bộ để upload lên Gemini File API
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                        temp_file.write(pdf_bytes)
                        temp_file_path = temp_file.name
                        
                    with st.spinner("AI đang quét file PDF sách giáo khoa bằng công nghệ nhận diện hình ảnh/chữ viết... Vui lòng đợi trong giây lát."):
                        try:
                            # Tải tệp lên Gemini File API
                            file_ref = client.files.upload(file=temp_file_path)
                            
                            # Chờ tệp xử lý xong
                            while file_ref.state.name == "PROCESSING":
                                time.sleep(1)
                                file_ref = client.files.get(name=file_ref.name)
                                
                            if file_ref.state.name == "FAILED":
                                st.error("Gemini không thể xử lý tệp PDF này!")
                                os.unlink(temp_file_path)
                            else:
                                # Tạo prompt yêu cầu AI tạo lộ trình
                                prompt = f"""
                                Bạn là một chuyên gia thiết kế chương trình học. Hãy phân tích tài liệu sách giáo khoa PDF được đính kèm dưới đây và xây dựng một lộ trình học tập khoa học, logic chia theo từng buổi học (Buổi 1, Buổi 2...). 
                                
                                Môn học: {selected_subject}

                                Hãy phân tích và viết lộ trình tổng thể chi tiết. Mỗi buổi học phải nêu rõ:
                                1. Tên buổi (Ví dụ: Buổi 1: Tập hợp và các phần tử của tập hợp)
                                2. Mục tiêu kiến thức cần đạt
                                3. Các khái niệm cốt lõi (sử dụng định dạng LaTeX như $...$ hoặc $$...$$ cho các công thức nếu cần thiết)
                                
                                Trình bày dưới dạng Markdown đẹp mắt, khoa học, dễ đọc để làm căn cứ cho việc soạn bài giảng sau này.
                                """
                                response = generate_content_with_retry(
                                    client,
                                    model='gemini-2.5-flash',
                                    contents=[file_ref, prompt]
                                )
                                syllabus_content = response.text
                                
                                # Lưu thông tin tạm thời để chờ phê duyệt
                                st.session_state['temp_syllabus_content'] = syllabus_content
                                st.session_state['show_approval'] = True
                                
                                # Trích xuất thử văn bản text để làm dự phòng cục bộ
                                try:
                                    extracted_text = extract_text_from_pdf(uploaded_pdf)
                                    st.session_state['temp_textbook_content'] = extracted_text if extracted_text else "PDF Scan"
                                except Exception:
                                    st.session_state['temp_textbook_content'] = "PDF Scan"
                                    
                                # Xóa file khỏi API của Gemini sau khi xử lý xong
                                client.files.delete(name=file_ref.name)
                                os.unlink(temp_file_path)
                        except Exception as e:
                            st.error(f"Lỗi khi xử lý file PDF và gọi Gemini: {e}")
                            if os.path.exists(temp_file_path):
                                os.unlink(temp_file_path)
            
            # Xử lý hiển thị chế độ Phê duyệt
            if st.session_state['show_approval']:
                st.warning("⚠️ LỘ TRÌNH ĐANG ĐƯỢC ĐỀ XUẤT. PHỤ HUYNH CẦN PHÊ DUYỆT ĐỂ LƯU VÀO LỊCH HỌC TẬP.")
                st.markdown(st.session_state['temp_syllabus_content'])
                st.write("---")
                st.write("### 🛠️ Góp ý điều chỉnh lộ trình")
                feedback = st.text_area(
                    "Nhập những điểm cần sửa đổi, thêm bớt (AI sẽ thiết kế lại lộ trình học dựa trên ý kiến này):", 
                    placeholder="Ví dụ: Rút ngắn lộ trình còn 20 buổi học; tập trung nhiều hơn vào bài tập thực hành; hoặc thêm 1 buổi ôn tập chương ở bài số 10...",
                    key="syllabus_feedback_text"
                )
                
                if st.button("AI Điều Chỉnh Lộ Trình Lại 🔄", use_container_width=True):
                    if not feedback.strip():
                        st.error("Vui lòng nhập nội dung cần điều chỉnh!")
                    else:
                        with st.spinner("AI đang điều chỉnh lại lộ trình học dựa trên ý kiến của bạn..."):
                            try:
                                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                                    temp_file.write(st.session_state['temp_pdf_bytes'])
                                    temp_file_path = temp_file.name
                                    
                                file_ref = client.files.upload(file=temp_file_path)
                                while file_ref.state.name == "PROCESSING":
                                    time.sleep(1)
                                    file_ref = client.files.get(name=file_ref.name)
                                    
                                if file_ref.state.name == "FAILED":
                                    st.error("Gemini không thể xử lý tệp PDF để điều chỉnh!")
                                    os.unlink(temp_file_path)
                                else:
                                    prompt = f"""
                                    Bạn là một chuyên gia thiết kế chương trình học. Trước đó bạn đã đề xuất một lộ trình học tập cho môn học: {st.session_state['temp_syllabus_subject']}.
                                    
                                    Lộ trình đề xuất trước đó:
                                    {st.session_state['temp_syllabus_content']}
                                    
                                    Phụ huynh đã xem lộ trình trên và đưa ra yêu cầu điều chỉnh, bổ sung như sau:
                                    "{feedback}"
                                    
                                    Hãy phân tích tệp PDF sách giáo khoa đính kèm cùng với ý kiến đóng góp của phụ huynh để thiết kế lại một lộ trình học tập khoa học, logic chia theo từng buổi học. 
                                    Đáp ứng chính xác và đầy đủ các yêu cầu điều chỉnh của phụ huynh (ví dụ: rút ngắn số buổi học, thêm bớt bài, tập trung trọng tâm...).
                                    
                                    Mỗi buổi học phải nêu rõ:
                                    1. Tên buổi (Ví dụ: Buổi 1: Tập hợp và các phần tử của tập hợp)
                                    2. Mục tiêu kiến thức cần đạt
                                    3. Các khái niệm cốt lõi (sử dụng định dạng LaTeX cho các công thức)
                                    
                                    Trình bày dưới dạng Markdown đẹp mắt, khoa học, dễ đọc.
                                    """
                                    response = generate_content_with_retry(
                                        client,
                                        model='gemini-2.5-flash',
                                        contents=[file_ref, prompt]
                                    )
                                    st.session_state['temp_syllabus_content'] = response.text
                                    
                                    client.files.delete(name=file_ref.name)
                                    os.unlink(temp_file_path)
                                    st.success("Đã điều chỉnh lộ trình thành công! Xem lại lộ trình mới ở phía trên.")
                                    st.rerun()
                            except Exception as e:
                                st.error(f"Lỗi khi AI điều chỉnh lộ trình: {e}")
                                if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
                                    os.unlink(temp_file_path)
                                    
                st.write("---")
                # Cho phép phụ huynh tùy chỉnh tổng số buổi học trước khi phê duyệt
                total_lessons = st.number_input("Chốt tổng số buổi học cho lộ trình này (Ví dụ: 39):", min_value=1, max_value=150, value=30, step=1)
                
                col_app1, col_app2 = st.columns(2)
                with col_app1:
                    if st.button("Phê duyệt & Lưu Lộ Trình ✅", use_container_width=True):
                        # Lưu file PDF cục bộ
                        pdf_path = save_pdf_bytes(
                            st.session_state['temp_pdf_bytes'],
                            st.session_state['temp_syllabus_subject']
                        )
                        
                        # Lưu chính thức vào SQLite
                        success = save_syllabus(
                            st.session_state['temp_syllabus_subject'],
                            st.session_state['temp_syllabus_content'],
                            st.session_state['temp_textbook_content'],
                            pdf_path,
                            total_lessons
                        )
                        if success:
                            st.success(f"Đã phê duyệt và lưu lộ trình ({total_lessons} buổi) cho môn '{st.session_state['temp_syllabus_subject']}' thành công!")
                            # Reset trạng thái
                            st.session_state['show_approval'] = False
                            st.session_state['temp_syllabus_content'] = None
                            st.session_state['temp_pdf_bytes'] = None
                            st.rerun()
                with col_app2:
                    if st.button("Hủy bỏ đề xuất này ❌", use_container_width=True):
                        st.session_state['show_approval'] = False
                        st.session_state['temp_syllabus_content'] = None
                        st.session_state['temp_pdf_bytes'] = None
                        st.rerun()
            else:
                # Hiển thị lộ trình đã lưu trước đó nếu có
                if selected_subject:
                    saved_data = get_syllabus_with_textbook(selected_subject)
                    if saved_data and saved_data['content']:
                        st.success(f"📅 Lịch học tập đã phê duyệt (Tổng số: {saved_data.get('total_lessons', 30)} buổi):")
                        st.markdown(saved_data['content'])
                    else:
                        st.info("Chưa có lộ trình học được phê duyệt cho môn học này. Hãy sử dụng form bên trái để tải PDF sách giáo khoa và tạo lộ trình học.")
                else:
                    st.info("Vui lòng chọn hoặc thêm môn học ở bên trái để xem lộ trình học.")

    # TAB 2: SOẠN BÀI THEO BUỔI (DỰA TRÊN CẢ SÁCH GIÁO KHOA & LỊCH HỌC)
    with tab2:
        st.subheader("Soạn Giáo Án Chi Tiết & Đề Thi 15 Câu")
        
        existing_subjects = get_subjects()
        if not existing_subjects:
            st.warning("Vui lòng tạo lộ trình học trước ở Tab 'Lộ Trình Học Tập' trước khi soạn giáo án.")
        else:
            col_left, col_right = st.columns([1, 2])
            with col_left:
                selected_sub_lesson = st.selectbox("Chọn môn học để soạn giáo án:", existing_subjects, key="select_sub_lesson")
                
                # Đồng bộ thủ công để reset chỉ số buổi học khi phụ huynh chuyển môn học
                if 'prev_selected_sub_lesson' not in st.session_state:
                    st.session_state['prev_selected_sub_lesson'] = selected_sub_lesson
                if selected_sub_lesson != st.session_state['prev_selected_sub_lesson']:
                    st.session_state['prev_selected_sub_lesson'] = selected_sub_lesson
                    if "parent_lesson_number_selectbox" in st.session_state:
                        del st.session_state["parent_lesson_number_selectbox"]
                
                # Lấy dữ liệu lịch học tập & sách giáo khoa
                syllabus_data = get_syllabus_with_textbook(selected_sub_lesson)
                syllabus_content = syllabus_data['content'] if syllabus_data else ""
                textbook_content = syllabus_data['textbook_content'] if syllabus_data else ""
                pdf_file_path = syllabus_data['pdf_file_path'] if syllabus_data else ""
                total_lessons = syllabus_data['total_lessons'] if (syllabus_data and syllabus_data['total_lessons']) else 30
                
                with st.expander("Xem lịch học tập đã phê duyệt", expanded=False):
                    st.markdown(syllabus_content)
                
                # Dropdown chọn buổi theo danh sách từ 1 đến total_lessons
                lesson_options = list(range(1, total_lessons + 1))
                lesson_number = st.selectbox("Chọn buổi cần soạn giáo án:", options=lesson_options, key="parent_lesson_number_selectbox")
                
                btn_create_lesson = st.button("AI Soạn Giáo Án & Đề Thi ✏️", use_container_width=True)
                
            with col_right:
                st.write("### Giáo án & Đề kiểm tra đã tạo")
                
                current_lesson = get_lesson_detail(selected_sub_lesson, lesson_number)
                
                if btn_create_lesson:
                    # Kiểm tra xem có file PDF gốc không (cục bộ hoặc trên mây Supabase)
                    use_multimodal = False
                    downloaded_temp_pdf = ""
                    
                    if pdf_file_path:
                        if pdf_file_path.startswith("http://") or pdf_file_path.startswith("https://"):
                            # Tải tệp từ Supabase Storage về thư mục tạm
                            with st.spinner("Đang tải tệp sách giáo khoa PDF từ bộ lưu trữ đám mây Supabase..."):
                                try:
                                    import requests
                                    dl_response = requests.get(pdf_file_path)
                                    if dl_response.status_code == 200:
                                        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                                            tmp_pdf.write(dl_response.content)
                                            downloaded_temp_pdf = tmp_pdf.name
                                        use_multimodal = True
                                    else:
                                        st.warning("Không thể tải sách giáo khoa từ link đám mây, tự động chuyển sang chế độ văn bản.")
                                except Exception as dl_err:
                                    st.warning(f"Lỗi tải sách từ đám mây: {dl_err}. Chuyển sang chế độ văn bản.")
                        elif os.path.exists(pdf_file_path):
                            downloaded_temp_pdf = pdf_file_path
                            use_multimodal = True
                    
                    with st.spinner(f"AI đang soạn giáo án & bộ 15 câu hỏi kiểm tra cho Buổi {lesson_number}..."):
                        try:
                            prompt = f"""
                            Bạn là một chuyên gia giáo dục và biên soạn tài liệu học tập. Dựa trên Lịch học tập tổng thể và Nội dung sách giáo khoa được cung cấp, hãy soạn thảo bài giảng chi tiết cùng bộ đề kiểm tra cuối buổi cho Buổi học số {lesson_number}.
                            
                            Môn học: {selected_sub_lesson}
                            
                            Lịch học tập tổng thể đã phê duyệt:
                            {syllabus_content}

                            Hãy tham khảo sách giáo khoa PDF đi kèm để viết bài soạn chi tiết.

                            Hãy biên soạn theo các tiêu chí nghiêm ngặt sau:

                            A. Yêu cầu chi tiết về BÀI GIẢNG (lecture_content):
                            Phải viết đầy đủ nội dung bài giảng chi tiết, dễ hiểu cho học sinh lớp 6, trình bày Markdown đẹp mắt và chia cấu trúc đúng 3 phần chính sau:
                            1. Mục tiêu bài học (Learning Objectives):
                               - Kiến thức: Người học hiểu và nhớ được những gì?
                               - Kỹ năng: Người học thực hiện được thao tác hoặc giải quyết vấn đề gì?
                               - Thái độ: Sự thay đổi trong tư duy, nhận thức của người học sau bài học.
                            2. Bài giảng E-learning (Lesson Plan):
                               - Lý thuyết trọng tâm: Trình bày lý thuyết ngắn gọn, cô đọng, dễ hiểu, ưu tiên sử dụng từ khóa cốt lõi (sử dụng công thức LaTeX $...$ hoặc $$...$$ nếu có).
                               - Minh họa thực tế (Ví dụ): Đưa ra case-study, ví dụ minh họa trực quan hoặc các tình huống áp dụng cụ thể trong thực tế.
                               - Tương tác/Thực hành: Đan xen các câu hỏi, bài tập nhỏ cụ thể để học sinh tự suy nghĩ và trả lời ngay lập tức, kèm theo đáp án (Trả lời) và giải thích chi tiết.
                            3. Tổng kết & Vận dụng:
                               - Tóm tắt (Summary): Nhấn mạnh từ 3 đến 5 điểm chính cốt lõi nhất của bài học.
                               - Kiểm tra cô đọng: Kiểm tra mức độ hiểu bài của học sinh bằng các câu hỏi nhanh cực kỳ ngắn gọn.
                               - Giao nhiệm vụ (Call to Action): Hướng dẫn học sinh cách tự ứng dụng kiến thức vào thực tế cuộc sống hoặc chuẩn bị nội dung cho bài tiếp theo.


                            B. Yêu cầu chi tiết về ĐỀ KIỂM TRA (questions):
                            Thiết kế bộ đề kiểm tra cuối buổi gồm ĐÚNG 15 câu hỏi bám sát nội dung sách giáo khoa:
                            - 10 câu đầu: Trắc nghiệm (multiple_choice, có 4 đáp án A, B, C, D)
                            - 5 câu tiếp theo: Tự luận ngắn (essay)
                            Hãy điền đầy đủ đáp án chuẩn hoặc hướng dẫn chấm vào từng câu.

                            C. Đề xuất thời gian làm bài (duration_minutes) hợp lý từ 30 đến 60 phút.

                            D. Yêu cầu chi tiết về 15 THẺ FLASHCARD (flashcards):
                            Thiết kế ĐÚNG 15 thẻ Flashcard ôn tập (mỗi thẻ có mặt trước 'front' và mặt sau 'back') chứa đựng các công thức toán/lý/hóa quan trọng, thuật ngữ trọng tâm, từ mới tiếng Anh (hoặc ngoại ngữ khác), sự kiện lịch sử/địa lý quan trọng để học sinh kiểm tra nhanh.
                            """
                            
                            # Tải file PDF hoặc sử dụng text fallback để gửi cho Gemini
                            if use_multimodal and downloaded_temp_pdf:
                                temp_upload_path = ""
                                try:
                                    # Tạo bản sao file tạm thời với tên hoàn toàn ASCII để upload an toàn
                                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                                        with open(downloaded_temp_pdf, "rb") as original_file:
                                            temp_file.write(original_file.read())
                                        temp_upload_path = temp_file.name
                                    
                                    file_ref = client.files.upload(file=temp_upload_path)
                                    # Xóa tệp tạm sau khi upload thành công
                                    os.unlink(temp_upload_path)
                                    temp_upload_path = ""
                                    
                                    while file_ref.state.name == "PROCESSING":
                                        time.sleep(1)
                                        file_ref = client.files.get(name=file_ref.name)
                                        
                                    if file_ref.state.name == "FAILED":
                                        st.warning("Không thể xử lý định dạng PDF, hệ thống tự động chuyển sang chế độ văn bản.")
                                        prompt_with_fallback = prompt + f"\n\nNội dung text sách giáo khoa:\n{textbook_content[:60000]}"
                                        response = generate_content_with_retry(
                                            client,
                                            model='gemini-2.5-flash',
                                            contents=prompt_with_fallback,
                                            config=types.GenerateContentConfig(
                                                response_mime_type="application/json",
                                                response_schema=LessonPayload,
                                                temperature=0.3,
                                            )
                                        )
                                    else:
                                        # Gọi Gemini multimodal
                                        response = generate_content_with_retry(
                                            client,
                                            model='gemini-2.5-flash',
                                            contents=[file_ref, prompt],
                                            config=types.GenerateContentConfig(
                                                response_mime_type="application/json",
                                                response_schema=LessonPayload,
                                                temperature=0.3,
                                            )
                                        )
                                        client.files.delete(name=file_ref.name)
                                except Exception as upload_err:
                                    if temp_upload_path and os.path.exists(temp_upload_path):
                                        os.unlink(temp_upload_path)
                                    st.warning(f"Lỗi tải PDF lên Gemini ({upload_err}). Tự động chuyển sang chế độ văn bản.")
                                    prompt_with_fallback = prompt + f"\n\nNội dung text sách giáo khoa:\n{textbook_content[:60000]}"
                                    response = generate_content_with_retry(
                                        client,
                                        model='gemini-2.5-flash',
                                        contents=prompt_with_fallback,
                                        config=types.GenerateContentConfig(
                                            response_mime_type="application/json",
                                            response_schema=LessonPayload,
                                            temperature=0.3,
                                        )
                                    )
                            else:
                                # Fallback nếu không có file PDF
                                prompt_with_fallback = prompt + f"\n\nNội dung text sách giáo khoa:\n{textbook_content[:60000]}"
                                response = generate_content_with_retry(
                                    client,
                                    model='gemini-2.5-flash',
                                    contents=prompt_with_fallback,
                                    config=types.GenerateContentConfig(
                                        response_mime_type="application/json",
                                        response_schema=LessonPayload,
                                        temperature=0.3,
                                    )
                                )
                                
                            lesson_data: LessonPayload = response.parsed
                            
                            if lesson_data:
                                questions_list = []
                                for q in lesson_data.questions:
                                    questions_list.append({
                                        "question_number": q.question_number,
                                        "question_type": q.question_type,
                                        "prompt": q.prompt,
                                        "options": q.options,
                                        "correct_answer": q.correct_answer
                                    })
                                questions_json = json.dumps(questions_list, ensure_ascii=False)
                                
                                # Đóng gói Flashcards
                                flashcards_list = []
                                if lesson_data.flashcards:
                                    for fc in lesson_data.flashcards:
                                        flashcards_list.append({
                                            "front": fc.front,
                                            "back": fc.back
                                        })
                                flashcards_json = json.dumps(flashcards_list, ensure_ascii=False)
                                
                                success = save_lesson(
                                    selected_sub_lesson,
                                    lesson_number,
                                    lesson_data.title,
                                    lesson_data.lecture_content,
                                    questions_json,
                                    lesson_data.duration_minutes,
                                    flashcards_json
                                )
                                
                                if success:
                                    st.success(f"Đã soạn thảo và lưu Buổi {lesson_number}: {lesson_data.title} thành công!")
                                    current_lesson = {
                                        "title": lesson_data.title,
                                        "lecture_content": lesson_data.lecture_content,
                                        "questions": questions_json,
                                        "duration": lesson_data.duration_minutes,
                                        "flashcards": flashcards_json
                                    }
                        except Exception as e:
                            st.error(f"Lỗi khi AI soạn giáo án: {e}")
                        finally:
                            # Giải phóng file tạm đã tải về từ mây nếu có
                            if downloaded_temp_pdf and (pdf_file_path.startswith("http://") or pdf_file_path.startswith("https://")):
                                try:
                                    os.unlink(downloaded_temp_pdf)
                                except Exception:
                                    pass
                
                if current_lesson:
                    st.markdown(f"#### Buổi {lesson_number}: {current_lesson['title']}")
                    st.write(f"⏱️ **Thời gian làm bài thi:** {current_lesson['duration']} phút")
                    
                    with st.expander("📖 Xem nội dung bài giảng chi tiết (5 mục chuẩn)", expanded=True):
                        st.markdown(current_lesson['lecture_content'])
                    
                    with st.expander("🗂️ Xem 15 thẻ Flashcard ghi nhớ", expanded=False):
                        if current_lesson.get('flashcards'):
                            fc_list = json.loads(current_lesson['flashcards'])
                            st.write(f"Tổng số thẻ: {len(fc_list)} thẻ")
                            for idx, fc in enumerate(fc_list):
                                st.markdown(f"**Thẻ {idx+1}:**")
                                st.markdown(f"- **Mặt trước (Câu hỏi/Từ):** {fc['front']}")
                                st.markdown(f"- **Mặt sau (Giải thích/Nghĩa):** {fc['back']}")
                                st.write("---")
                        else:
                            st.warning("Bài học này chưa được cấu hình Flashcard.")
                            
                    with st.expander("✍️ Xem đề kiểm tra (15 câu)", expanded=False):
                        questions = json.loads(current_lesson['questions'])
                        st.info(f"Tổng số câu hỏi: {len(questions)} câu (10 trắc nghiệm, 5 tự luận)")
                        for q in questions:
                            st.markdown(f"**Câu {q['question_number']} ({'Trắc nghiệm' if q['question_type'] == 'multiple_choice' else 'Tự luận'}):** {q['prompt']}")
                            if q['question_type'] == 'multiple_choice' and q['options']:
                                for opt in q['options']:
                                    st.write(f"  {opt}")
                            st.info(f"💡 **Đáp án/Hướng dẫn:** {q['correct_answer']}")
                else:
                    st.info(f"Chưa có nội dung soạn thảo cho Buổi {lesson_number}. Hãy bấm nút bên trái để tạo.")

    # TAB 3: GIÁM SÁT KẾT QUẢ
    with tab3:
        st.subheader("Bảng điểm & Chi tiết bài kiểm tra của con")
        
        grades_list = get_grades_for_parent()
        if not grades_list:
            st.info("Chưa có học sinh nào nộp bài kiểm tra.")
        else:
            df_data = []
            for idx, g in enumerate(grades_list):
                df_data.append({
                    "STT": idx + 1,
                    "Học sinh": g['student_username'],
                    "Môn học": g['subject'],
                    "Buổi": f"Buổi {g['lesson_number']}",
                    "Tên bài học": g['lesson_title'],
                    "Điểm số": f"{g['score']:.1f} / 10.0",
                    "Thời gian nộp": g['submitted_at']
                })
            
            st.dataframe(df_data, use_container_width=True)
            
            st.write("---")
            st.write("### 🔍 Xem chi tiết bài kiểm tra")
            
            grade_options = [
                f"STT {g['STT']}. {g['Học sinh']} - {g['Môn học']} ({g['Buổi']}) - Điểm: {g['Điểm số']}"
                for g in df_data
            ]
            
            selected_grade_str = st.selectbox("Chọn bài thi muốn kiểm tra chi tiết:", grade_options)
            try:
                selected_stt = int(selected_grade_str.split(".")[0].split()[-1])
                selected_idx = selected_stt - 1
                selected_grade = grades_list[selected_idx]
            except Exception:
                selected_grade = None
            
            if selected_grade:
                col_score, col_feedback = st.columns([1, 3])
                
                with col_score:
                    score = selected_grade['score']
                    score_color = "score-good" if score >= 8 else "score-average" if score >= 5 else "score-poor"
                    st.markdown(
                        f"""
                        <div class='custom-card' style='text-align: center;'>
                            <h4>Điểm Số</h4>
                            <div class='score-display {score_color}'>{score:.1f}</div>
                            <p style='color:#94a3b8;'>Hệ điểm 10.0</p>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                    
                with col_feedback:
                    ai_fb = json.loads(selected_grade['ai_feedback'])
                    st.markdown(
                        f"""
                        <div class='custom-card'>
                            <h4>Nhận xét tổng quan từ AI</h4>
                            <p style='font-style: italic; line-height: 1.6;'>"{ai_fb.get('overall_feedback', '')}"</p>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                
                st.write("#### 📑 Báo cáo chi tiết từng câu hỏi (15 câu):")
                
                detailed_fb = ai_fb.get('detailed_feedback', [])
                original_questions = json.loads(selected_grade['original_questions'])
                student_answers = json.loads(selected_grade['answers'])
                
                for idx, q_fb in enumerate(detailed_fb):
                    q_num = q_fb.get('question_number', idx + 1)
                    orig_q = next((q for q in original_questions if q['question_number'] == q_num), None)
                    
                    status_icon = "🟢" if q_fb.get('is_correct', False) else "🔴"
                    card_border = "border-left: 5px solid #22c55e;" if q_fb.get('is_correct', False) else "border-left: 5px solid #ef4444;"
                    
                    st.markdown(
                        f"""
                        <div class='custom-card' style='{card_border} padding: 15px 20px; margin-bottom: 10px;'>
                            <div style='display: flex; justify-content: space-between;'>
                                <strong>Câu hỏi {q_num} ({orig_q['question_type'] if orig_q else 'unknown'})</strong>
                                <span>Điểm đạt: <strong>{q_fb.get('score_awarded', 0.0):.2f} / 1.00</strong> {status_icon}</span>
                            </div>
                            <div style='margin-top: 10px; color:#e2e8f0;'>
                                {orig_q['prompt'] if orig_q else ''}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                    
                    if orig_q and orig_q['question_type'] == 'multiple_choice' and orig_q['options']:
                        for opt in orig_q['options']:
                            st.write(f"  {opt}")
                    
                    col_ans, col_exp = st.columns(2)
                    with col_ans:
                        st.markdown(f"**✍️ Bài làm của con:** `{q_fb.get('student_answer', '')}`")
                        if orig_q:
                            st.markdown(f"💡 **Đáp án chuẩn:** `{orig_q['correct_answer']}`")
                    with col_exp:
                        st.markdown(f"🤖 **AI Giải thích & Nhận xét:** {q_fb.get('correct_explanation', '')}")
                    st.write("---")

    # TAB 4: QUẢN LÝ DỮ LIỆU
    with tab4:
        st.subheader("⚙️ Quản lý Cơ sở dữ liệu học tập")
        st.write("Ba mẹ có thể theo dõi danh sách các môn học, số bài giảng đã soạn và xóa các dữ liệu bị nhầm lẫn tại đây.")
        
        # 1. Quản lý Lộ trình (Syllabus)
        st.write("### 🗺️ Danh sách Lộ trình học tập")
        try:
            with get_db() as conn:
                syllabus_rows = conn.execute("SELECT id, subject, total_lessons FROM Syllabus ORDER BY subject ASC").fetchall()
                syllabus_list = [dict(r) for r in syllabus_rows]
        except Exception as e:
            st.error(f"Lỗi tải lộ trình: {e}")
            syllabus_list = []
            
        if not syllabus_list:
            st.info("Chưa có môn học nào được tạo lộ trình.")
        else:
            for s in syllabus_list:
                col_sub, col_tot, col_del = st.columns([3, 1, 1])
                with col_sub:
                    st.write(f"📚 **{s['subject']}**")
                with col_tot:
                    st.write(f"⏱️ Tổng số: {s['total_lessons']} buổi")
                with col_del:
                    if st.button("Xóa môn ❌", key=f"del_sub_{s['id']}"):
                        try:
                            with get_db() as conn:
                                # Xóa Syllabus
                                conn.execute("DELETE FROM Syllabus WHERE subject = ?", (s['subject'],))
                                # Xóa Lessons liên quan
                                conn.execute("DELETE FROM Lessons WHERE subject = ?", (s['subject'],))
                                conn.commit()
                            st.success(f"Đã xóa môn học '{s['subject']}' và toàn bộ bài giảng liên quan!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Lỗi khi xóa môn học: {e}")
                            
        st.write("---")
        
        # 2. Quản lý Bài giảng (Lessons)
        st.write("### 📝 Danh sách bài giảng đã soạn thảo")
        try:
            with get_db() as conn:
                lessons_rows = conn.execute("SELECT id, subject, lesson_number, title FROM Lessons ORDER BY subject ASC, lesson_number ASC").fetchall()
                lessons_list = [dict(r) for r in lessons_rows]
        except Exception as e:
            st.error(f"Lỗi tải bài giảng: {e}")
            lessons_list = []
            
        if not lessons_list:
            st.info("Chưa có bài giảng nào được soạn thảo.")
        else:
            for l in lessons_list:
                col_sub_l, col_num, col_title_l, col_del_l = st.columns([2, 1, 3, 1])
                with col_sub_l:
                    st.write(f"📖 {l['subject']}")
                with col_num:
                    st.write(f"Buổi {l['lesson_number']}")
                with col_title_l:
                    st.write(l['title'])
                with col_del_l:
                    if st.button("Xóa bài 🗑️", key=f"del_les_{l['id']}"):
                        try:
                            with get_db() as conn:
                                conn.execute("DELETE FROM Lessons WHERE id = ?", (l['id'],))
                                conn.commit()
                            st.success(f"Đã xóa bài giảng Buổi {l['lesson_number']} của môn '{l['subject']}'!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Lỗi khi xóa bài giảng: {e}")
                            
        st.write("---")
        st.write("### ⚠️ Khu Vực Nguy Hiểm")
        st.warning("Khởi tạo lại cơ sở dữ liệu sẽ xóa sạch toàn bộ lộ trình, bài giảng, điểm số và tin nhắn. Hành động này không thể hoàn tác!")
        if st.button("Xóa Sạch Dữ Liệu & Làm Lại Từ Đầu 🔄", key="reset_db_button", use_container_width=True):
            if reset_active_database():
                st.success("Đã xóa sạch cơ sở dữ liệu thành công! Vui lòng đăng nhập lại.")
                st.session_state.clear()
                st.rerun()


# --- GIAO DIỆN HỌC SINH ---

def start_new_exam(lesson):
    st.session_state['exam_in_progress'] = True
    st.session_state['exam_lesson'] = lesson
    st.session_state['exam_start_time'] = datetime.datetime.now()
    st.rerun()

def show_student_interface(client):
    st.markdown("<h1>Không Gian <span class='student-gradient-text'>Học Tập</span> <span class='badge badge-student'>Student Workspace</span></h1>", unsafe_allow_html=True)
    
    if st.session_state.get('exam_in_progress', False):
        show_exam_taking_room(client)
        return
        
    if st.session_state.get('just_submitted', False):
        show_exam_result_room()
        return

    subjects = get_subjects()
    if not subjects:
        st.info("Ba mẹ chưa tạo lộ trình môn học nào. Hãy nhắc ba mẹ vào tài khoản phụ huynh để tạo bài học nhé!")
        return
        
    col_sel, col_main = st.columns([1, 3])
    
    with col_sel:
        st.write("### 📚 Chọn bài học")
        selected_subject = st.selectbox("Chọn môn học:", subjects, key="student_subject_select")
        
        # Đồng bộ thủ công để reset chỉ số bài học khi học sinh đổi môn học
        if 'prev_student_subject' not in st.session_state:
            st.session_state['prev_student_subject'] = selected_subject
        if selected_subject != st.session_state['prev_student_subject']:
            st.session_state['prev_student_subject'] = selected_subject
            if "student_lesson_selectbox" in st.session_state:
                del st.session_state["student_lesson_selectbox"]
        
        lessons = get_lessons_for_subject(selected_subject)
        
        if not lessons:
            st.warning("Môn học này chưa được ba mẹ soạn giáo án nào.")
            selected_lesson_num = None
        else:
            lesson_options = [f"Buổi {l['lesson_number']}: {l['title']}" for l in lessons]
            selected_lesson_str = st.selectbox("Chọn buổi học:", lesson_options, key="student_lesson_selectbox")
            selected_lesson_num = lessons[lesson_options.index(selected_lesson_str)]['lesson_number']
            
        # Thêm nút mở Sách giáo khoa PDF từ bộ lưu trữ đám mây cho học sinh
        syllabus_data = get_syllabus_with_textbook(selected_subject)
        if syllabus_data and syllabus_data.get('pdf_file_path'):
            pdf_path = syllabus_data['pdf_file_path']
            
            target_url = ""
            if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
                target_url = pdf_path
            else:
                # Nếu là đường dẫn cục bộ cũ (ví dụ: data/textbooks/abc.pdf), chuyển thành link public trên Supabase Storage
                import os
                basename = os.path.basename(pdf_path)
                if basename:
                    supabase_url = "https://ubaupchqavybpjpxjmle.supabase.co"
                    if st.secrets and "SUPABASE_URL" in st.secrets:
                        supabase_url = st.secrets["SUPABASE_URL"].strip().rstrip('/')
                    target_url = f"{supabase_url}/storage/v1/object/public/textbooks/{basename}"
            
            if target_url:
                st.markdown("<div style='margin-top: 20px;'></div>", unsafe_allow_html=True)
                st.link_button("📖 Xem Sách Giáo Khoa (PDF)", target_url, use_container_width=True)
            
    with col_main:
        if selected_lesson_num:
            lesson = get_lesson_detail(selected_subject, selected_lesson_num)
            
            st.markdown(f"## Buổi {selected_lesson_num}: {lesson['title']}")
            
            grade_record = get_grade_for_student(st.session_state['username'], lesson['id'])
            
            if grade_record:
                score = grade_record['score']
                score_color = "score-good" if score >= 8 else "score-average" if score >= 5 else "score-poor"
                st.markdown(
                    f"""
                    <div class='custom-card' style='display:flex; align-items:center; justify-content:space-between; border-left:5px solid #22c55e;'>
                        <div>
                            <h4 style='margin:0;'>🎉 Bạn đã hoàn thành bài kiểm tra này!</h4>
                            <p style='margin:5px 0 0 0; color:#94a3b8;'>Nộp vào lúc: {grade_record['submitted_at']}</p>
                        </div>
                        <div style='text-align:right;'>
                            <span style='font-size:0.9rem; color:#94a3b8;'>Điểm số đạt được:</span>
                            <div class='{score_color}' style='font-size:2rem; font-weight:800; margin:0;'>{score:.1f} / 10.0</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
                
            tab_lecture, tab_flashcards, tab_practice = st.tabs([
                "📖 Bài giảng lý thuyết (5 mục)", 
                "🗂️ Thẻ Ghi Nhớ (15 Flashcards)", 
                "✍️ Bài kiểm tra"
            ])
            
            with tab_lecture:
                st.markdown(lesson['lecture_content'])
                
            with tab_flashcards:
                st.write("### 🗂️ Thẻ Ghi Nhớ Ôn Tập (15 Flashcards)")
                st.write("Rê chuột (hoặc chạm tay trên điện thoại) lên thẻ để **lật ngược 3D** xem đáp án/giải thích!")
                
                flashcards_data = lesson.get('flashcards')
                if not flashcards_data:
                    st.info("Bài học này chưa được cấu hình Flashcard. Ba mẹ hãy dùng tính năng AI Soạn bài giảng mới để tự động cập nhật Flashcards.")
                else:
                    try:
                        fc_list = json.loads(flashcards_data)
                    except Exception:
                        fc_list = []
                        
                    if len(fc_list) == 0:
                        st.info("Không có Flashcard nào được tìm thấy cho bài học này.")
                    else:
                        # Lưu trữ vị trí thẻ đang chọn trong session state
                        fc_key = f"fc_index_{lesson['id']}"
                        if fc_key not in st.session_state:
                            st.session_state[fc_key] = 0
                            
                        current_fc_idx = st.session_state[fc_key]
                        # Giới hạn chỉ số hợp lệ
                        current_fc_idx = max(0, min(current_fc_idx, len(fc_list) - 1))
                        st.session_state[fc_key] = current_fc_idx
                        
                        fc = fc_list[current_fc_idx]
                        
                        # Vẽ thanh tiến trình
                        progress_val = (current_fc_idx + 1) / len(fc_list)
                        st.progress(progress_val)
                        st.write(f"📝 **Thẻ {current_fc_idx + 1} trên {len(fc_list)}**")
                        
                        # Hiển thị thẻ lật 3D bằng HTML/CSS
                        front_text = fc.get('front', '')
                        back_text = fc.get('back', '')
                        
                        st.markdown(
                            f"""
                            <div class="flashcard-container">
                                <div class="flip-card">
                                    <div class="flip-card-inner">
                                        <div class="flip-card-front">
                                            <div class="flashcard-title">Mặt Trước (Câu hỏi / Từ mới)</div>
                                            <div class="flashcard-content">{front_text}</div>
                                            <div class="flashcard-hint">💡 Rê chuột/chạm tay để lật xem mặt sau</div>
                                        </div>
                                        <div class="flip-card-back">
                                            <div class="flashcard-title" style="color: #64748b;">Mặt Sau (Lời giải / Giải thích)</div>
                                            <div class="flashcard-content">{back_text}</div>
                                            <div class="flashcard-hint" style="color: #94a3b8;">💡 Bôi đen chữ để nghe phát âm</div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                        
                        # Nút điều hướng Carousel
                        col_fc_prev, col_fc_space, col_fc_next = st.columns([1, 2, 1])
                        with col_fc_prev:
                            if st.button("⬅️ Thẻ Trước", use_container_width=True, disabled=(current_fc_idx == 0)):
                                st.session_state[fc_key] = current_fc_idx - 1
                                st.rerun()
                        with col_fc_next:
                            if st.button("Thẻ Kế ➡️", use_container_width=True, disabled=(current_fc_idx == len(fc_list) - 1)):
                                st.session_state[fc_key] = current_fc_idx + 1
                                st.rerun()
            
            with tab_practice:
                if grade_record:
                    st.info("Bạn đã làm bài kiểm tra này rồi. Bạn muốn xem lại phản hồi của AI hay làm lại bài?")
                    col_btn1, col_btn2 = st.columns(2)
                    with col_btn1:
                        if st.button("Xem lại nhận xét chi tiết 🤖", use_container_width=True):
                            st.session_state['result_grade'] = grade_record
                            st.session_state['result_lesson'] = lesson
                            st.session_state['just_submitted'] = True
                            st.rerun()
                    with col_btn2:
                        if st.button("Làm lại bài kiểm tra 🔄", use_container_width=True):
                            start_new_exam(lesson)
                else:
                    st.write(f"📝 Đề thi bao gồm **15 câu hỏi** (10 câu trắc nghiệm kết hợp 5 câu tự luận).")
                    st.write(f"⏱️ **Thời gian làm bài:** `{lesson['duration']}` phút.")
                    st.warning("⚠️ Sau khi bấm nút bắt đầu, bộ đếm thời gian sẽ chạy. Hãy tập trung làm bài nhé!")
                    
                    if st.button("🚀 Bắt đầu làm bài", use_container_width=True):
                        start_new_exam(lesson)


# --- PHÒNG THI BẤM GIỜ ---

def show_exam_taking_room(client):
    lesson = st.session_state['exam_lesson']
    start_time = st.session_state['exam_start_time']
    duration = lesson['duration']
    
    end_time = start_time + datetime.timedelta(minutes=duration)
    now = datetime.datetime.now()
    remaining_seconds = int((end_time - now).total_seconds())
    
    if remaining_seconds <= 0:
        st.warning("⏳ Đã hết thời gian làm bài! Đang tự động nộp bài...")
        submit_exam(client, lesson, is_auto_submit=True)
        return

    st.markdown(f"## 📝 Bài Kiểm Tra: Buổi {lesson['lesson_number']} - {lesson['title']}")
    
    with st.sidebar:
        st.components.v1.html(
            f"""
            <div style="background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; text-align: center; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); font-family: sans-serif;">
                <div style="font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em; color: #64748b; margin-bottom: 8px; font-weight: 600;">Thời gian còn lại</div>
                <div id="visual-timer" style="font-size: 2.25rem; font-weight: 800; color: #0284c7; font-family: monospace; font-variant-numeric: tabular-nums;">--:--</div>
            </div>
            <script>
                (function() {{
                    let secondsLeft = {remaining_seconds};
                    
                    function updateDisplay() {{
                        if (secondsLeft <= 0) {{
                            clearInterval(timerInterval);
                            const clock = document.getElementById("visual-timer");
                            if (clock) {{
                                clock.innerHTML = "HẾT GIỜ!";
                                clock.style.color = "#ef4444";
                            }}
                            
                            try {{
                                const parentDoc = window.parent.document;
                                const buttons = parentDoc.querySelectorAll("button");
                                for (let btn of buttons) {{
                                    if (btn.innerText.indexOf("Nộp Bài") !== -1 || btn.innerText.indexOf("Nộp bài") !== -1) {{
                                        btn.click();
                                        break;
                                    }}
                                }}
                            }} catch(e) {{
                                console.log("Iframe sandbox block access:", e);
                            }}
                        }} else {{
                            const minutes = Math.floor(secondsLeft / 60);
                            const seconds = secondsLeft % 60;
                            const displayMin = minutes < 10 ? "0" + minutes : minutes;
                            const displaySec = seconds < 10 ? "0" + seconds : seconds;
                            const clock = document.getElementById("visual-timer");
                            if (clock) {{
                                clock.innerHTML = displayMin + ":" + displaySec;
                                if (minutes < 2) {{
                                    clock.style.color = "#f59e0b";
                                    clock.style.animation = "blink 1s infinite";
                                }}
                                if (minutes < 1 && seconds < 30) {{
                                    clock.style.color = "#ef4444";
                                }}
                            }}
                        }}
                    }}
                    
                    // Chạy hiển thị ngay lập tức
                    updateDisplay();
                    
                    const timerInterval = setInterval(function() {{
                        secondsLeft--;
                        updateDisplay();
                    }}, 1000);
                }})();
            </script>
            <style>
                @keyframes blink {{
                    50% {{ opacity: 0.6; }}
                }}
            </style>
            """,
            height=150
        )
    
    questions = json.loads(lesson['questions'])
    
    with st.form(key="visual_taking_form"):
        st.write("👉 Hãy đọc kỹ câu hỏi và điền câu trả lời hoàn chỉnh của bạn.")
        
        answers_dict = {}
        for q in questions:
            q_num = q['question_number']
            st.markdown(f"#### Câu {q_num} ({'Trắc nghiệm' if q['question_type'] == 'multiple_choice' else 'Tự luận'}): {q['prompt']}")
            
            if q['question_type'] == 'multiple_choice' and q['options']:
                ans = st.radio(
                    f"Chọn đáp án đúng cho câu {q_num}:",
                    options=q['options'],
                    index=None,
                    key=f"q_{q_num}",
                    label_visibility="collapsed"
                )
                answers_dict[str(q_num)] = ans.split(".")[0].strip() if ans else ""
            else:
                ans = st.text_area(
                    f"Nhập câu trả lời tự luận cho câu {q_num}:",
                    placeholder="Viết lời giải chi tiết của bạn tại đây...",
                    key=f"q_{q_num}",
                    label_visibility="collapsed"
                )
                answers_dict[str(q_num)] = ans.strip() if ans else ""
            st.write("---")
            
        btn_submit = st.form_submit_button("Nộp Bài Thi 💾", use_container_width=True)
        
        if btn_submit:
            st.session_state['exam_answers'] = answers_dict
            submit_exam(client, lesson, is_auto_submit=False)


def submit_exam(client, lesson, is_auto_submit=False):
    answers = st.session_state.get('exam_answers', {})
    
    if not answers:
        questions = json.loads(lesson['questions'])
        answers = {str(q['question_number']): "" for q in questions}
        
    questions_list = json.loads(lesson['questions'])
    
    questions_formatted = json.dumps(questions_list, ensure_ascii=False, indent=2)
    answers_formatted = json.dumps(answers, ensure_ascii=False, indent=2)
    
    prompt = f"""
    Bạn là một giáo viên/giám khảo thông thái và nghiêm khắc. Hãy chấm điểm bài kiểm tra gồm 15 câu hỏi dưới đây của học sinh (gồm 10 câu trắc nghiệm và 5 câu tự luận).
    
    Đề kiểm tra gốc:
    {questions_formatted}
    
    Bài làm của học sinh:
    {answers_formatted}
    
    Yêu cầu chấm điểm:
    1. Chấm điểm chi tiết từng câu trong 15 câu hỏi (từ 1 đến 15). Mỗi câu tối đa 1.0 điểm chuẩn (hoặc điểm quy đổi).
       - Với câu trắc nghiệm (10 câu): Học sinh chọn chữ cái tương ứng với đáp án đúng (A, B, C, D). Đúng được 1.0 điểm, sai được 0.0 điểm.
       - Với câu tự luận (5 câu): Đọc câu trả lời, so sánh với đáp án chuẩn. Cho điểm từ 0.0 đến 1.0 tùy mức độ.
    2. Quy đổi tổng điểm bài kiểm tra (total_score) về thang điểm 10. Hãy tính toán chính xác, công bằng và nghiêm khắc.
    3. Trả về nhận xét chung (overall_feedback) phân tích ưu nhược điểm bài thi và chi tiết chấm điểm từng câu (detailed_feedback).
    """
    
    with st.spinner("AI đang chấm điểm chi tiết bài làm của bạn... Vui lòng đợi trong giây lát."):
        try:
            response = generate_content_with_retry(
                client,
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GradePayload,
                    temperature=0.1,
                )
            )
            grading_result: GradePayload = response.parsed
            
            if grading_result:
                ai_feedback_dict = {
                    "total_score": grading_result.total_score,
                    "overall_feedback": grading_result.overall_feedback,
                    "detailed_feedback": [
                        {
                            "question_number": f.question_number,
                            "student_answer": f.student_answer,
                            "is_correct": f.is_correct,
                            "score_awarded": f.score_awarded,
                            "correct_explanation": f.correct_explanation
                        }
                        for f in grading_result.detailed_feedback
                    ]
                }
                ai_feedback_json = json.dumps(ai_feedback_dict, ensure_ascii=False)
                answers_json = json.dumps(answers, ensure_ascii=False)
                
                save_grade(
                    st.session_state['username'],
                    lesson['id'],
                    answers_json,
                    grading_result.total_score,
                    ai_feedback_json
                )
                
                st.session_state['result_grade'] = {
                    "score": grading_result.total_score,
                    "answers": answers_json,
                    "ai_feedback": ai_feedback_json,
                    "submitted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                st.session_state['result_lesson'] = lesson
                st.session_state['just_submitted'] = True
                
                st.session_state['exam_in_progress'] = False
                st.session_state['exam_answers'] = {}
                st.session_state['exam_lesson'] = None
                
                st.rerun()
        except Exception as e:
            st.error(f"Lỗi khi AI chấm điểm bài thi: {e}")


# --- PHÒNG HIỂN THỊ KẾT QUẢ CHẤM ĐIỂM ---

def show_exam_result_room():
    grade = st.session_state['result_grade']
    lesson = st.session_state['result_lesson']
    
    st.balloons()
    st.markdown("## 🎉 Kết Quả Bài Làm Kiểm Tra")
    st.success("Bạn đã hoàn thành bài kiểm tra! Dưới đây là nhận xét và điểm số từ Giám khảo AI.")
    
    col_score, col_overall = st.columns([1, 2])
    
    ai_fb = json.loads(grade['ai_feedback'])
    score = grade['score']
    
    with col_score:
        score_color = "score-good" if score >= 8 else "score-average" if score >= 5 else "score-poor"
        st.markdown(
            f"""
            <div class='custom-card' style='text-align: center;'>
                <h4>Tổng Điểm</h4>
                <div class='score-display {score_color}'>{score:.1f} / 10.0</div>
                <p style='color: #94a3b8;'>Đã lưu kết quả thành công vào hệ thống!</p>
            </div>
            """,
            unsafe_allow_html=True
        )
        
    with col_overall:
        st.markdown(
            f"""
            <div class='custom-card'>
                <h4>Nhận xét tổng quan của Thầy Cô AI:</h4>
                <p style='line-height:1.6; font-style:italic;'>"{ai_fb.get('overall_feedback', '')}"</p>
            </div>
            """,
            unsafe_allow_html=True
        )
        
    st.write("---")
    st.write("### 📝 Sửa bài kiểm tra chi tiết từng câu (15 câu):")
    
    detailed_fb = ai_fb.get('detailed_feedback', [])
    original_questions = json.loads(lesson['questions'])
    
    for idx, q_fb in enumerate(detailed_fb):
        q_num = q_fb.get('question_number', idx + 1)
        orig_q = next((q for q in original_questions if q['question_number'] == q_num), None)
        
        status_icon = "🟢 Đúng" if q_fb.get('is_correct', False) else "🔴 Sai"
        card_border = "border-left: 5px solid #22c55e;" if q_fb.get('is_correct', False) else "border-left: 5px solid #ef4444;"
        
        st.markdown(
            f"""
            <div class='custom-card' style='{card_border} padding: 15px 20px; margin-bottom: 10px;'>
                <div style='display: flex; justify-content: space-between;'>
                    <strong>Câu {q_num}</strong>
                    <span>Trạng thái: {status_icon} | Điểm số: <strong>{q_fb.get('score_awarded', 0.0):.2f} / 1.00</strong></span>
                </div>
                <div style='margin-top: 10px;'>
                    {orig_q['prompt'] if orig_q else ''}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
        
        if orig_q and orig_q['question_type'] == 'multiple_choice' and orig_q['options']:
            for opt in orig_q['options']:
                st.write(f"  {opt}")
                
        col_ans, col_exp = st.columns(2)
        with col_ans:
            st.markdown(f"👉 **Câu trả lời của bạn:** `{q_fb.get('student_answer', '')}`")
            if orig_q:
                st.markdown(f"💡 **Đáp án đúng:** `{orig_q['correct_answer']}`")
        with col_exp:
            st.markdown(f"🤖 **Hướng dẫn sửa từ AI:** {q_fb.get('correct_explanation', '')}")
        st.write("---")
        
    if st.button("Quay lại Trang Chủ Bài Học", use_container_width=True):
        st.session_state['just_submitted'] = False
        st.session_state['result_grade'] = None
        st.session_state['result_lesson'] = None
        st.rerun()


# --- HÀM MAIN ĐIỀU PHỐI CHÍNH ---

def main():
    inject_custom_css()
    inject_selection_speak_js()
    
    if 'logged_in' not in st.session_state:
        st.session_state['logged_in'] = False
        
    if not st.session_state['logged_in']:
        st.markdown(
            """
            <div class='custom-card' style='max-width: 450px; margin: 80px auto; text-align: center;'>
                <h2>🔑 Đăng Nhập Hệ Thống</h2>
                <p style='color: #475569; font-size: 0.9rem;'>Ứng Dụng Học Tập Gia Đình Riêng Tư</p>
            </div>
            """,
            unsafe_allow_html=True
        )
        
        with st.form("login_form"):
            username = st.text_input("Tên đăng nhập:", placeholder="Nhập phuhuynh hoặc hocsinh")
            password = st.text_input("Mật khẩu:", type="password", placeholder="Nhập mật khẩu")
            btn_submit = st.form_submit_button("Đăng Nhập 🔓", use_container_width=True)
            
            if btn_submit:
                user = verify_user(username, password)
                if user:
                    st.session_state['logged_in'] = True
                    st.session_state['username'] = user['username']
                    st.session_state['role'] = user['role']
                    st.success("Đăng nhập thành công!")
                    st.rerun()
                else:
                    st.error("❌ Sai tên đăng nhập hoặc mật khẩu!")
        return

    client = get_gemini_client()
    
    st.sidebar.markdown(f"### 👋 Xin chào, **{st.session_state['username']}**!")
    role_name = "Phụ huynh" if st.session_state['role'] == 'parent' else "Học sinh"
    badge_style = "badge-parent" if st.session_state['role'] == 'parent' else "badge-student"
    st.sidebar.markdown(f"<span class='badge {badge_style}'>{role_name}</span>", unsafe_allow_html=True)
    st.sidebar.write("---")
    
    # show_sidebar_chat()
    # st.sidebar.write("---")
    
    if st.sidebar.button("Đăng Xuất 🚪", use_container_width=True):
        st.session_state['logged_in'] = False
        st.session_state['username'] = None
        st.session_state['role'] = None
        st.session_state['exam_in_progress'] = False
        st.session_state['just_submitted'] = False
        st.rerun()

    if not client:
        st.info("Vui lòng thiết lập API Key của Gemini ở cột bên trái để sử dụng đầy đủ các tính năng AI.")
        return

    if st.session_state['role'] == 'parent':
        show_parent_interface(client)
    else:
        show_student_interface(client)


if __name__ == '__main__':
    main()
