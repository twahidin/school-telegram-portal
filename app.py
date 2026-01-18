from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
import os
import io
from datetime import datetime, timedelta
from models import db, Student, Teacher, Message, Class, Assignment, Submission
from utils.auth import hash_password, verify_password, generate_assignment_id, generate_submission_id, encrypt_api_key, decrypt_api_key
from utils.ai_marking import get_teacher_ai_service, mark_submission
from utils.google_drive import get_teacher_drive_manager, upload_assignment_file
from utils.pdf_generator import generate_feedback_pdf
from utils.notifications import notify_submission_ready
import logging

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'change-this-in-production-please')
app.config['MONGODB_URI'] = os.getenv('MONGODB_URI') or os.getenv('MONGO_URL')
app.config['MONGODB_DB'] = os.getenv('MONGODB_DB', 'school_portal')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# Initialize database
db.init_app(app)

# Initialize rate limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# Admin password from environment
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

# ============================================================================
# DECORATORS
# ============================================================================

def login_required(f):
    """Require student login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'student_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def teacher_required(f):
    """Require teacher login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'teacher_id' not in session:
            return redirect(url_for('teacher_login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Require admin login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'error': 'Unauthorized'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    """Student login"""
    if request.method == 'POST':
        student_id = request.form.get('student_id', '').strip().upper()
        password = request.form.get('password', '')
        
        if not student_id or not password:
            return render_template('login.html', error='Please enter both ID and password')
        
        student = Student.find_one({'student_id': student_id})
        
        if not student:
            return render_template('login.html', error='Invalid student ID')
        
        if not verify_password(password, student.get('password_hash', '')):
            return render_template('login.html', error='Invalid password')
        
        session['student_id'] = student_id
        session['student_name'] = student.get('name', 'Student')
        session['student_class'] = student.get('class', '')
        session.permanent = True
        
        return redirect(url_for('dashboard'))
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """Student logout"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/teacher/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def teacher_login():
    """Teacher login"""
    if request.method == 'POST':
        teacher_id = request.form.get('teacher_id', '').strip().upper()
        password = request.form.get('password', '')
        
        if not teacher_id or not password:
            return render_template('teacher_login.html', error='Please enter both ID and password')
        
        teacher = Teacher.find_one({'teacher_id': teacher_id})
        
        if not teacher:
            return render_template('teacher_login.html', error='Invalid teacher ID')
        
        if not verify_password(password, teacher.get('password_hash', '')):
            return render_template('teacher_login.html', error='Invalid password')
        
        session['teacher_id'] = teacher_id
        session['teacher_name'] = teacher.get('name', 'Teacher')
        session.permanent = True
        
        return redirect(url_for('teacher_dashboard'))
    
    return render_template('teacher_login.html')

@app.route('/teacher/logout')
def teacher_logout():
    """Teacher logout"""
    session.clear()
    return redirect(url_for('teacher_login'))

@app.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def admin_login():
    """Admin login"""
    if request.method == 'POST':
        password = request.form.get('password', '')
        
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            session.permanent = True
            return redirect(url_for('admin_dashboard'))
        
        return render_template('admin_login.html', error='Invalid password')
    
    return render_template('admin_login.html')

# ============================================================================
# STUDENT DASHBOARD & CHAT ROUTES
# ============================================================================

@app.route('/')
def index():
    """Redirect to appropriate page"""
    if 'student_id' in session:
        return redirect(url_for('dashboard'))
    if 'teacher_id' in session:
        return redirect(url_for('teacher_dashboard'))
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Student dashboard - show assigned teachers"""
    student = Student.find_one({'student_id': session['student_id']})
    
    if not student:
        session.clear()
        return redirect(url_for('login'))
    
    # Get assigned teachers
    teacher_ids = student.get('teachers', [])
    teachers = list(Teacher.find({'teacher_id': {'$in': teacher_ids}}))
    
    # Get unread message counts for each teacher
    for t in teachers:
        unread = Message.count({
            'student_id': session['student_id'],
            'teacher_id': t['teacher_id'],
            'from_student': False,
            'read': False
        })
        t['unread_count'] = unread
    
    return render_template('dashboard.html', 
                         student=student, 
                         teachers=teachers)

@app.route('/chat/<teacher_id>')
@login_required
def chat(teacher_id):
    """Chat interface with a teacher"""
    student = Student.find_one({'student_id': session['student_id']})
    teacher = Teacher.find_one({'teacher_id': teacher_id})
    
    if not teacher:
        return redirect(url_for('dashboard'))
    
    # Check if student is assigned to this teacher
    if teacher_id not in student.get('teachers', []):
        return redirect(url_for('dashboard'))
    
    # Get messages
    messages = list(Message.find({
        'student_id': session['student_id'],
        'teacher_id': teacher_id
    }).sort('timestamp', 1))
    
    # Mark teacher's messages as read
    Message.update_many(
        {
            'student_id': session['student_id'],
            'teacher_id': teacher_id,
            'from_student': False,
            'read': False
        },
        {'$set': {'read': True}}
    )
    
    return render_template('chat.html',
                         student=student,
                         teacher=teacher,
                         messages=messages)

@app.route('/api/send_message', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def send_message():
    """Send a message to a teacher"""
    try:
        data = request.get_json()
        teacher_id = data.get('teacher_id')
        message_text = data.get('message', '').strip()
        
        if not teacher_id or not message_text:
            return jsonify({'error': 'Missing teacher_id or message'}), 400
        
        student = Student.find_one({'student_id': session['student_id']})
        teacher = Teacher.find_one({'teacher_id': teacher_id})
        
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Check assignment
        if teacher_id not in student.get('teachers', []):
            return jsonify({'error': 'Not assigned to this teacher'}), 403
        
        # Save message
        message_doc = {
            'student_id': session['student_id'],
            'teacher_id': teacher_id,
            'message': message_text,
            'from_student': True,
            'timestamp': datetime.utcnow(),
            'read': False
        }
        Message.insert_one(message_doc)
        
        # Send to teacher via Telegram
        try:
            from bot_handler import send_to_teacher
            if teacher.get('telegram_id'):
                send_to_teacher(
                    telegram_id=teacher['telegram_id'],
                    student_name=student.get('name', 'Student'),
                    message=message_text,
                    teacher_id=teacher_id
                )
        except Exception as e:
            logger.warning(f"Could not send Telegram notification: {e}")
        
        return jsonify({
            'success': True,
            'message': {
                'text': message_text,
                'from_student': True,
                'timestamp': datetime.utcnow().isoformat()
            }
        })
        
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return jsonify({'error': 'Failed to send message'}), 500

@app.route('/api/poll_messages/<teacher_id>')
@login_required
def poll_messages(teacher_id):
    """Poll for new messages from a teacher"""
    try:
        since = request.args.get('since')
        
        query = {
            'student_id': session['student_id'],
            'teacher_id': teacher_id,
            'from_student': False
        }
        
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
                query['timestamp'] = {'$gt': since_dt}
            except:
                pass
        
        messages = list(Message.find(query).sort('timestamp', 1))
        
        # Mark as read
        Message.update_many(
            {
                'student_id': session['student_id'],
                'teacher_id': teacher_id,
                'from_student': False,
                'read': False
            },
            {'$set': {'read': True}}
        )
        
        return jsonify({
            'messages': [{
                'text': m['message'],
                'from_student': m['from_student'],
                'timestamp': m['timestamp'].isoformat() if isinstance(m['timestamp'], datetime) else m['timestamp']
            } for m in messages]
        })
        
    except Exception as e:
        logger.error(f"Error polling messages: {e}")
        return jsonify({'messages': []})

# ============================================================================
# STUDENT ASSIGNMENT ROUTES
# ============================================================================

@app.route('/assignments')
@login_required
def assignments_list():
    """List subjects with assignments"""
    student = Student.find_one({'student_id': session['student_id']})
    teacher_ids = student.get('teachers', [])
    
    # Get all assignments from student's teachers
    assignments = list(Assignment.find({
        'teacher_id': {'$in': teacher_ids},
        'status': 'published'
    }))
    
    # Group by subject
    subjects = {}
    for a in assignments:
        subject = a.get('subject', 'General')
        if subject not in subjects:
            subjects[subject] = {
                'name': subject,
                'assignments': [],
                'total': 0,
                'completed': 0
            }
        subjects[subject]['assignments'].append(a)
        subjects[subject]['total'] += 1
        
        # Check if student has submitted
        submission = Submission.find_one({
            'assignment_id': a['assignment_id'],
            'student_id': session['student_id']
        })
        if submission and submission.get('status') in ['submitted', 'ai_reviewed', 'approved']:
            subjects[subject]['completed'] += 1
    
    return render_template('assignments_list.html',
                         student=student,
                         subjects=subjects)

@app.route('/assignments/subject/<subject>')
@login_required
def assignments_by_subject(subject):
    """List assignments for a specific subject"""
    student = Student.find_one({'student_id': session['student_id']})
    teacher_ids = student.get('teachers', [])
    
    assignments = list(Assignment.find({
        'teacher_id': {'$in': teacher_ids},
        'subject': subject,
        'status': 'published'
    }).sort('created_at', -1))
    
    # Add submission status for each
    for a in assignments:
        submission = Submission.find_one({
            'assignment_id': a['assignment_id'],
            'student_id': session['student_id']
        })
        a['submission'] = submission
    
    return render_template('assignments_subject.html',
                         student=student,
                         subject=subject,
                         assignments=assignments)

@app.route('/assignments/<assignment_id>')
@login_required
def view_assignment(assignment_id):
    """View and work on an assignment"""
    student = Student.find_one({'student_id': session['student_id']})
    assignment = Assignment.find_one({'assignment_id': assignment_id})
    
    if not assignment:
        return redirect(url_for('assignments_list'))
    
    # Get existing submission/draft
    submission = Submission.find_one({
        'assignment_id': assignment_id,
        'student_id': session['student_id']
    })
    
    teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
    
    return render_template('assignment_view.html',
                         student=student,
                         assignment=assignment,
                         submission=submission,
                         teacher=teacher)

@app.route('/assignments/<assignment_id>/save', methods=['POST'])
@login_required
def save_draft(assignment_id):
    """Save assignment draft"""
    try:
        data = request.get_json()
        answers = data.get('answers', {})
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        # Check for existing submission
        existing = Submission.find_one({
            'assignment_id': assignment_id,
            'student_id': session['student_id']
        })
        
        if existing:
            if existing.get('status') in ['submitted', 'ai_reviewed', 'approved']:
                return jsonify({'error': 'Already submitted'}), 400
            
            Submission.update_one(
                {'submission_id': existing['submission_id']},
                {'$set': {
                    'answers': answers,
                    'updated_at': datetime.utcnow()
                }}
            )
            submission_id = existing['submission_id']
        else:
            submission_id = generate_submission_id()
            Submission.insert_one({
                'submission_id': submission_id,
                'assignment_id': assignment_id,
                'student_id': session['student_id'],
                'answers': answers,
                'status': 'draft',
                'created_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            })
        
        return jsonify({'success': True, 'submission_id': submission_id})
        
    except Exception as e:
        logger.error(f"Error saving draft: {e}")
        return jsonify({'error': 'Failed to save'}), 500

@app.route('/assignments/<assignment_id>/feedback', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def get_ai_feedback(assignment_id):
    """Get AI feedback on current answers"""
    try:
        data = request.get_json()
        answers = data.get('answers', {})
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
        
        # Create temporary submission object for feedback
        temp_submission = {
            'answers': answers,
            'student_id': session['student_id']
        }
        
        feedback = mark_submission(temp_submission, assignment, teacher)
        
        return jsonify({
            'success': True,
            'feedback': feedback.get('overall', 'No feedback generated')
        })
        
    except Exception as e:
        logger.error(f"Error getting AI feedback: {e}")
        return jsonify({'error': 'Failed to get feedback'}), 500

@app.route('/assignments/<assignment_id>/submit', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def submit_assignment(assignment_id):
    """Submit assignment for review"""
    try:
        data = request.get_json()
        answers = data.get('answers', {})
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        student = Student.find_one({'student_id': session['student_id']})
        teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
        
        # Check for existing submission
        existing = Submission.find_one({
            'assignment_id': assignment_id,
            'student_id': session['student_id']
        })
        
        submission_id = existing['submission_id'] if existing else generate_submission_id()
        
        # Generate AI feedback
        temp_submission = {'answers': answers, 'student_id': session['student_id']}
        ai_feedback = mark_submission(temp_submission, assignment, teacher)
        
        submission_data = {
            'answers': answers,
            'status': 'ai_reviewed' if ai_feedback and not ai_feedback.get('error') else 'submitted',
            'ai_feedback': ai_feedback,
            'submitted_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }
        
        if existing:
            Submission.update_one(
                {'submission_id': submission_id},
                {'$set': submission_data}
            )
        else:
            submission_data.update({
                'submission_id': submission_id,
                'assignment_id': assignment_id,
                'student_id': session['student_id'],
                'created_at': datetime.utcnow()
            })
            Submission.insert_one(submission_data)
        
        # Notify teacher
        try:
            submission_data['submission_id'] = submission_id
            notify_submission_ready(submission_data, assignment, student, teacher)
        except Exception as e:
            logger.warning(f"Could not send notification: {e}")
        
        return jsonify({
            'success': True,
            'submission_id': submission_id,
            'message': 'Assignment submitted successfully!'
        })
        
    except Exception as e:
        logger.error(f"Error submitting assignment: {e}")
        return jsonify({'error': 'Failed to submit'}), 500

@app.route('/submissions')
@login_required
def student_submissions():
    """View student's submissions"""
    student = Student.find_one({'student_id': session['student_id']})
    
    submissions = list(Submission.find({
        'student_id': session['student_id']
    }).sort('submitted_at', -1))
    
    # Add assignment details
    for s in submissions:
        assignment = Assignment.find_one({'assignment_id': s['assignment_id']})
        s['assignment'] = assignment
    
    return render_template('submissions.html',
                         student=student,
                         submissions=submissions)

@app.route('/submissions/<submission_id>')
@login_required
def view_submission(submission_id):
    """View a specific submission"""
    student = Student.find_one({'student_id': session['student_id']})
    submission = Submission.find_one({
        'submission_id': submission_id,
        'student_id': session['student_id']
    })
    
    if not submission:
        return redirect(url_for('student_submissions'))
    
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']}) if assignment else None
    
    return render_template('submission_view.html',
                         student=student,
                         submission=submission,
                         assignment=assignment,
                         teacher=teacher)

@app.route('/submissions/<submission_id>/pdf')
@login_required
def download_submission_pdf(submission_id):
    """Download submission as PDF"""
    student = Student.find_one({'student_id': session['student_id']})
    submission = Submission.find_one({
        'submission_id': submission_id,
        'student_id': session['student_id']
    })
    
    if not submission:
        return redirect(url_for('student_submissions'))
    
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    
    pdf_content = generate_feedback_pdf(submission, assignment, student)
    
    if not pdf_content:
        return redirect(url_for('view_submission', submission_id=submission_id))
    
    return send_file(
        io.BytesIO(pdf_content),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"feedback_{submission_id}.pdf"
    )

# ============================================================================
# TEACHER ROUTES
# ============================================================================

@app.route('/teacher/dashboard')
@teacher_required
def teacher_dashboard():
    """Teacher dashboard with stats"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    if not teacher:
        session.clear()
        return redirect(url_for('teacher_login'))
    
    # Get statistics
    assignments = list(Assignment.find({'teacher_id': session['teacher_id']}))
    assignment_ids = [a['assignment_id'] for a in assignments]
    
    pending_submissions = Submission.count({
        'assignment_id': {'$in': assignment_ids},
        'status': {'$in': ['submitted', 'ai_reviewed']}
    })
    
    approved_submissions = Submission.count({
        'assignment_id': {'$in': assignment_ids},
        'status': 'approved'
    })
    
    # Get recent pending submissions
    recent_pending = list(Submission.find({
        'assignment_id': {'$in': assignment_ids},
        'status': {'$in': ['submitted', 'ai_reviewed']}
    }).sort('submitted_at', -1).limit(10))
    
    # Add details
    for s in recent_pending:
        assignment = Assignment.find_one({'assignment_id': s['assignment_id']})
        student = Student.find_one({'student_id': s['student_id']})
        s['assignment'] = assignment
        s['student'] = student
    
    # Get unread messages count
    unread_messages = Message.count({
        'teacher_id': session['teacher_id'],
        'from_student': True,
        'read': False
    })
    
    # Get students with unread messages
    students_with_messages = Message.distinct('student_id', {
        'teacher_id': session['teacher_id'],
        'from_student': True,
        'read': False
    })
    
    return render_template('teacher_dashboard.html',
                         teacher=teacher,
                         stats={
                             'total_assignments': len(assignments),
                             'pending_submissions': pending_submissions,
                             'approved_submissions': approved_submissions,
                             'unread_messages': unread_messages
                         },
                         recent_pending=recent_pending,
                         students_with_messages=students_with_messages)

@app.route('/teacher/assignments')
@teacher_required
def teacher_assignments():
    """List teacher's assignments"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    assignments = list(Assignment.find({
        'teacher_id': session['teacher_id']
    }).sort('created_at', -1))
    
    # Add submission counts
    for a in assignments:
        a['submission_count'] = Submission.count({'assignment_id': a['assignment_id']})
        a['pending_count'] = Submission.count({
            'assignment_id': a['assignment_id'],
            'status': {'$in': ['submitted', 'ai_reviewed']}
        })
    
    return render_template('teacher_assignments.html',
                         teacher=teacher,
                         assignments=assignments)

@app.route('/teacher/assignments/create', methods=['GET', 'POST'])
@teacher_required
def create_assignment():
    """Create a new assignment"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    if request.method == 'POST':
        try:
            data = request.form
            
            # Get uploaded files
            question_paper = request.files.get('question_paper')
            answer_key = request.files.get('answer_key')
            
            if not question_paper or not answer_key:
                return render_template('teacher_create_assignment.html',
                                     teacher=teacher,
                                     error='Both question paper and answer key PDFs are required')
            
            # Validate file types
            if not question_paper.filename.lower().endswith('.pdf') or not answer_key.filename.lower().endswith('.pdf'):
                return render_template('teacher_create_assignment.html',
                                     teacher=teacher,
                                     error='Only PDF files are allowed')
            
            assignment_id = generate_assignment_id()
            total_marks = int(data.get('total_marks', 100))
            
            # Store files in GridFS
            from gridfs import GridFS
            fs = GridFS(db.db)
            
            # Save question paper
            question_paper_id = fs.put(
                question_paper.read(),
                filename=f"{assignment_id}_question.pdf",
                content_type='application/pdf',
                assignment_id=assignment_id,
                file_type='question_paper'
            )
            
            # Save answer key
            answer_key_id = fs.put(
                answer_key.read(),
                filename=f"{assignment_id}_answer.pdf",
                content_type='application/pdf',
                assignment_id=assignment_id,
                file_type='answer_key'
            )
            
            Assignment.insert_one({
                'assignment_id': assignment_id,
                'teacher_id': session['teacher_id'],
                'title': data.get('title', 'Untitled'),
                'subject': data.get('subject', 'General'),
                'instructions': data.get('instructions', ''),
                'total_marks': total_marks,
                'question_paper_id': question_paper_id,
                'answer_key_id': answer_key_id,
                'question_paper_name': question_paper.filename,
                'answer_key_name': answer_key.filename,
                'due_date': data.get('due_date') or None,
                'status': 'published' if data.get('publish') else 'draft',
                'created_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            })
            
            return redirect(url_for('teacher_assignments'))
            
        except Exception as e:
            logger.error(f"Error creating assignment: {e}")
            return render_template('teacher_create_assignment.html',
                                 teacher=teacher,
                                 error=f'Failed to create assignment: {str(e)}')
    
    return render_template('teacher_create_assignment.html', teacher=teacher)

@app.route('/teacher/assignments/<assignment_id>/edit', methods=['GET', 'POST'])
@teacher_required
def edit_assignment(assignment_id):
    """Edit an existing assignment"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    
    if not assignment:
        return redirect(url_for('teacher_assignments'))
    
    if request.method == 'POST':
        try:
            data = request.form
            
            update_data = {
                'title': data.get('title', assignment['title']),
                'subject': data.get('subject', assignment['subject']),
                'instructions': data.get('instructions', ''),
                'total_marks': int(data.get('total_marks', assignment.get('total_marks', 100))),
                'due_date': data.get('due_date') or None,
                'status': 'published' if data.get('publish') else 'draft',
                'updated_at': datetime.utcnow()
            }
            
            # Handle new file uploads
            from gridfs import GridFS
            fs = GridFS(db.db)
            
            question_paper = request.files.get('question_paper')
            if question_paper and question_paper.filename:
                if question_paper.filename.lower().endswith('.pdf'):
                    # Delete old file if exists
                    if assignment.get('question_paper_id'):
                        try:
                            fs.delete(assignment['question_paper_id'])
                        except:
                            pass
                    
                    # Save new file
                    question_paper_id = fs.put(
                        question_paper.read(),
                        filename=f"{assignment_id}_question.pdf",
                        content_type='application/pdf',
                        assignment_id=assignment_id,
                        file_type='question_paper'
                    )
                    update_data['question_paper_id'] = question_paper_id
                    update_data['question_paper_name'] = question_paper.filename
            
            answer_key = request.files.get('answer_key')
            if answer_key and answer_key.filename:
                if answer_key.filename.lower().endswith('.pdf'):
                    # Delete old file if exists
                    if assignment.get('answer_key_id'):
                        try:
                            fs.delete(assignment['answer_key_id'])
                        except:
                            pass
                    
                    # Save new file
                    answer_key_id = fs.put(
                        answer_key.read(),
                        filename=f"{assignment_id}_answer.pdf",
                        content_type='application/pdf',
                        assignment_id=assignment_id,
                        file_type='answer_key'
                    )
                    update_data['answer_key_id'] = answer_key_id
                    update_data['answer_key_name'] = answer_key.filename
            
            Assignment.update_one(
                {'assignment_id': assignment_id},
                {'$set': update_data}
            )
            
            return redirect(url_for('teacher_assignments'))
            
        except Exception as e:
            logger.error(f"Error updating assignment: {e}")
    
    return render_template('teacher_edit_assignment.html',
                         teacher=teacher,
                         assignment=assignment)

@app.route('/teacher/assignments/<assignment_id>/file/<file_type>')
@teacher_required
def download_assignment_file(assignment_id, file_type):
    """Download assignment PDF file"""
    from gridfs import GridFS
    from flask import Response
    
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    
    if not assignment:
        return 'Assignment not found', 404
    
    file_id_field = f"{file_type}_id"
    file_name_field = f"{file_type}_name"
    
    if file_id_field not in assignment:
        return 'File not found', 404
    
    fs = GridFS(db.db)
    try:
        file_data = fs.get(assignment[file_id_field])
        return Response(
            file_data.read(),
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'inline; filename="{assignment.get(file_name_field, "document.pdf")}"'
            }
        )
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return 'File not found', 404

@app.route('/teacher/assignments/<assignment_id>/delete', methods=['POST'])
@teacher_required
def delete_assignment(assignment_id):
    """Delete an assignment"""
    try:
        assignment = Assignment.find_one({
            'assignment_id': assignment_id,
            'teacher_id': session['teacher_id']
        })
        
        if assignment:
            # Check for submissions
            submission_count = Submission.count({'assignment_id': assignment_id})
            
            if submission_count > 0:
                return jsonify({'error': 'Cannot delete - has submissions'}), 400
            
            Assignment.update_one(
                {'assignment_id': assignment_id},
                {'$set': {'status': 'deleted', 'deleted_at': datetime.utcnow()}}
            )
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"Error deleting assignment: {e}")
        return jsonify({'error': 'Failed to delete'}), 500

@app.route('/teacher/submissions')
@teacher_required
def teacher_submissions():
    """List all submissions for teacher's assignments"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    # Get filter params
    status_filter = request.args.get('status', 'pending')
    assignment_filter = request.args.get('assignment', '')
    
    # Get teacher's assignments
    assignments = list(Assignment.find({'teacher_id': session['teacher_id']}))
    assignment_ids = [a['assignment_id'] for a in assignments]
    assignment_map = {a['assignment_id']: a for a in assignments}
    
    # Build query
    query = {'assignment_id': {'$in': assignment_ids}}
    
    if status_filter == 'pending':
        query['status'] = {'$in': ['submitted', 'ai_reviewed']}
    elif status_filter == 'approved':
        query['status'] = 'approved'
    
    if assignment_filter:
        query['assignment_id'] = assignment_filter
    
    submissions = list(Submission.find(query).sort('submitted_at', -1))
    
    # Add details
    for s in submissions:
        s['assignment'] = assignment_map.get(s['assignment_id'], {})
        student = Student.find_one({'student_id': s['student_id']})
        s['student'] = student
    
    return render_template('teacher_submissions.html',
                         teacher=teacher,
                         submissions=submissions,
                         assignments=assignments,
                         status_filter=status_filter,
                         assignment_filter=assignment_filter)

@app.route('/teacher/submissions/<submission_id>/review', methods=['GET'])
@app.route('/teacher/review/<submission_id>', methods=['GET'])
@teacher_required
def review_submission(submission_id):
    """Review a student submission with side-by-side feedback"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    submission = Submission.find_one({'submission_id': submission_id})
    
    if not submission:
        return redirect(url_for('teacher_submissions'))
    
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    
    if not assignment or assignment['teacher_id'] != session['teacher_id']:
        return redirect(url_for('teacher_submissions'))
    
    student = Student.find_one({'student_id': submission['student_id']})
    
    # Get AI feedback if available
    ai_feedback = submission.get('ai_feedback', {})
    
    # Get page count
    page_count = submission.get('page_count', len(submission.get('file_ids', [1])))
    
    return render_template('teacher_review.html',
                         teacher=teacher,
                         submission=submission,
                         assignment=assignment,
                         student=student,
                         ai_feedback=ai_feedback,
                         page_count=page_count)

@app.route('/teacher/submission/<submission_id>/file/<int:file_index>')
@teacher_required
def view_submission_file(submission_id, file_index):
    """Serve submission file (image or PDF page)"""
    from gridfs import GridFS
    
    submission = Submission.find_one({'submission_id': submission_id})
    if not submission:
        return 'Not found', 404
    
    # Verify teacher access
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    if not assignment or assignment['teacher_id'] != session['teacher_id']:
        return 'Unauthorized', 403
    
    file_ids = submission.get('file_ids', [])
    if file_index >= len(file_ids):
        return 'File not found', 404
    
    fs = GridFS(db.db)
    try:
        from bson import ObjectId
        file_data = fs.get(ObjectId(file_ids[file_index]))
        content_type = file_data.content_type or 'application/octet-stream'
        return Response(
            file_data.read(),
            mimetype=content_type,
            headers={'Content-Disposition': 'inline'}
        )
    except Exception as e:
        logger.error(f"Error serving file: {e}")
        return 'File not found', 404

@app.route('/teacher/review/<submission_id>/save', methods=['POST'])
@teacher_required
def save_review_feedback(submission_id):
    """Save teacher feedback edits"""
    try:
        data = request.get_json()
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        # Update teacher feedback
        teacher_feedback = {
            'questions': data.get('questions', {}),
            'overall_feedback': data.get('overall_feedback', ''),
            'total_marks': data.get('total_marks'),
            'edited_at': datetime.utcnow(),
            'edited_by': session['teacher_id']
        }
        
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'teacher_feedback': teacher_feedback,
                'final_marks': data.get('total_marks'),
                'updated_at': datetime.utcnow()
            }}
        )
        
        return jsonify({'success': True, 'message': 'Feedback saved'})
        
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/send', methods=['POST'])
@teacher_required
def send_feedback_to_student(submission_id):
    """Send feedback to student via Telegram"""
    try:
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        student = Student.find_one({'student_id': submission['student_id']})
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        
        # Update status
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'status': 'reviewed',
                'reviewed_at': datetime.utcnow()
            }}
        )
        
        # Send Telegram notification if student has linked account
        if student and student.get('telegram_id'):
            try:
                from telegram import Bot
                bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
                if bot_token:
                    import asyncio
                    
                    async def send_notification():
                        bot = Bot(token=bot_token)
                        
                        # Format feedback message
                        feedback = submission.get('teacher_feedback', {})
                        marks = submission.get('final_marks', 'N/A')
                        total = assignment.get('total_marks', 100)
                        
                        message = (
                            f"📬 *Assignment Feedback*\n\n"
                            f"📝 {assignment.get('title')}\n"
                            f"📊 Marks: *{marks}/{total}*\n\n"
                        )
                        
                        if feedback.get('overall_feedback'):
                            message += f"💬 {feedback['overall_feedback']}\n\n"
                        
                        message += f"👨‍🏫 Reviewed by: {teacher.get('name', 'Teacher')}"
                        
                        await bot.send_message(
                            chat_id=student['telegram_id'],
                            text=message,
                            parse_mode='Markdown'
                        )
                    
                    # Run async function
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(send_notification())
                    loop.close()
                    
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        
        return jsonify({'success': True, 'message': 'Feedback sent to student'})
        
    except Exception as e:
        logger.error(f"Error sending feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/pdf')
@teacher_required
def download_feedback_pdf(submission_id):
    """Generate and download PDF feedback report"""
    from utils.pdf_generator import generate_review_pdf
    
    submission = Submission.find_one({'submission_id': submission_id})
    if not submission:
        return 'Not found', 404
    
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    if not assignment or assignment['teacher_id'] != session['teacher_id']:
        return 'Unauthorized', 403
    
    student = Student.find_one({'student_id': submission['student_id']})
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    try:
        pdf_content = generate_review_pdf(submission, assignment, student, teacher)
        
        filename = f"feedback_{student['student_id']}_{assignment['assignment_id']}.pdf"
        
        return Response(
            pdf_content,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        return f'Error generating PDF: {str(e)}', 500

@app.route('/teacher/submissions/<submission_id>/approve', methods=['POST'])
@teacher_required
def approve_submission(submission_id):
    """Approve a submission"""
    try:
        data = request.get_json()
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        teacher_review = {
            'comments': data.get('comments', ''),
            'final_score': int(data.get('final_score', 0)),
            'reviewed_at': datetime.utcnow(),
            'reviewed_by': session['teacher_id']
        }
        
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'status': 'approved',
                'teacher_review': teacher_review,
                'approved_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            }}
        )
        
        # Optionally upload to Google Drive
        student = Student.find_one({'student_id': submission['student_id']})
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        
        if teacher.get('google_drive_folder_id'):
            try:
                pdf_content = generate_feedback_pdf(submission, assignment, student)
                if pdf_content:
                    drive_manager = get_teacher_drive_manager(teacher)
                    if drive_manager:
                        drive_manager.upload_content(
                            pdf_content,
                            f"{student['student_id']}_{assignment['assignment_id']}_feedback.pdf"
                        )
            except Exception as e:
                logger.warning(f"Could not upload to Drive: {e}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"Error approving submission: {e}")
        return jsonify({'error': 'Failed to approve'}), 500

@app.route('/teacher/settings', methods=['GET', 'POST'])
@teacher_required
def teacher_settings():
    """Teacher settings page"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    if request.method == 'POST':
        try:
            data = request.form
            
            update_data = {}
            
            # Update Anthropic API key
            if data.get('anthropic_api_key'):
                encrypted = encrypt_api_key(data['anthropic_api_key'])
                if encrypted:
                    update_data['anthropic_api_key'] = encrypted
            
            # Update Google Drive folder
            if data.get('google_drive_folder_id'):
                update_data['google_drive_folder_id'] = data['google_drive_folder_id']
            
            # Update subjects
            if data.get('subjects'):
                subjects = [s.strip() for s in data['subjects'].split(',') if s.strip()]
                update_data['subjects'] = subjects
            
            if update_data:
                update_data['updated_at'] = datetime.utcnow()
                Teacher.update_one(
                    {'teacher_id': session['teacher_id']},
                    {'$set': update_data}
                )
                
                # Refresh teacher data
                teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
            
            return render_template('teacher_settings.html',
                                 teacher=teacher,
                                 success='Settings updated successfully')
            
        except Exception as e:
            logger.error(f"Error updating settings: {e}")
            return render_template('teacher_settings.html',
                                 teacher=teacher,
                                 error='Failed to update settings')
    
    return render_template('teacher_settings.html', teacher=teacher)

@app.route('/teacher/messages')
@teacher_required
def teacher_messages():
    """View messages from students"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    # Get students who have messaged this teacher
    student_ids = Message.distinct('student_id', {'teacher_id': session['teacher_id']})
    students = list(Student.find({'student_id': {'$in': student_ids}}))
    
    # Add unread counts and last message
    for s in students:
        s['unread_count'] = Message.count({
            'student_id': s['student_id'],
            'teacher_id': session['teacher_id'],
            'from_student': True,
            'read': False
        })
        
        last_msg = list(Message.find({
            'student_id': s['student_id'],
            'teacher_id': session['teacher_id']
        }).sort('timestamp', -1).limit(1))
        
        s['last_message'] = last_msg[0] if last_msg else None
    
    # Sort by unread count, then by last message time
    students.sort(key=lambda x: (-x['unread_count'], -(x['last_message']['timestamp'].timestamp() if x.get('last_message') else 0)))
    
    return render_template('teacher_messages.html',
                         teacher=teacher,
                         students=students)

@app.route('/teacher/messages/<student_id>')
@teacher_required
def teacher_chat(student_id):
    """Chat with a specific student"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    student = Student.find_one({'student_id': student_id})
    
    if not student:
        return redirect(url_for('teacher_messages'))
    
    # Get messages
    messages = list(Message.find({
        'student_id': student_id,
        'teacher_id': session['teacher_id']
    }).sort('timestamp', 1))
    
    # Mark as read
    Message.update_many(
        {
            'student_id': student_id,
            'teacher_id': session['teacher_id'],
            'from_student': True,
            'read': False
        },
        {'$set': {'read': True}}
    )
    
    return render_template('teacher_chat.html',
                         teacher=teacher,
                         student=student,
                         messages=messages)

@app.route('/api/teacher/send_message', methods=['POST'])
@teacher_required
def teacher_send_message():
    """Send a message to a student"""
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        message_text = data.get('message', '').strip()
        
        if not student_id or not message_text:
            return jsonify({'error': 'Missing data'}), 400
        
        student = Student.find_one({'student_id': student_id})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Save message
        Message.insert_one({
            'student_id': student_id,
            'teacher_id': session['teacher_id'],
            'message': message_text,
            'from_student': False,
            'timestamp': datetime.utcnow(),
            'read': False
        })
        
        return jsonify({
            'success': True,
            'message': {
                'text': message_text,
                'from_student': False,
                'timestamp': datetime.utcnow().isoformat()
            }
        })
        
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return jsonify({'error': 'Failed to send'}), 500

# ============================================================================
# ADMIN ROUTES
# ============================================================================

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    """Admin dashboard"""
    stats = {
        'students': Student.count({}),
        'teachers': Teacher.count({}),
        'classes': Class.count({}),
        'assignments': Assignment.count({'status': 'published'}),
        'submissions': Submission.count({})
    }
    
    teachers = list(Teacher.find({}))
    classes = list(Class.find({}))
    
    # Add student counts to teachers
    for t in teachers:
        t['student_count'] = Student.count({'teachers': t['teacher_id']})
    
    # Add student counts to classes
    for c in classes:
        c['student_count'] = Student.count({'class': c['class_id']})
    
    return render_template('admin_dashboard.html',
                         stats=stats,
                         teachers=teachers,
                         classes=classes)

@app.route('/admin/import_students', methods=['POST'])
@admin_required
def import_students():
    """Import students from CSV/JSON"""
    try:
        data = request.get_json()
        students_data = data.get('students', [])
        
        if not students_data:
            return jsonify({'error': 'No student data provided'}), 400
        
        imported = 0
        errors = []
        
        for s in students_data:
            try:
                student_id = s.get('student_id', '').upper()
                
                if not student_id:
                    errors.append(f"Missing student_id")
                    continue
                
                # Check if exists
                if Student.find_one({'student_id': student_id}):
                    errors.append(f"{student_id} already exists")
                    continue
                
                password = s.get('password', 'student123')
                
                Student.insert_one({
                    'student_id': student_id,
                    'name': s.get('name', student_id),
                    'class': s.get('class', ''),
                    'password_hash': hash_password(password),
                    'teachers': s.get('teachers', []),
                    'created_at': datetime.utcnow()
                })
                
                imported += 1
                
            except Exception as e:
                errors.append(f"Error with {s.get('student_id', 'unknown')}: {str(e)}")
        
        return jsonify({
            'success': True,
            'imported': imported,
            'errors': errors
        })
        
    except Exception as e:
        logger.error(f"Error importing students: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/add_teacher', methods=['POST'])
@admin_required
def add_teacher():
    """Add a new teacher"""
    try:
        data = request.get_json()
        
        teacher_id = data.get('teacher_id', '').upper()
        
        if not teacher_id:
            return jsonify({'error': 'Teacher ID required'}), 400
        
        if Teacher.find_one({'teacher_id': teacher_id}):
            return jsonify({'error': 'Teacher ID already exists'}), 400
        
        password = data.get('password', 'teacher123')
        
        Teacher.insert_one({
            'teacher_id': teacher_id,
            'name': data.get('name', teacher_id),
            'password_hash': hash_password(password),
            'subjects': data.get('subjects', []),
            'classes': data.get('classes', []),
            'telegram_id': None,
            'created_at': datetime.utcnow()
        })
        
        return jsonify({'success': True, 'teacher_id': teacher_id})
        
    except Exception as e:
        logger.error(f"Error adding teacher: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/assign_teacher', methods=['POST'])
@admin_required
def assign_teacher():
    """Assign a teacher to students"""
    try:
        data = request.get_json()
        
        teacher_id = data.get('teacher_id')
        class_id = data.get('class_id')
        student_ids = data.get('student_ids', [])
        
        if not teacher_id:
            return jsonify({'error': 'Teacher ID required'}), 400
        
        teacher = Teacher.find_one({'teacher_id': teacher_id})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        updated = 0
        
        if class_id:
            # Assign to all students in class
            result = Student.update_many(
                {'class': class_id},
                {'$addToSet': {'teachers': teacher_id}}
            )
            updated = result.modified_count
        elif student_ids:
            # Assign to specific students
            result = Student.update_many(
                {'student_id': {'$in': student_ids}},
                {'$addToSet': {'teachers': teacher_id}}
            )
            updated = result.modified_count
        
        return jsonify({'success': True, 'updated': updated})
        
    except Exception as e:
        logger.error(f"Error assigning teacher: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/add_class', methods=['POST'])
@admin_required
def add_class():
    """Add a new class"""
    try:
        data = request.get_json()
        
        class_id = data.get('class_id', '').upper()
        
        if not class_id:
            return jsonify({'error': 'Class ID required'}), 400
        
        Class.update_one(
            {'class_id': class_id},
            {'$set': {
                'class_id': class_id,
                'name': data.get('name', class_id),
                'updated_at': datetime.utcnow()
            }},
            upsert=True
        )
        
        return jsonify({'success': True, 'class_id': class_id})
        
    except Exception as e:
        logger.error(f"Error adding class: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/api/students')
@admin_required
def get_students():
    """Get list of students with optional class filter"""
    try:
        class_filter = request.args.get('class', '')
        
        query = {}
        if class_filter:
            query['class'] = class_filter
        
        students = list(Student.find(query))
        
        return jsonify({
            'success': True,
            'students': [{
                'student_id': s.get('student_id'),
                'name': s.get('name'),
                'class': s.get('class'),
                'teachers': s.get('teachers', [])
            } for s in students]
        })
        
    except Exception as e:
        logger.error(f"Error getting students: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/delete_students', methods=['POST'])
@admin_required
def delete_students():
    """Delete students by IDs or by class"""
    try:
        data = request.get_json()
        student_ids = data.get('student_ids', [])
        class_id = data.get('class_id')
        
        deleted = 0
        
        if class_id:
            # Delete all students in class
            result = db.db.students.delete_many({'class': class_id})
            deleted = result.deleted_count
        elif student_ids:
            # Delete specific students
            result = db.db.students.delete_many({'student_id': {'$in': student_ids}})
            deleted = result.deleted_count
        
        return jsonify({'success': True, 'deleted': deleted})
        
    except Exception as e:
        logger.error(f"Error deleting students: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/delete_teachers', methods=['POST'])
@admin_required
def delete_teachers():
    """Delete teachers by IDs"""
    try:
        data = request.get_json()
        teacher_ids = data.get('teacher_ids', [])
        
        if not teacher_ids:
            return jsonify({'error': 'No teachers specified'}), 400
        
        # Remove teacher from all students
        Student.update_many(
            {'teachers': {'$in': teacher_ids}},
            {'$pull': {'teachers': {'$in': teacher_ids}}}
        )
        
        # Delete teachers
        result = db.db.teachers.delete_many({'teacher_id': {'$in': teacher_ids}})
        
        return jsonify({'success': True, 'deleted': result.deleted_count})
        
    except Exception as e:
        logger.error(f"Error deleting teachers: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', error='Page not found', code=404), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', error='Server error', code=500), 500

@app.errorhandler(429)
def rate_limit(e):
    return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
