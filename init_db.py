import sqlite3
import os

DB_FILE = 'he_thong_hoc_tap.db'

def init_database():
    print(f"Khởi tạo cơ sở dữ liệu: {DB_FILE}")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Bật foreign keys
    cursor.execute("PRAGMA foreign_keys = ON;")

    # 1. Bảng Users
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS Users (
        username TEXT PRIMARY KEY,
        password TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('parent', 'student'))
    );
    """)

    # 2. Bảng Syllabus
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS Syllabus (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT UNIQUE NOT NULL,
        content TEXT NOT NULL,
        textbook_content TEXT, -- cột để lưu trữ văn bản thô trích xuất từ PDF
        pdf_file_path TEXT, -- cột để lưu trữ đường dẫn file PDF cục bộ
        total_lessons INTEGER DEFAULT 30, -- tổng số buổi học được chốt
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Hỗ trợ di cư (Migration) nếu các cột mới chưa tồn tại
    try:
        cursor.execute("ALTER TABLE Syllabus ADD COLUMN textbook_content TEXT;")
        print("Đã kiểm tra/thêm cột 'textbook_content' vào bảng Syllabus.")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE Syllabus ADD COLUMN pdf_file_path TEXT;")
        print("Đã kiểm tra/thêm cột 'pdf_file_path' vào bảng Syllabus.")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE Syllabus ADD COLUMN total_lessons INTEGER DEFAULT 30;")
        print("Đã kiểm tra/thêm cột 'total_lessons' vào bảng Syllabus.")
    except sqlite3.OperationalError:
        pass

    # 3. Bảng Lessons
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS Lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        lesson_number INTEGER NOT NULL,
        title TEXT NOT NULL,
        lecture_content TEXT NOT NULL,
        questions TEXT NOT NULL, -- JSON string của danh sách câu hỏi
        duration INTEGER NOT NULL, -- thời gian làm bài (phút)
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(subject, lesson_number)
    );
    """)

    # 4. Bảng Grades
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS Grades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_username TEXT NOT NULL,
        lesson_id INTEGER NOT NULL,
        answers TEXT NOT NULL, -- JSON string câu trả lời của học sinh
        score REAL NOT NULL,
        ai_feedback TEXT NOT NULL, -- JSON string phản hồi chi tiết của AI
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_username) REFERENCES Users(username),
        FOREIGN KEY (lesson_id) REFERENCES Lessons(id)
    );
    """)

    # Gieo dữ liệu tài khoản cố định (idempotent)
    users_data = [
        ('phuhuynh', '123456', 'parent'),
        ('hocsinh', '123456', 'student')
    ]
    cursor.executemany("""
    INSERT OR REPLACE INTO Users (username, password, role)
    VALUES (?, ?, ?);
    """, users_data)

    conn.commit()
    conn.close()
    print("Khởi tạo cơ sở dữ liệu thành công và đã đồng bộ tài khoản!")

if __name__ == '__main__':
    init_database()
