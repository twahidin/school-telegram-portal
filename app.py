from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
import os
import io
from datetime import datetime, timedelta, timezone
from models import db, Student, Teacher, Message, Class, TeachingGroup, Assignment, Submission, Module, ModuleResource, ModuleTextbook, StudentModuleMastery, StudentLearningProfile, LearningSession
from utils.auth import hash_password, verify_password, validate_password, generate_assignment_id, generate_submission_id, encrypt_api_key, decrypt_api_key
from utils.ai_marking import get_teacher_ai_service, mark_submission
from utils.google_drive import get_teacher_drive_manager, upload_assignment_file
from utils.pdf_generator import generate_feedback_pdf
from utils.notifications import notify_submission_ready
from utils.module_ai import (
    generate_modules_from_syllabus,
    assess_student_understanding,
    generate_interactive_assessment,
    analyze_writing_submission,
)
from utils import rag_service
import logging
import math
import uuid
import json
import base64
import subprocess
import tempfile
import PyPDF2

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# PDF TEXT EXTRACTION UTILITY
# ============================================================================

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extract text content from a PDF file.
    This allows the AI to use text instead of vision for PDFs, reducing costs.
    
    Args:
        pdf_bytes: Raw bytes of the PDF file
        
    Returns:
        Extracted text content as a string
    """
    try:
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text_content = []
        
        for page_num, page in enumerate(pdf_reader.pages):
            page_text = page.extract_text()
            if page_text:
                text_content.append(f"--- Page {page_num + 1} ---\n{page_text}")
        
        extracted_text = "\n\n".join(text_content)
        
        # Log extraction stats
        if extracted_text.strip():
            logger.info(f"Extracted {len(extracted_text)} characters from {len(pdf_reader.pages)} PDF pages")
        else:
            logger.warning("PDF text extraction yielded empty result - PDF may contain only images")
        
        return extracted_text.strip()
    except Exception as e:
        logger.error(f"Error extracting text from PDF: {e}")
        return ""

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'change-this-in-production-please')
app.config['MONGODB_URI'] = os.getenv('MONGODB_URI') or os.getenv('MONGO_URL')
app.config['MONGODB_DB'] = os.getenv('MONGODB_DB', 'school_portal')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# Initialize database
db.init_app(app)

# ============================================================================
# JINJA2 FILTERS
# ============================================================================

# Singapore timezone (UTC+8)
SGT = timezone(timedelta(hours=8))

@app.template_filter('sgt')
def sgt_filter(dt):
    """Convert UTC datetime to Singapore time (SGT, UTC+8)"""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        # If datetime is naive (no timezone), assume it's UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Convert to Singapore time
        return dt.astimezone(SGT)
    return dt

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
    """Require student login."""
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

def student_or_teacher_required(f):
    """Require either student or teacher login. For API routes, return JSON 401 so fetch works."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('student_id') or session.get('teacher_id'):
            return f(*args, **kwargs)
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect(url_for('login'))
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
    """Unified login for students, teachers, and admins"""
    if request.method == 'POST':
        user_id = request.form.get('user_id', '').strip()
        # Also check old field name for backwards compatibility
        if not user_id:
            user_id = request.form.get('student_id', '').strip()
        user_id_upper = user_id.upper()
        password = request.form.get('password', '')
        
        if not user_id or not password:
            return render_template('login.html', error='Please enter both ID and password')
        
        # Check if admin login (username: "admin")
        if user_id.lower() == 'admin':
            if password == ADMIN_PASSWORD:
                session['is_admin'] = True
                session.permanent = True
                return redirect(url_for('admin_dashboard'))
            else:
                return render_template('login.html', error='Invalid admin password')
        
        # Check if teacher login
        teacher = Teacher.find_one({'teacher_id': user_id_upper})
        if teacher:
            if verify_password(password, teacher.get('password_hash', '')):
                session['teacher_id'] = user_id_upper
                session['teacher_name'] = teacher.get('name', 'Teacher')
                session.permanent = True
                return redirect(url_for('teacher_dashboard'))
            else:
                return render_template('login.html', error='Invalid password')
        
        # Check if student login
        student = Student.find_one({'student_id': user_id_upper})
        if student:
            password_hash = student.get('password_hash', '')
            
            # If password_hash is missing or empty, set it to default 'student123' and require change
            if not password_hash:
                logger.warning(f"Student {user_id_upper} has no password_hash, setting default")
                default_hash = hash_password('student123')
                Student.update_one(
                    {'student_id': user_id_upper},
                    {'$set': {'password_hash': default_hash, 'must_change_password': True}}
                )
                password_hash = default_hash
            
            # Verify password only against stored hash. Never overwrite hash on failed verify.
            password_valid = False
            try:
                password_valid = verify_password(password, password_hash)
            except Exception as e:
                logger.error(f"Error verifying password for {user_id_upper}: {e}")
            
            if password_valid:
                # Flag default-password users so they're prompted to change on dashboard
                if password == 'student123':
                    Student.update_one(
                        {'student_id': user_id_upper},
                        {'$set': {'must_change_password': True}}
                    )
                session['student_id'] = user_id_upper
                session['student_name'] = student.get('name', 'Student')
                session['student_class'] = student.get('class', '')
                session.permanent = True
                return redirect(url_for('dashboard'))
            else:
                logger.warning(f"Invalid password attempt for student {user_id_upper}")
                return render_template('login.html', error='Invalid password')
        
        # User not found
        return render_template('login.html', error='User not found. Check your ID.')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """Student logout"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/teacher/login', methods=['GET', 'POST'])
def teacher_login():
    """Redirect to unified login"""
    return redirect(url_for('login'))

@app.route('/teacher/logout')
def teacher_logout():
    """Teacher logout"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Redirect to unified login"""
    return redirect(url_for('login'))

@app.route('/admin/logout')
def admin_logout():
    """Admin logout"""
    session.clear()
    return redirect(url_for('login'))

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_student_teacher_ids(student_id):
    """Get all teacher IDs for a student, including those from teaching groups"""
    student = Student.find_one({'student_id': student_id})
    if not student:
        return []
    
    # Start with directly assigned teachers
    teacher_ids = set(student.get('teachers', []))
    
    # Find all teaching groups that include this student
    teaching_groups = TeachingGroup.find({'student_ids': student_id})
    for group in teaching_groups:
        teacher_id = group.get('teacher_id')
        if teacher_id:
            teacher_ids.add(teacher_id)
    
    return list(teacher_ids)

def can_student_access_assignment(student, assignment):
    """Check if a student can access an assignment based on its target (class or teaching group)"""
    # If no target is specified, assignment is available to all students of the teacher
    target_type = assignment.get('target_type', 'class')
    target_class_id = assignment.get('target_class_id')
    target_group_id = assignment.get('target_group_id')
    
    # No target specified - accessible to all students of this teacher
    if not target_class_id and not target_group_id:
        return True
    
    # Get student's class(es)
    student_classes = student.get('classes', [])
    if not student_classes and student.get('class'):
        student_classes = [student.get('class')]
    
    # Check class-based targeting
    if target_type == 'class' and target_class_id:
        return target_class_id in student_classes
    
    # Check teaching group-based targeting
    if target_type == 'teaching_group' and target_group_id:
        teaching_group = TeachingGroup.find_one({'group_id': target_group_id})
        if teaching_group:
            return student.get('student_id') in teaching_group.get('student_ids', [])
        return False
    
    return True

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
    
    # Get student's classes (support both 'class' and 'classes' fields)
    student_classes = student.get('classes', [])
    if not student_classes and student.get('class'):
        student_classes = [student.get('class')]
    
    # Get assigned teachers (including those from teaching groups)
    teacher_ids = get_student_teacher_ids(session['student_id'])
    teachers = list(Teacher.find({'teacher_id': {'$in': teacher_ids}}))
    
    # Get unread message counts and shared classes for each teacher
    for t in teachers:
        unread = Message.count({
            'student_id': session['student_id'],
            'teacher_id': t['teacher_id'],
            'from_student': False,
            'read': False
        })
        t['unread_count'] = unread
        
        # Find shared classes between student and teacher
        teacher_classes = t.get('classes', [])
        shared_classes = list(set(student_classes) & set(teacher_classes))
        t['shared_classes'] = shared_classes
    
    # Calculate assignment status counts for the dashboard
    teacher_ids = get_student_teacher_ids(session['student_id'])
    all_assignments = list(Assignment.find({
        'teacher_id': {'$in': teacher_ids},
        'status': 'published'
    }))
    
    # Filter assignments based on target class/teaching group
    assignments = [a for a in all_assignments if can_student_access_assignment(student, a)]
    
    # Count assignments by status
    not_submitted_count = 0
    pending_review_count = 0
    feedback_received_count = 0
    
    for a in assignments:
        # Use latest submission so resubmission after rejection shows new status
        submission = Submission.find_one(
            {'assignment_id': a['assignment_id'], 'student_id': session['student_id']},
            sort=[('submitted_at', -1), ('created_at', -1)]
        )
        
        if not submission:
            not_submitted_count += 1
        elif submission.get('status') == 'rejected':
            not_submitted_count += 1  # Rejected with no newer submission = can resubmit
        elif submission.get('feedback_sent', False) or submission.get('status') in ['reviewed', 'approved']:
            feedback_received_count += 1  # AI feedback sent or teacher reviewed
        elif submission.get('status') in ['submitted', 'ai_reviewed']:
            pending_review_count += 1  # Waiting for teacher (or AI not sent yet)
    
    assignment_stats = {
        'not_submitted': not_submitted_count,
        'pending_review': pending_review_count,
        'feedback_received': feedback_received_count
    }
    
    must_change = student.get('must_change_password', False)
    return render_template('dashboard.html', 
                         student=student, 
                         teachers=teachers,
                         assignment_stats=assignment_stats,
                         must_change_password=must_change)

@app.route('/chat/<teacher_id>')
@login_required
def chat(teacher_id):
    """Chat interface with a teacher"""
    student = Student.find_one({'student_id': session['student_id']})
    teacher = Teacher.find_one({'teacher_id': teacher_id})
    
    if not teacher:
        return redirect(url_for('dashboard'))
    
    # Check if student is assigned to this teacher (directly or via teaching group)
    teacher_ids = get_student_teacher_ids(session['student_id'])
    if teacher_id not in teacher_ids:
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
                    teacher_id=teacher_id,
                    student_class=student.get('class')
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
    # Get all teacher IDs including those from teaching groups
    teacher_ids = get_student_teacher_ids(session['student_id'])
    
    # Get all assignments from student's teachers (including via teaching groups)
    all_assignments = list(Assignment.find({
        'teacher_id': {'$in': teacher_ids},
        'status': 'published'
    }))
    
    # Filter assignments based on target class/teaching group
    assignments = [a for a in all_assignments if can_student_access_assignment(student, a)]
    
    # Group by subject
    subjects = {}
    total_feedback_count = 0
    
    for a in assignments:
        subject = a.get('subject', 'General')
        if subject not in subjects:
            subjects[subject] = {
                'name': subject,
                'assignments': [],
                'total': 0,
                'submitted': 0,
                'feedback': 0
            }
        subjects[subject]['assignments'].append(a)
        subjects[subject]['total'] += 1
        
        # Check if student has submitted (use latest so resubmission after rejection counts)
        submission = Submission.find_one(
            {'assignment_id': a['assignment_id'], 'student_id': session['student_id']},
            sort=[('submitted_at', -1), ('created_at', -1)]
        )
        
        if submission and submission.get('status') != 'rejected':
            # Check if feedback has been received
            if submission.get('feedback_sent', False) or submission.get('status') == 'reviewed':
                subjects[subject]['feedback'] += 1
                total_feedback_count += 1
            # Check if submitted but no feedback yet
            elif submission.get('status') in ['submitted', 'ai_reviewed']:
                subjects[subject]['submitted'] += 1
    
    return render_template('assignments_list.html',
                         student=student,
                         subjects=subjects,
                         total_feedback_count=total_feedback_count)

@app.route('/assignments/subject/<subject>')
@login_required
def assignments_by_subject(subject):
    """List assignments for a specific subject"""
    student = Student.find_one({'student_id': session['student_id']})
    # Get all teacher IDs including those from teaching groups
    teacher_ids = get_student_teacher_ids(session['student_id'])
    
    all_assignments = list(Assignment.find({
        'teacher_id': {'$in': teacher_ids},
        'subject': subject,
        'status': 'published'
    }).sort('created_at', -1))
    
    # Filter assignments based on target class/teaching group
    assignments = [a for a in all_assignments if can_student_access_assignment(student, a)]
    
    # Count submitted assignments
    submitted_count = 0
    
    # Add submission status and teacher info for each (use latest submission so resubmission after rejection shows new status)
    for a in assignments:
        submission = Submission.find_one(
            {'assignment_id': a['assignment_id'], 'student_id': session['student_id']},
            sort=[('submitted_at', -1), ('created_at', -1)]
        )
        a['submission'] = submission
        
        # Determine status for display from latest submission
        if not submission:
            a['status_display'] = 'Not Submitted'
            a['status_class'] = 'secondary'
        elif submission.get('status') == 'rejected':
            a['status_display'] = 'Rejected'
            a['status_class'] = 'danger'
            submitted_count += 1  # Rejected still counts as submitted (can resubmit)
        elif submission.get('status') in ['submitted', 'ai_reviewed']:
            a['status_display'] = 'Pending Review'
            a['status_class'] = 'warning'
            submitted_count += 1
        elif submission.get('feedback_sent', False) or submission.get('status') in ['reviewed', 'approved']:
            a['status_display'] = 'Feedback Received'
            a['status_class'] = 'success'
            submitted_count += 1
        else:
            a['status_display'] = 'Not Submitted'
            a['status_class'] = 'secondary'
        
        # Add teacher info
        teacher = Teacher.find_one({'teacher_id': a['teacher_id']})
        a['teacher_name'] = teacher.get('name', a['teacher_id']) if teacher else a['teacher_id']
    
    return render_template('assignments_subject.html',
                         student=student,
                         subject=subject,
                         assignments=assignments,
                         total_count=len(assignments),
                         submitted_count=submitted_count)

@app.route('/assignments/<assignment_id>')
@login_required
def view_assignment(assignment_id):
    """View and work on an assignment"""
    student = Student.find_one({'student_id': session['student_id']})
    assignment = Assignment.find_one({'assignment_id': assignment_id})
    
    if not assignment:
        return redirect(url_for('assignments_list'))
    
    # Check if student has access to this teacher (directly or via teaching group)
    teacher_ids = get_student_teacher_ids(session['student_id'])
    if assignment.get('teacher_id') not in teacher_ids:
        return redirect(url_for('assignments_list'))
    
    # Check if student can access this assignment based on target class/teaching group
    if not can_student_access_assignment(student, assignment):
        return redirect(url_for('assignments_list'))
    
    # Get existing submission (submitted, ai_reviewed, or reviewed)
    existing_submission = Submission.find_one({
        'assignment_id': assignment_id,
        'student_id': session['student_id'],
        'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
    })
    
    # If no active submission, check for a rejected one so we can show the reason (e.g. 413 - resubmit with smaller images)
    rejected_submission = None
    if not existing_submission:
        rejected_list = list(Submission.find({
            'assignment_id': assignment_id,
            'student_id': session['student_id'],
            'status': 'rejected'
        }).sort('rejected_at', -1).limit(1))
        rejected_submission = rejected_list[0] if rejected_list else None
    
    teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
    
    return render_template('assignment_view.html',
                         student=student,
                         assignment=assignment,
                         existing_submission=existing_submission,
                         rejected_submission=rejected_submission,
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

@app.route('/api/student/question-help', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def get_question_help_api():
    """Get AI help for a specific question (supports text and images)"""
    from utils.ai_marking import get_question_help
    
    try:
        data = request.get_json()
        assignment_id = data.get('assignment_id')
        question_text = data.get('question', '').strip()
        answer_text = data.get('answer', '').strip()
        question_image = data.get('question_image')  # Base64 encoded image
        answer_image = data.get('answer_image')  # Base64 encoded image
        help_type = data.get('help_type', 'stuck')
        
        # Check if at least question text or image is provided
        if not question_text and not question_image:
            return jsonify({'error': 'Please provide the question text or upload a photo'}), 400
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        # Check if question help is enabled for this assignment
        if not assignment.get('enable_question_help', True):
            return jsonify({
                'error': 'Question help is not available for this assignment.',
                'limit_reached': True
            }), 403
        
        # Check usage limit
        student_id = session['student_id']
        question_help_limit = assignment.get('question_help_limit', 5)
        
        # Get or create usage tracking document
        AIUsage = db.db.ai_usage
        usage = AIUsage.find_one({
            'student_id': student_id,
            'assignment_id': assignment_id
        })
        
        current_question_help_count = usage.get('question_help_count', 0) if usage else 0
        
        if current_question_help_count >= question_help_limit:
            return jsonify({
                'error': f'You have reached the maximum of {question_help_limit} question help requests for this assignment.',
                'limit_reached': True,
                'used': current_question_help_count,
                'limit': question_help_limit
            }), 403
        
        teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
        
        # Get AI help (pass db instance for custom prompts)
        help_response = get_question_help(
            question=question_text,
            student_answer=answer_text,
            help_type=help_type,
            assignment=assignment,
            teacher=teacher,
            db_instance=db,
            question_image=question_image,
            answer_image=answer_image
        )
        
        # Increment usage count on success
        AIUsage.update_one(
            {'student_id': student_id, 'assignment_id': assignment_id},
            {
                '$inc': {'question_help_count': 1},
                '$set': {'updated_at': datetime.utcnow()},
                '$setOnInsert': {'created_at': datetime.utcnow(), 'overall_review_count': 0}
            },
            upsert=True
        )
        
        return jsonify({
            'success': True,
            'help': help_response,
            'usage': {
                'used': current_question_help_count + 1,
                'limit': question_help_limit,
                'remaining': question_help_limit - current_question_help_count - 1
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting question help: {e}")
        return jsonify({'error': 'Failed to get help. Please try again.'}), 500

@app.route('/api/student/ai-usage/<assignment_id>')
@login_required
def get_student_ai_usage(assignment_id):
    """Get student's AI help usage and limits for an assignment"""
    try:
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        student_id = session['student_id']
        
        # Get usage tracking
        AIUsage = db.db.ai_usage
        usage = AIUsage.find_one({
            'student_id': student_id,
            'assignment_id': assignment_id
        })
        
        question_help_count = usage.get('question_help_count', 0) if usage else 0
        overall_review_count = usage.get('overall_review_count', 0) if usage else 0
        
        # Get limits from assignment (with defaults)
        enable_question_help = assignment.get('enable_question_help', True)
        question_help_limit = assignment.get('question_help_limit', 5)
        enable_overall_review = assignment.get('enable_overall_review', True)
        overall_review_limit = assignment.get('overall_review_limit', 1)
        
        return jsonify({
            'success': True,
            'question_help': {
                'enabled': enable_question_help,
                'used': question_help_count,
                'limit': question_help_limit,
                'remaining': max(0, question_help_limit - question_help_count) if enable_question_help else 0
            },
            'overall_review': {
                'enabled': enable_overall_review,
                'used': overall_review_count,
                'limit': overall_review_limit,
                'remaining': max(0, overall_review_limit - overall_review_count) if enable_overall_review else 0
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting AI usage: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/<teacher_id>/availability')
@login_required
def get_teacher_availability(teacher_id):
    """Check if teacher is available based on their messaging hours (Singapore timezone)"""
    try:
        teacher = Teacher.find_one({'teacher_id': teacher_id})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Check if messaging hours are enabled
        hours_enabled = teacher.get('messaging_hours_enabled', True)
        
        if not hours_enabled:
            return jsonify({
                'available': True,
                'hours_enabled': False,
                'message': None
            })
        
        # Get messaging hours
        start_time = teacher.get('messaging_start_time', '07:00')
        end_time = teacher.get('messaging_end_time', '19:00')
        outside_message = teacher.get('outside_hours_message', 
            'I am currently unavailable. I will respond to your message during my available hours.')
        
        # Get current time in Singapore timezone (UTC+8)
        from datetime import datetime, timezone, timedelta
        singapore_tz = timezone(timedelta(hours=8))
        now_sg = datetime.now(singapore_tz)
        current_time = now_sg.strftime('%H:%M')
        
        # Check if current time is within available hours
        is_available = start_time <= current_time <= end_time
        
        return jsonify({
            'available': is_available,
            'hours_enabled': True,
            'start_time': start_time,
            'end_time': end_time,
            'current_time': current_time,
            'timezone': 'Asia/Singapore (UTC+8)',
            'message': outside_message if not is_available else None,
            'teacher_name': teacher.get('name', 'Teacher')
        })
        
    except Exception as e:
        logger.error(f"Error checking teacher availability: {e}")
        return jsonify({'error': str(e)}), 500

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
        
        # Check for existing submission (cannot resubmit unless rejected)
        existing = Submission.find_one({
            'assignment_id': assignment_id,
            'student_id': session['student_id']
        })
        if existing and existing.get('status') in ['submitted', 'ai_reviewed', 'reviewed']:
            if existing.get('submitted_via') == 'manual':
                return jsonify({'error': 'This assignment was submitted by your teacher. You cannot resubmit unless the teacher rejects it.'}), 400
            return jsonify({'error': 'Already submitted'}), 400
        
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
    try:
        student = Student.find_one({'student_id': session['student_id']})
        if not student:
            logger.warning(f"Student not found: {session.get('student_id')}")
            return redirect(url_for('student_submissions'))
        
        submission = Submission.find_one({
            'submission_id': submission_id,
            'student_id': session['student_id']
        })
        
        if not submission:
            logger.warning(f"Submission not found: {submission_id} for student {session.get('student_id')}")
            return redirect(url_for('student_submissions'))
        
        assignment_id = submission.get('assignment_id')
        if not assignment_id:
            logger.error(f"Submission {submission_id} has no assignment_id")
            return redirect(url_for('student_submissions'))
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            logger.error(f"Assignment not found for submission {submission_id}, assignment_id: {assignment_id}")
            return redirect(url_for('student_submissions'))
        
        teacher_id = assignment.get('teacher_id')
        teacher = Teacher.find_one({'teacher_id': teacher_id}) if teacher_id else None
        
        # Ensure submission has required fields with defaults
        if 'feedback_sent' not in submission:
            submission['feedback_sent'] = False
        if 'ai_feedback' not in submission:
            submission['ai_feedback'] = {}
        if 'teacher_feedback' not in submission:
            submission['teacher_feedback'] = {}
        
        return render_template('submission_view.html',
                             student=student,
                             submission=submission,
                             assignment=assignment,
                             teacher=teacher)
    except Exception as e:
        logger.error(f"Error viewing submission {submission_id}: {e}", exc_info=True)
        return redirect(url_for('student_submissions'))


@app.route('/submissions/<submission_id>/send-corrections', methods=['POST'])
@login_required
def send_corrections(submission_id):
    """Save student corrections/challenges and notify teacher."""
    try:
        submission = Submission.find_one({
            'submission_id': submission_id,
            'student_id': session['student_id']
        })
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        if not submission.get('feedback_sent', False):
            return jsonify({'error': 'Feedback not yet sent'}), 400
        if submission.get('correction_sent', False):
            return jsonify({'error': 'Corrections already sent'}), 400

        data = request.get_json() or {}
        questions = data.get('questions') or {}
        criteria = data.get('criteria') or {}
        errors = data.get('errors') or []

        student_corrections = {
            'questions': {k: (v or '').strip() for k, v in questions.items()},
            'criteria': {k: (v or '').strip() for k, v in criteria.items()},
            'errors': [(e.get('text', e) if isinstance(e, dict) else (e or '')).strip() for e in errors]
        }

        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'student_corrections': student_corrections,
                'correction_sent': True,
                'correction_sent_at': datetime.utcnow()
            }}
        )

        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']}) if assignment else None
        student = Student.find_one({'student_id': session['student_id']})
        if teacher and student and assignment:
            try:
                from utils.notifications import notify_correction_challenge_received
                notify_correction_challenge_received(submission, assignment, student, teacher)
            except Exception as e:
                logger.warning(f"Could not send correction notification: {e}")

        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error sending corrections: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/submissions/<submission_id>/pdf')
@login_required
def download_submission_pdf(submission_id):
    """Download submission as PDF"""
    try:
        student = Student.find_one({'student_id': session['student_id']})
        if not student:
            logger.warning(f"Student not found: {session.get('student_id')}")
            return redirect(url_for('student_submissions'))
        
        submission = Submission.find_one({
            'submission_id': submission_id,
            'student_id': session['student_id']
        })
        
        if not submission:
            logger.warning(f"Submission not found: {submission_id} for student {session.get('student_id')}")
            return redirect(url_for('student_submissions'))
        
        assignment_id = submission.get('assignment_id')
        if not assignment_id:
            logger.error(f"Submission {submission_id} has no assignment_id")
            return redirect(url_for('student_submissions'))
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            logger.error(f"Assignment not found for submission {submission_id}, assignment_id: {assignment_id}")
            return redirect(url_for('student_submissions'))
        
        pdf_content = generate_feedback_pdf(submission, assignment, student)
        
        if not pdf_content:
            logger.warning(f"Failed to generate PDF for submission {submission_id}")
            return redirect(url_for('view_submission', submission_id=submission_id))
        
        return send_file(
            io.BytesIO(pdf_content),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"feedback_{submission_id}.pdf"
        )
    except Exception as e:
        logger.error(f"Error downloading PDF for submission {submission_id}: {e}", exc_info=True)
        return redirect(url_for('view_submission', submission_id=submission_id))

@app.route('/student/submit', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def student_submit_files():
    """Submit assignment with photo/PDF upload"""
    from gridfs import GridFS
    from utils.ai_marking import analyze_submission_images, analyze_essay_with_rubrics
    
    try:
        assignment_id = request.form.get('assignment_id')
        file_type = request.form.get('file_type', 'images')
        files = request.files.getlist('files')
        
        if not assignment_id or not files:
            return jsonify({'error': 'Missing assignment or files'}), 400
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        student = Student.find_one({'student_id': session['student_id']})
        teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
        
        from bson import ObjectId
        is_rubric_resubmit = request.form.get('is_rubric_resubmit') == '1'
        marking_type = assignment.get('marking_type', 'standard')

        # Rubric resubmit: allow new submission (new version) instead of blocking
        if is_rubric_resubmit and marking_type == 'rubric':
            existing = None  # always create new submission
            version_count = Submission.count({
                'assignment_id': assignment_id,
                'student_id': session['student_id']
            })
            version = version_count + 1
        else:
            existing = Submission.find_one(
                {'assignment_id': assignment_id, 'student_id': session['student_id']},
                sort=[('submitted_at', -1), ('created_at', -1)]
            )
            if existing and existing.get('status') in ['submitted', 'ai_reviewed', 'reviewed']:
                if existing.get('submitted_via') == 'manual':
                    return jsonify({'error': 'This assignment was submitted by your teacher. You cannot resubmit unless the teacher rejects it.'}), 400
                return jsonify({'error': 'Already submitted'}), 400
            version = 1 if marking_type == 'rubric' else None

        fs = GridFS(db.db)
        if existing:
            submission_id = existing['submission_id']
            # Overwrite: delete old files from GridFS
            for old_fid in existing.get('file_ids', []):
                try:
                    fs.delete(ObjectId(old_fid))
                except Exception as e:
                    logger.warning(f"Error deleting old submission file {old_fid}: {e}")
        else:
            submission_id = generate_submission_id()
        
        # Store files
        file_ids = []
        pages = []
        
        for i, file in enumerate(files):
            file_data = file.read()
            
            # Determine content type
            if file.filename.lower().endswith('.pdf'):
                content_type = 'application/pdf'
                page_type = 'pdf'
            else:
                content_type = 'image/jpeg'
                page_type = 'image'
            
            # Store in GridFS
            file_id = fs.put(
                file_data,
                filename=f"{submission_id}_page_{i+1}.{file.filename.split('.')[-1]}",
                content_type=content_type,
                submission_id=submission_id,
                page_num=i + 1
            )
            file_ids.append(str(file_id))
            
            pages.append({
                'type': page_type,
                'data': file_data,
                'page_num': i + 1
            })
        
        submission = {
            'submission_id': submission_id,
            'assignment_id': assignment_id,
            'student_id': session['student_id'],
            'teacher_id': assignment['teacher_id'],
            'file_ids': file_ids,
            'file_type': 'pdf' if file_type == 'pdf' else 'image',
            'page_count': len(files),
            'status': 'submitted',
            'submitted_at': datetime.utcnow(),
            'submitted_via': 'web',
            'created_at': existing['created_at'] if existing else datetime.utcnow()
        }
        if version is not None:
            submission['version'] = version
        if existing:
            Submission.update_one(
                {'submission_id': submission_id},
                {'$set': submission, '$unset': {'rejection_reason': '', 'rejected_at': '', 'rejected_by': ''}}
            )
        else:
            Submission.insert_one(submission)
        
        # Generate AI feedback
        try:
            marking_type = assignment.get('marking_type', 'standard')
            
            if marking_type == 'rubric':
                # For rubric-based essays, use the essay analysis function
                rubrics_content = None
                if assignment.get('rubrics_id'):
                    try:
                        rubrics_file = fs.get(assignment['rubrics_id'])
                        rubrics_content = rubrics_file.read()
                    except:
                        pass
                
                ai_result = analyze_essay_with_rubrics(pages, assignment, rubrics_content, teacher)
            else:
                # For standard marking, use the question-based analysis
                answer_key_content = None
                if assignment.get('answer_key_id'):
                    try:
                        answer_file = fs.get(assignment['answer_key_id'])
                        answer_key_content = answer_file.read()
                    except:
                        pass
                
                ai_result = analyze_submission_images(pages, assignment, answer_key_content, teacher)
            
            # If AI returned 413 (request too large), auto-reject so student can resubmit with smaller/fewer images
            is_413 = ai_result.get('error_code') == 'request_too_large' or (
                ai_result.get('error') and ('413' in str(ai_result.get('error')) or 'request_too_large' in str(ai_result.get('error')).lower())
            )
            if is_413:
                rejection_reason = (
                    "Your submission was too large to process. Please resubmit with fewer or smaller images: "
                    "e.g. one photo per page, lower resolution, or fewer pages. This helps the system process your work."
                )
                Submission.update_one(
                    {'submission_id': submission_id},
                    {'$set': {
                        'ai_feedback': ai_result,
                        'status': 'rejected',
                        'rejection_reason': rejection_reason,
                        'rejected_at': datetime.utcnow(),
                        'rejected_by': 'system_413'
                    }}
                )
                logger.info(f"Auto-rejected submission {submission_id} due to 413 request_too_large; student can resubmit.")
            else:
                update_fields = {
                    'ai_feedback': ai_result,
                    'status': 'ai_reviewed'
                }
                # If assignment is set to send AI feedback straight away, student can see feedback without teacher review
                if assignment.get('send_ai_feedback_immediately') and not ai_result.get('error'):
                    update_fields['feedback_sent'] = True
                Submission.update_one(
                    {'submission_id': submission_id},
                    {'$set': update_fields}
                )
                # Update profile/mastery when assignment is linked to module and feedback is sent
                if update_fields.get('feedback_sent'):
                    submission_after = Submission.find_one({'submission_id': submission_id})
                    if submission_after:
                        _update_profile_and_mastery_from_assignment(session['student_id'], assignment, submission_after)
        except Exception as e:
            logger.error(f"AI feedback error: {e}")
        
        # Notify teacher
        try:
            notify_submission_ready(submission, assignment, student, teacher)
        except Exception as e:
            logger.warning(f"Notification error: {e}")
        
        # Upload to Google Drive if teacher has it configured and assignment has Drive folders
        if teacher.get('google_drive_folder_id') and assignment.get('drive_folders', {}).get('submissions_folder_id'):
            try:
                from utils.google_drive import upload_student_submission
                from utils.pdf_generator import generate_submission_pdf
                
                # Generate a PDF of the submission for upload
                try:
                    submission_pdf = generate_submission_pdf(pages, submission_id)
                    if submission_pdf:
                        drive_result = upload_student_submission(
                            teacher=teacher,
                            submissions_folder_id=assignment['drive_folders']['submissions_folder_id'],
                            submission_content=submission_pdf,
                            filename=f"submission_{submission_id}.pdf",
                            student_name=student.get('name'),
                            student_id=session['student_id']
                        )
                        if drive_result:
                            Submission.update_one(
                                {'submission_id': submission_id},
                                {'$set': {'drive_file': drive_result}}
                            )
                            logger.info(f"Uploaded submission {submission_id} to Google Drive")
                except Exception as pdf_error:
                    logger.warning(f"Could not generate/upload submission PDF: {pdf_error}")
            except Exception as drive_error:
                logger.warning(f"Google Drive upload failed: {drive_error}")
        
        return jsonify({
            'success': True,
            'submission_id': submission_id,
            'redirect': url_for('view_submission', submission_id=submission_id)
        })
        
    except Exception as e:
        logger.error(f"Submission error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/student/preview-feedback', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def student_preview_feedback():
    """Get AI feedback on work without submitting (for review)"""
    from utils.ai_marking import get_preview_feedback
    
    try:
        assignment_id = request.form.get('assignment_id')
        feedback_type = request.form.get('feedback_type', 'overall')
        file_type = request.form.get('file_type', 'images')
        files = request.files.getlist('files')
        
        if not assignment_id or not files:
            return jsonify({'error': 'Missing assignment or files'}), 400
        
        assignment = Assignment.find_one({'assignment_id': assignment_id})
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        # Check if overall review is enabled for this assignment
        if not assignment.get('enable_overall_review', True):
            return jsonify({
                'error': 'Overall review is not available for this assignment.',
                'limit_reached': True
            }), 403
        
        # Check usage limit
        student_id = session['student_id']
        overall_review_limit = assignment.get('overall_review_limit', 1)
        
        # Get or create usage tracking document
        AIUsage = db.db.ai_usage
        usage = AIUsage.find_one({
            'student_id': student_id,
            'assignment_id': assignment_id
        })
        
        current_overall_review_count = usage.get('overall_review_count', 0) if usage else 0
        
        if current_overall_review_count >= overall_review_limit:
            return jsonify({
                'error': f'You have reached the maximum of {overall_review_limit} overall review(s) for this assignment.',
                'limit_reached': True,
                'used': current_overall_review_count,
                'limit': overall_review_limit
            }), 403
        
        teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
        
        # Build pages list
        pages = []
        for i, file in enumerate(files):
            file_data = file.read()
            if file.filename.lower().endswith('.pdf'):
                page_type = 'pdf'
            else:
                page_type = 'image'
            pages.append({
                'type': page_type,
                'data': file_data,
                'page_num': i + 1
            })
        
        # Get preview feedback
        feedback = get_preview_feedback(pages, assignment, feedback_type, teacher)
        
        # Increment usage count on success
        AIUsage.update_one(
            {'student_id': student_id, 'assignment_id': assignment_id},
            {
                '$inc': {'overall_review_count': 1},
                '$set': {'updated_at': datetime.utcnow()},
                '$setOnInsert': {'created_at': datetime.utcnow(), 'question_help_count': 0}
            },
            upsert=True
        )
        
        return jsonify({
            'success': True,
            'feedback': feedback,
            'usage': {
                'used': current_overall_review_count + 1,
                'limit': overall_review_limit,
                'remaining': overall_review_limit - current_overall_review_count - 1
            }
        })
        
    except Exception as e:
        logger.error(f"Preview feedback error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/student/submission/<submission_id>/file/<int:file_index>')
@login_required
def view_student_submission_file(submission_id, file_index):
    """Serve student's submission file"""
    from gridfs import GridFS
    from bson import ObjectId
    
    submission = Submission.find_one({
        'submission_id': submission_id,
        'student_id': session['student_id']
    })
    
    if not submission:
        return 'Not found', 404
    
    file_ids = submission.get('file_ids', [])
    if file_index >= len(file_ids):
        return 'File not found', 404
    
    fs = GridFS(db.db)
    try:
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

@app.route('/student/assignment/<assignment_id>/file/<file_type>')
@login_required
def download_student_assignment_file(assignment_id, file_type):
    """Download assignment file (question paper) for student"""
    from gridfs import GridFS
    from bson import ObjectId
    
    assignment = Assignment.find_one({'assignment_id': assignment_id})
    if not assignment:
        logger.error(f"Assignment not found: {assignment_id}")
        return 'Assignment not found', 404
    
    # Verify student has access (teacher is assigned to student directly or via teaching group)
    student = Student.find_one({'student_id': session['student_id']})
    teacher_ids = get_student_teacher_ids(session['student_id'])
    if assignment['teacher_id'] not in teacher_ids:
        logger.error(f"Unauthorized access attempt by {session['student_id']} to {assignment_id}")
        return 'Unauthorized', 403
    
    # Also verify student can access this specific assignment based on target class/teaching group
    if not can_student_access_assignment(student, assignment):
        logger.error(f"Student {session['student_id']} cannot access assignment {assignment_id} due to target restrictions")
        return 'Unauthorized', 403
    
    file_id_field = f"{file_type}_id"
    file_name_field = f"{file_type}_name"
    drive_ref_field = f"{file_type}_drive_id"
    
    # Check if file is from Google Drive (reference only, not copied)
    drive_file_refs = assignment.get('drive_file_refs', {})
    drive_file_id = drive_file_refs.get(drive_ref_field)
    
    if drive_file_id:
        # Fetch from Google Drive on-demand
        try:
            from utils.google_drive import get_drive_service, DriveManager
            service = get_drive_service()
            if service:
                manager = DriveManager(service)
                file_content = manager.get_file_content(drive_file_id, export_as_pdf=True)
                if file_content:
                    # Get file name from Drive
                    file_metadata = service.files().get(fileId=drive_file_id, fields="name").execute()
                    file_name = file_metadata.get('name', assignment.get(file_name_field, 'document.pdf'))
                    return Response(
                        file_content,
                        mimetype='application/pdf',
                        headers={
                            'Content-Disposition': f'inline; filename="{file_name}"'
                        }
                    )
            return 'File not found', 404
        except Exception as e:
            logger.error(f"Error fetching file from Google Drive: {e}")
            return 'File not found', 404
    
    # Otherwise, get from GridFS (uploaded file)
    if file_id_field not in assignment or not assignment[file_id_field]:
        logger.error(f"File ID field '{file_id_field}' not found in assignment {assignment_id}")
        return 'File not found', 404
    
    fs = GridFS(db.db)
    try:
        file_id = assignment[file_id_field]
        # Ensure file_id is ObjectId
        if isinstance(file_id, str):
            file_id = ObjectId(file_id)
        
        logger.info(f"Attempting to get file {file_id} from GridFS")
        file_data = fs.get(file_id)
        return Response(
            file_data.read(),
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'inline; filename="{assignment.get(file_name_field, "document.pdf")}"'
            }
        )
    except Exception as e:
        logger.error(f"Error downloading file {assignment.get(file_id_field)}: {e}")
        return 'File not found', 404

@app.route('/student/feedback/<submission_id>/pdf')
@login_required
def download_student_feedback_pdf(submission_id):
    """Download feedback PDF for student (same content as teacher review: standard or rubric)."""
    from utils.pdf_generator import generate_review_pdf, generate_rubric_review_pdf
    
    submission = Submission.find_one({
        'submission_id': submission_id,
        'student_id': session['student_id']
    })
    
    if not submission:
        return 'Not found', 404
    
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    student = Student.find_one({'student_id': session['student_id']})
    teacher = Teacher.find_one({'teacher_id': submission.get('teacher_id')})
    
    try:
        # Use rubric PDF when assignment is rubric-based so student gets same criteria + detailed corrections
        if assignment.get('marking_type') == 'rubric' or (submission.get('ai_feedback') or {}).get('criteria'):
            pdf_content = generate_rubric_review_pdf(submission, assignment, student, teacher)
        else:
            pdf_content = generate_review_pdf(submission, assignment, student, teacher)
        
        filename = f"feedback_{assignment['title']}_{student['student_id']}.pdf"
        
        return Response(
            pdf_content,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        return 'Error generating PDF', 500


# ============================================================================
# MY MODULES - STUDENT ROUTES
# ============================================================================

@app.route('/modules')
@login_required
def student_modules():
    """Student's module space - shows all available module trees."""
    if not _student_has_module_access(session['student_id']):
        return redirect(url_for('dashboard'))
    student = Student.find_one({'student_id': session['student_id']})
    teacher_ids = get_student_teacher_ids(session['student_id'])

    root_modules = list(
        Module.find({
            'teacher_id': {'$in': teacher_ids},
            'parent_id': None,
            'status': 'published',
        })
    )

    for module in root_modules:
        module['student_mastery'] = _calculate_tree_mastery(module['module_id'], session['student_id'])

    return render_template('student_modules.html', student=student, modules=root_modules)

@app.route('/modules/<module_id>')
@login_required
def student_module_view(module_id):
    """3D module visualization for student."""
    if not _student_has_module_access(session['student_id']):
        return redirect(url_for('dashboard'))
    root_module = Module.find_one({'module_id': module_id, 'status': 'published'})
    if not root_module:
        return redirect(url_for('student_modules'))

    teacher_ids = get_student_teacher_ids(session['student_id'])
    if root_module.get('teacher_id') not in teacher_ids:
        return redirect(url_for('student_modules'))

    def build_tree_with_mastery(m):
        m = dict(m)
        mastery = StudentModuleMastery.find_one({
            'student_id': session['student_id'],
            'module_id': m['module_id'],
        })
        m['mastery_score'] = mastery.get('mastery_score', 0) if mastery else 0
        m['status'] = mastery.get('status', 'not_started') if mastery else 'not_started'
        m['children'] = []
        for cid in m.get('children_ids', []):
            child = Module.find_one({'module_id': cid})
            if child:
                m['children'].append(build_tree_with_mastery(child))
        return m

    module_tree = build_tree_with_mastery(root_module)
    return render_template(
        'student_module_view.html',
        module=root_module,
        module_tree=module_tree,
        modules_json=json.dumps(module_tree, default=str),
    )

@app.route('/modules/<module_id>/learn/<node_id>')
@login_required
def learning_page(module_id, node_id):
    """Main learning page for a specific (leaf) module."""
    if not _student_has_module_access(session['student_id']):
        return redirect(url_for('dashboard'))
    module = Module.find_one({'module_id': node_id})
    root_module = Module.find_one({'module_id': module_id})
    if not module or not root_module:
        return redirect(url_for('student_modules'))

    teacher_ids = get_student_teacher_ids(session['student_id'])
    if root_module.get('teacher_id') not in teacher_ids:
        return redirect(url_for('student_modules'))

    if not module.get('is_leaf'):
        return redirect(url_for('student_module_view', module_id=module_id))

    existing_session = LearningSession.find_one({
        'student_id': session['student_id'],
        'module_id': node_id,
        'ended_at': None,
    })

    if not existing_session:
        session_doc = {
            'session_id': _generate_session_id(),
            'student_id': session['student_id'],
            'module_id': node_id,
            'started_at': datetime.utcnow(),
            'ended_at': None,
            'chat_history': [],
            'assessments': [],
            'writing_submissions': [],
            'resources_viewed': [],
        }
        LearningSession.insert_one(session_doc)
        existing_session = session_doc

    raw_resources = list(ModuleResource.find({'module_id': node_id}).sort('order', 1))
    # Build resource list with view URL for uploaded PDFs (no external url)
    resources = []
    for r in raw_resources:
        res = {
            'resource_id': r.get('resource_id'),
            'type': r.get('type', 'link'),
            'title': r.get('title', ''),
            'url': r.get('url', ''),
            'description': r.get('description', ''),
            'order': r.get('order', 0),
        }
        if res['type'] == 'pdf' and r.get('content') and not res['url']:
            res['url'] = url_for('serve_module_resource_file', resource_id=res['resource_id'])
        resources.append(res)
    mastery = StudentModuleMastery.find_one({
        'student_id': session['student_id'],
        'module_id': node_id,
    })
    profile = StudentLearningProfile.find_one({
        'student_id': session['student_id'],
        'subject': root_module.get('subject'),
    })
    overall_mastery = _calculate_tree_mastery(module_id, session['student_id'])

    return render_template(
        'learning_page.html',
        module=module,
        root_module=root_module,
        session_data=existing_session,
        resources=resources,
        mastery=mastery,
        profile=profile,
        overall_mastery=overall_mastery,
    )


@app.route('/modules/resource/<resource_id>/file')
@login_required
def serve_module_resource_file(resource_id):
    """Serve uploaded PDF content for a module resource. Student must have access to the module."""
    if not _student_has_module_access(session['student_id']):
        return 'Forbidden', 403
    res = ModuleResource.find_one({'resource_id': resource_id})
    if not res or res.get('type') != 'pdf' or not res.get('content'):
        return 'Not found', 404
    module = Module.find_one({'module_id': res['module_id']})
    if not module:
        return 'Not found', 404
    root_id = module.get('parent_id') or res['module_id']
    root_module = Module.find_one({'module_id': root_id})
    if not root_module or root_module.get('status') != 'published':
        return 'Not found', 404
    teacher_ids = get_student_teacher_ids(session['student_id'])
    if root_module.get('teacher_id') not in teacher_ids:
        return 'Forbidden', 403
    try:
        pdf_bytes = base64.b64decode(res['content'])
    except Exception:
        return 'Invalid resource', 500
    from flask import Response
    return Response(pdf_bytes, mimetype='application/pdf', headers={
        'Content-Disposition': 'inline; filename="%s.pdf"' % (res.get('title', 'resource') or 'resource',),
    })


# ============================================================================
# PYTHON LAB - In-browser Python interpreter (classes/teaching groups only)
# ============================================================================

PYTHON_EXECUTE_TIMEOUT = 5
PYTHON_OUTPUT_MAX_BYTES = 100000

@app.route('/python-lab')
@login_required
def student_python_lab():
    """Python Lab page: write and run Python code. Access controlled by admin (classes/teaching groups)."""
    if not _student_has_python_lab_access(session['student_id']):
        return redirect(url_for('dashboard'))
    return render_template('python_lab.html', execute_timeout=PYTHON_EXECUTE_TIMEOUT)


@app.route('/teacher/python-lab')
@teacher_required
def teacher_python_lab():
    """Python Lab page for teachers. Access controlled by admin (teachers allowed list)."""
    if not _teacher_has_python_lab_access(session['teacher_id']):
        return redirect(url_for('teacher_dashboard'))
    return render_template('python_lab_teacher.html', execute_timeout=PYTHON_EXECUTE_TIMEOUT)


@app.route('/api/python/execute', methods=['POST'])
@student_or_teacher_required
def student_python_execute():
    """Execute Python code server-side. For students or teachers with Python Lab access."""
    if session.get('student_id'):
        if not _student_has_python_lab_access(session['student_id']):
            return jsonify({'error': 'Access denied'}), 403
    elif session.get('teacher_id'):
        if not _teacher_has_python_lab_access(session['teacher_id']):
            return jsonify({'error': 'Access denied'}), 403
    else:
        return jsonify({'error': 'Access denied'}), 403
    try:
        data = request.get_json() or {}
        code = (data.get('code') or '').strip()
        if not code:
            return jsonify({'error': 'No code provided'}), 400
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [os.environ.get('PYTHON_EXECUTABLE', 'python3'), '-c', code],
                capture_output=True,
                timeout=PYTHON_EXECUTE_TIMEOUT,
                cwd=tmpdir,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
            )
        out = (result.stdout or b'').decode('utf-8', errors='replace')
        err = (result.stderr or b'').decode('utf-8', errors='replace')
        if len(out) > PYTHON_OUTPUT_MAX_BYTES:
            out = out[:PYTHON_OUTPUT_MAX_BYTES] + '\n... (output truncated)'
        if len(err) > PYTHON_OUTPUT_MAX_BYTES:
            err = err[:PYTHON_OUTPUT_MAX_BYTES] + '\n... (output truncated)'
        combined = (out + (('\n' + err) if err else '')).strip() or '(no output)'
        return jsonify({'output': combined, 'executed': True})
    except subprocess.TimeoutExpired:
        return jsonify({'output': 'Execution timed out (max %s seconds).' % PYTHON_EXECUTE_TIMEOUT, 'executed': False})
    except Exception as e:
        logger.exception("Python execute error")
        return jsonify({'output': str(e), 'executed': False}), 500


@app.route('/api/learning/chat', methods=['POST'])
@login_required
def learning_chat():
    """Handle chat messages in learning session."""
    if not _student_has_module_access(session['student_id']):
        return jsonify({'error': 'Access denied'}), 403
    try:
        data = request.get_json() or {}
        module_id = data.get('module_id')
        message = (data.get('message') or '').strip()
        session_id = data.get('session_id')
        writing_image = data.get('writing_image')

        if not message and not writing_image:
            return jsonify({'error': 'No message or image provided'}), 400

        module = Module.find_one({'module_id': module_id})
        if not module:
            return jsonify({'error': 'Module not found'}), 404

        root_module = Module.find_one({'module_id': module.get('parent_id') or module_id})
        if not root_module:
            root_module = module

        learning_session = LearningSession.find_one({'session_id': session_id})
        chat_history = learning_session.get('chat_history', []) if learning_session else []

        profile = StudentLearningProfile.find_one({
            'student_id': session['student_id'],
            'subject': root_module.get('subject'),
        })

        writing_bytes = None
        if writing_image:
            if ',' in writing_image:
                writing_image = writing_image.split(',')[1]
            writing_bytes = base64.b64decode(writing_image)

        # Prefer Agno agent when available (tools: pull resources, generate quiz)
        try:
            from utils.agno_learning_agent import get_learning_agent
            agent = get_learning_agent()
        except Exception:
            agent = None

        if agent:
            root_module_id = root_module.get('module_id')
            textbook_context = None
            if root_module_id:
                rag_result = rag_service.query_textbook(root_module_id, message or module.get('title', ''))
                if rag_result.get('success') and rag_result.get('chunks'):
                    textbook_context = "\n\n---\n\n".join(
                        c.get('content', '') for c in rag_result['chunks'] if c.get('content')
                    )
            result = agent.chat(
                message=message,
                student_id=session['student_id'],
                module=module,
                subject=root_module.get('subject', ''),
                student_profile=profile,
                chat_history=chat_history,
                image_data=writing_bytes,
                root_module_id=root_module_id,
                textbook_context=textbook_context,
            )
            if not result.get('success') and 'error' in result and 'response' not in result:
                return jsonify({'error': result.get('error', 'Agent error')}), 500
            # Propagate mastery to parent when agent called update_student_mastery
            for tc in result.get('tool_calls', []):
                if tc.get('name') == 'update_student_mastery':
                    args = tc.get('arguments') or {}
                    mid = args.get('module_id')
                    if mid and module.get('parent_id'):
                        _propagate_mastery_to_parent(session['student_id'], module['parent_id'])
                    break
            new_messages = [
                {'role': 'student', 'content': message, 'timestamp': datetime.utcnow().isoformat()},
            ]
            if writing_bytes:
                new_messages[0]['has_image'] = True
            new_messages.append({
                'role': 'assistant',
                'content': result.get('response', ''),
                'timestamp': datetime.utcnow().isoformat(),
            })
            LearningSession.update_one(
                {'session_id': session_id},
                {
                    '$push': {'chat_history': {'$each': new_messages}},
                    '$set': {'last_activity': datetime.utcnow()},
                },
            )
            return jsonify({
                'response': result.get('response', ''),
                'response_type': 'teaching',
                'tool_calls': result.get('tool_calls', []),
                'mastery_updated': any(
                    tc.get('name') == 'update_student_mastery'
                    for tc in result.get('tool_calls', [])
                ),
            })

        # Fallback: raw Claude (no tools)
        root_module_id = root_module.get('module_id')
        textbook_context = None
        if root_module_id:
            rag_result = rag_service.query_textbook(root_module_id, message or module.get('title', ''))
            if rag_result.get('success') and rag_result.get('chunks'):
                textbook_context = "\n\n---\n\n".join(
                    c.get('content', '') for c in rag_result['chunks'] if c.get('content')
                )
        result = assess_student_understanding(
            student_message=message,
            module=module,
            chat_history=chat_history,
            student_profile=profile,
            writing_image=writing_bytes,
            textbook_context=textbook_context,
        )

        if 'error' in result and 'response' not in result:
            return jsonify({'error': result['error']}), 500

        new_messages = [
            {'role': 'student', 'content': message, 'timestamp': datetime.utcnow().isoformat()},
        ]
        if writing_bytes:
            new_messages[0]['has_image'] = True
        new_messages.append({
            'role': 'assistant',
            'content': result.get('response', ''),
            'timestamp': datetime.utcnow().isoformat(),
        })

        LearningSession.update_one(
            {'session_id': session_id},
            {
                '$push': {'chat_history': {'$each': new_messages}},
                '$set': {'last_activity': datetime.utcnow()},
            },
        )

        if result.get('assessment') and result['assessment'].get('mastery_change'):
            _update_student_mastery(
                session['student_id'],
                module_id,
                result['assessment']['mastery_change'],
            )
        if result.get('profile_updates'):
            _update_student_profile(
                session['student_id'],
                root_module.get('subject'),
                result['profile_updates'],
            )

        return jsonify({
            'response': result.get('response', ''),
            'response_type': result.get('response_type', 'teaching'),
            'assessment': result.get('assessment'),
            'interactive': result.get('interactive_element'),
            'tool_calls': [],
            'mastery_updated': bool(result.get('assessment', {}).get('mastery_change')),
        })
    except Exception as e:
        logger.error("Error in learning chat: %s", e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/learning/submit_writing', methods=['POST'])
@login_required
def submit_writing():
    """Submit handwritten work for analysis."""
    if not _student_has_module_access(session['student_id']):
        return jsonify({'error': 'Access denied'}), 403
    try:
        data = request.get_json() or {}
        module_id = data.get('module_id')
        session_id = data.get('session_id')
        image_data = data.get('image')
        expected_content = data.get('expected_content', '')

        if not image_data:
            return jsonify({'error': 'No image provided'}), 400

        module = Module.find_one({'module_id': module_id})
        if not module:
            return jsonify({'error': 'Module not found'}), 404

        if ',' in image_data:
            image_data = image_data.split(',')[1]
        image_bytes = base64.b64decode(image_data)

        result = analyze_writing_submission(
            image_data=image_bytes,
            module=module,
            expected_content=expected_content,
        )

        LearningSession.update_one(
            {'session_id': session_id},
            {
                '$push': {
                    'writing_submissions': {
                        'image_data': (image_data[:100] + '...') if len(image_data) > 100 else image_data,
                        'ai_analysis': result,
                        'timestamp': datetime.utcnow().isoformat(),
                    }
                }
            },
        )

        if result.get('mastery_indication') is not None:
            change = (result['mastery_indication'] - 50) / 10
            _update_student_mastery(session['student_id'], module_id, change)

        return jsonify(result)
    except Exception as e:
        logger.error("Error analyzing writing: %s", e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/learning/resource_viewed', methods=['POST'])
@login_required
def mark_resource_viewed():
    """Mark a resource as viewed and update progress."""
    if not _student_has_module_access(session['student_id']):
        return jsonify({'error': 'Access denied'}), 403
    try:
        data = request.get_json() or {}
        resource_id = data.get('resource_id')
        session_id = data.get('session_id')

        LearningSession.update_one(
            {'session_id': session_id},
            {'$addToSet': {'resources_viewed': resource_id}},
        )

        resource = ModuleResource.find_one({'resource_id': resource_id})
        if resource:
            _update_student_mastery(session['student_id'], resource['module_id'], 2)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
    
    # Pending Review = waiting for teacher (exclude AI feedback already sent)
    pending_submissions = Submission.count({
        'assignment_id': {'$in': assignment_ids},
        '$or': [
            {'status': 'submitted'},
            {'status': 'ai_reviewed', 'feedback_sent': {'$ne': True}}
        ]
    })
    
    approved_submissions = Submission.count({
        'assignment_id': {'$in': assignment_ids},
        'status': 'approved'
    })
    
    # Get recent pending submissions (exclude AI feedback sent)
    recent_pending = list(Submission.find({
        'assignment_id': {'$in': assignment_ids},
        '$or': [
            {'status': 'submitted'},
            {'status': 'ai_reviewed', 'feedback_sent': {'$ne': True}}
        ]
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
    
    # Get teacher's classes with students
    # Include: classes in teacher profile, students assigned to teacher, AND students with submissions
    teacher_classes = set(teacher.get('classes', []))
    teacher_student_ids = set()
    
    # Find students who have this teacher assigned
    students_with_teacher = list(Student.find({'teachers': session['teacher_id']}))
    for student in students_with_teacher:
        teacher_student_ids.add(student.get('student_id'))
        if student.get('class'):
            teacher_classes.add(student.get('class'))
    
    # Also find students who have submissions to this teacher's assignments
    submission_student_ids = Submission.find({
        'assignment_id': {'$in': assignment_ids}
    }).distinct('student_id')
    
    for student_id in submission_student_ids:
        teacher_student_ids.add(student_id)
        student = Student.find_one({'student_id': student_id})
        if student and student.get('class'):
            teacher_classes.add(student.get('class'))
    
    classes_data = []
    for class_id in sorted(teacher_classes):
        class_info = Class.find_one({'class_id': class_id}) or {'class_id': class_id}
        # Get students in this class who are either assigned to teacher OR have submissions
        students = list(Student.find({
            'class': class_id,
            'student_id': {'$in': list(teacher_student_ids)}
        }).sort('name', 1))
        if students:  # Only add class if it has relevant students
            classes_data.append({
                'class_id': class_id,
                'name': class_info.get('name', class_id),
                'students': students,
                'student_count': len(students)
            })
    
    # Get teacher's teaching groups with students
    teaching_groups = list(TeachingGroup.find({'teacher_id': session['teacher_id']}))
    for group in teaching_groups:
        students = list(Student.find({'student_id': {'$in': group.get('student_ids', [])}}))
        group['students'] = students
        group['student_count'] = len(students)
    
    return render_template('teacher_dashboard.html',
                         teacher=teacher,
                         stats={
                             'total_assignments': len(assignments),
                             'assignments': len(assignments),
                             'pending_submissions': pending_submissions,
                             'approved_submissions': approved_submissions,
                             'unread_messages': unread_messages
                         },
                         assignments=assignments,
                         recent_pending=recent_pending,
                         students_with_messages=students_with_messages,
                         classes=classes_data,
                         teaching_groups=teaching_groups)

@app.route('/teacher/class/<class_id>')
@teacher_required
def view_class(class_id):
    """View a specific class with students and assignment status"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    # Get teacher's assignments assigned to this specific class
    assignments = list(Assignment.find({
        'teacher_id': session['teacher_id'],
        'status': 'published',
        'target_type': 'class',
        'target_class_id': class_id
    }).sort('created_at', -1))
    assignment_ids = [a['assignment_id'] for a in assignments]
    
    # Find all students relevant to this teacher in this class:
    # 1. Students assigned to this teacher
    # 2. Students with submissions to this teacher's assignments
    teacher_student_ids = set()
    
    # Students assigned to teacher
    assigned_students = Student.find({'class': class_id, 'teachers': session['teacher_id']})
    for s in assigned_students:
        teacher_student_ids.add(s['student_id'])
    
    # Students with submissions
    submission_student_ids = Submission.find({
        'assignment_id': {'$in': assignment_ids}
    }).distinct('student_id')
    
    for student_id in submission_student_ids:
        student = Student.find_one({'student_id': student_id, 'class': class_id})
        if student:
            teacher_student_ids.add(student_id)
    
    # Get all relevant students
    students = list(Student.find({
        'class': class_id,
        'student_id': {'$in': list(teacher_student_ids)}
    }).sort('name', 1))
    
    # Verify teacher has students in this class
    if not students and class_id not in teacher.get('classes', []):
        return redirect(url_for('teacher_dashboard'))
    
    # Get class info
    class_info = Class.find_one({'class_id': class_id}) or {'class_id': class_id}
    
    # Get teaching groups from this class
    teaching_groups = list(TeachingGroup.find({
        'teacher_id': session['teacher_id'],
        'class_id': class_id
    }))
    
    title = class_id
    subtitle = class_info.get('name') if class_info.get('name') and class_info.get('name') != class_id else None
    
    return render_template('teacher_class_view.html',
                         teacher=teacher,
                         title=title,
                         subtitle=subtitle,
                         students=students,
                         assignments=assignments,
                         teaching_groups=teaching_groups,
                         is_teaching_group=False)

@app.route('/teacher/group/<group_id>')
@teacher_required
def view_teaching_group(group_id):
    """View a specific teaching group with students and assignment status"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    # Get teaching group
    group = TeachingGroup.find_one({
        'group_id': group_id,
        'teacher_id': session['teacher_id']
    })
    
    if not group:
        return redirect(url_for('teacher_dashboard'))
    
    # Get students in this group
    students = list(Student.find({
        'student_id': {'$in': group.get('student_ids', [])}
    }).sort('name', 1))
    
    # Get teacher's assignments assigned to this specific teaching group
    assignments = list(Assignment.find({
        'teacher_id': session['teacher_id'],
        'status': 'published',
        'target_type': 'teaching_group',
        'target_group_id': group_id
    }).sort('created_at', -1))
    
    title = group.get('name', group_id)
    subtitle = f"Teaching Group from {group.get('class_id', 'Unknown Class')}"
    
    return render_template('teacher_class_view.html',
                         teacher=teacher,
                         title=title,
                         subtitle=subtitle,
                         students=students,
                         assignments=assignments,
                         teaching_groups=[],
                         is_teaching_group=True)

@app.route('/teacher/assignments')
@teacher_required
def teacher_assignments():
    """List teacher's assignments"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    assignments = list(Assignment.find({
        'teacher_id': session['teacher_id']
    }).sort('created_at', -1))
    
    # Add submission counts and target info
    for a in assignments:
        a['submission_count'] = Submission.count({'assignment_id': a['assignment_id']})
        a['pending_count'] = Submission.count({
            'assignment_id': a['assignment_id'],
            'status': {'$in': ['submitted', 'ai_reviewed']}
        })
        
        # Add target display name
        target_type = a.get('target_type', 'class')
        if target_type == 'teaching_group' and a.get('target_group_id'):
            group = TeachingGroup.find_one({'group_id': a['target_group_id']})
            a['target_display'] = group.get('name', a['target_group_id']) if group else 'Unknown Group'
            a['target_icon'] = 'diagram-3'
        elif a.get('target_class_id'):
            a['target_display'] = a['target_class_id']
            a['target_icon'] = 'collection'
        else:
            a['target_display'] = None
    
    return render_template('teacher_assignments.html',
                         teacher=teacher,
                         assignments=assignments)

@app.route('/teacher/api/student-statuses')
@teacher_required
def get_student_statuses():
    """Get submission statuses for students by assignment"""
    try:
        assignment_id = request.args.get('assignment_id')
        student_ids = request.args.getlist('student_ids[]')
        
        if not assignment_id or not student_ids:
            return jsonify({'success': False, 'error': 'Missing parameters'}), 400
        
        # Verify teacher owns this assignment
        assignment = Assignment.find_one({
            'assignment_id': assignment_id,
            'teacher_id': session['teacher_id']
        })
        
        if not assignment:
            return jsonify({'success': False, 'error': 'Assignment not found'}), 404
        
        # Get latest submission per student (rubric may have multiple versions)
        statuses = {}
        for student_id in student_ids:
            submission = Submission.find_one(
                {
                    'assignment_id': assignment_id,
                    'student_id': student_id
                },
                sort=[('submitted_at', -1), ('created_at', -1)]
            )
            
            if submission:
                status = submission.get('status', 'submitted')
                sub_id = submission.get('submission_id')
                feedback_sent = submission.get('feedback_sent', False)
                # AI Feedback Sent: AI reviewed and feedback already sent to student
                if status == 'ai_reviewed' and feedback_sent:
                    statuses[student_id] = {'status': 'ai_feedback_sent', 'label': 'AI Feedback Sent', 'class': 'info', 'submission_id': sub_id}
                # Pending Review: waiting for teacher (submitted or ai_reviewed but not sent)
                elif status in ['submitted', 'ai_reviewed']:
                    statuses[student_id] = {'status': 'pending', 'label': 'Pending Review', 'class': 'warning', 'submission_id': sub_id}
                # Returned: teacher has reviewed and returned
                elif status in ['reviewed', 'approved']:
                    statuses[student_id] = {'status': 'returned', 'label': 'Returned', 'class': 'success', 'submission_id': sub_id}
                else:
                    statuses[student_id] = {'status': status, 'label': status.title(), 'class': 'secondary', 'submission_id': sub_id}
            else:
                statuses[student_id] = {'status': 'none', 'label': 'No Submission', 'class': 'light', 'submission_id': None}
        
        return jsonify({'success': True, 'statuses': statuses})
    except Exception as e:
        logger.error(f"Error in get_student_statuses: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Server error loading statuses'}), 500

@app.route('/teacher/assignment/<assignment_id>/summary')
@teacher_required
def assignment_summary(assignment_id):
    """View detailed assignment summary with class statistics"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    
    if not assignment:
        return redirect(url_for('teacher_assignments'))
    
    # Get students based on the assignment's target type (class or teaching group)
    target_type = assignment.get('target_type', 'class')
    target_class_id = assignment.get('target_class_id')
    target_group_id = assignment.get('target_group_id')
    
    if target_type == 'teaching_group' and target_group_id:
        # Get students from the specific teaching group
        teaching_group = TeachingGroup.find_one({'group_id': target_group_id})
        if teaching_group:
            student_ids = teaching_group.get('student_ids', [])
            all_students = list(Student.find({'student_id': {'$in': student_ids}}))
        else:
            all_students = []
    elif target_type == 'class' and target_class_id:
        # Get students from the specific class who are also assigned to this teacher
        all_students = list(Student.find({
            'class': target_class_id,
            'teachers': session['teacher_id']
        }))
    else:
        # Fallback: get all students assigned to this teacher
        all_students = list(Student.find({'teachers': session['teacher_id']}))
    
    total_students = len(all_students)
    
    # Get all submissions for this assignment
    submissions = list(Submission.find({
        'assignment_id': assignment_id,
        'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
    }))
    
    submission_map = {s['student_id']: s for s in submissions}
    
    total_marks = float(assignment.get('total_marks', 100) or 100)
    
    # Calculate statistics (use AI-derived marks when final_marks not yet set)
    reviewed_count = len([s for s in submissions if s['status'] == 'reviewed'])
    pending_count = len(submissions) - reviewed_count
    
    scores = []
    for s in submissions:
        display_marks, _ = _submission_display_marks(s, total_marks)
        if display_marks is not None:
            scores.append(display_marks)
    
    avg_marks = sum(scores) / len(scores) if scores else 0
    avg_score = (avg_marks / total_marks * 100) if total_marks > 0 else 0
    pass_count = len([s for s in scores if s >= total_marks * 0.5])
    pass_rate = (pass_count / len(scores) * 100) if scores else 0
    
    # For no-marks mode (standard only): correct (100%), incorrect (0%), partial (else)
    # Use same percentage as student rows so summary counts match the table.
    award_marks = assignment.get('award_marks', True)
    no_marks_mode = assignment.get('marking_type') == 'standard' and not award_marks
    correct_count = partial_count = incorrect_count = 0
    if no_marks_mode:
        # Use a denominator when total_marks is 0 so we can still classify from final_marks
        denom = total_marks if total_marks > 0 else 100.0
        for s in submissions:
            _, pct = _submission_display_marks(s, denom)
            if pct >= 99.9:
                correct_count += 1
            elif pct <= 0.01:
                incorrect_count += 1
            else:
                partial_count += 1
    
    stats = {
        'total_students': total_students,
        'submitted': len(submissions),
        'reviewed': reviewed_count,
        'pending': pending_count,
        'avg_marks': avg_marks,
        'avg_score': avg_score,
        'pass_rate': round(pass_rate),
        'correct_count': correct_count,
        'partial_count': partial_count + incorrect_count,
        'incorrect_count': incorrect_count
    }
    
    # Score distribution
    score_distribution = {
        'A (80-100%)': {'count': 0, 'percentage': 0, 'color': 'bg-success'},
        'B (60-79%)': {'count': 0, 'percentage': 0, 'color': 'bg-info'},
        'C (40-59%)': {'count': 0, 'percentage': 0, 'color': 'bg-warning'},
        'D (0-39%)': {'count': 0, 'percentage': 0, 'color': 'bg-danger'}
    }
    
    for score in scores:
        pct = (score / total_marks * 100) if total_marks > 0 else 0
        if pct >= 80:
            score_distribution['A (80-100%)']['count'] += 1
        elif pct >= 60:
            score_distribution['B (60-79%)']['count'] += 1
        elif pct >= 40:
            score_distribution['C (40-59%)']['count'] += 1
        else:
            score_distribution['D (0-39%)']['count'] += 1
    
    if scores:
        for grade in score_distribution:
            score_distribution[grade]['percentage'] = round(score_distribution[grade]['count'] / len(scores) * 100)
    
    # Analyze class insights from AI feedback
    insights = analyze_class_insights(submissions)
    
    # Build student submission list (use AI-derived marks when final_marks not yet set)
    student_submissions = []
    for student in sorted(all_students, key=lambda x: x.get('name', '')):
        sub = submission_map.get(student['student_id'])
        status = sub['status'] if sub else 'not_submitted'
        feedback_sent = sub.get('feedback_sent', False) if sub else False
        display_status = 'ai_feedback_sent' if (status == 'ai_reviewed' and feedback_sent) else ('pending' if status in ('submitted', 'ai_reviewed') else status)
        display_marks, percentage = _submission_display_marks(sub, total_marks) if sub else (None, 0)
        
        student_submissions.append({
            'student': student,
            'submission': sub,
            'status': status,
            'display_status': display_status,
            'percentage': percentage,
            'display_marks': display_marks
        })
    
    # Sort: pending first, then ai_feedback_sent, then reviewed, then not_submitted; within group by score descending
    def _sort_order(item):
        ds = item.get('display_status', item.get('status'))
        if ds == 'pending':
            return (0, -item['percentage'])
        if ds == 'ai_feedback_sent':
            return (1, -item['percentage'])
        if ds in ('reviewed', 'approved'):
            return (2, -item['percentage'])
        return (3, -item['percentage'])
    student_submissions.sort(key=_sort_order)
    
    return render_template('teacher_assignment_summary.html',
                         teacher=teacher,
                         assignment=assignment,
                         stats=stats,
                         score_distribution=score_distribution,
                         insights=insights,
                         student_submissions=student_submissions)


def _build_feedback_summary_report(assignment, submissions, student_submissions_list, insights):
    """Build extended report: topics to revisit, students needing attention, support approaches, heatmap data."""
    total_marks = float(assignment.get('total_marks', 100) or 100)
    
    # Topics to go through again (from insights improvements)
    topics_to_revisit = []
    for imp in insights.get('improvements', []):
        topics_to_revisit.append({
            'question': imp['question'],
            'description': f"Q{imp['question']}: {imp['incorrect']}/{imp['total']} need improvement ({imp['percentage']}%)",
            'percentage': imp['percentage'],
            'common_wrong': imp.get('common_wrong', [])
        })
    
    # Students needing particular attention (low scorers, or many wrong)
    students_needing_attention = []
    for item in student_submissions_list:
        student, sub, status, percentage = item['student'], item.get('submission'), item['status'], item['percentage']
        if sub and percentage is not None and (percentage < 50 or (status in ('submitted', 'ai_reviewed') and percentage < 70)):
            students_needing_attention.append({
                'student': student,
                'percentage': percentage,
                'final_marks': item.get('submission', {}).get('final_marks'),
                'total_marks': total_marks,
                'status': status
            })
    students_needing_attention.sort(key=lambda x: x['percentage'] or 0)
    
    # Support approaches (recommendations + targeted suggestions)
    support_approaches = list(insights.get('recommendations', []))
    if students_needing_attention:
        support_approaches.append("Consider one-to-one or small-group support for students scoring below 50%.")
    if topics_to_revisit:
        support_approaches.append("Re-teach or recap topics for questions with high error rates before moving on.")
    if insights.get('misconceptions'):
        support_approaches.append("Address common misconceptions in class discussion or targeted practice.")
    
    # Heatmap: question labels from submissions' AI feedback
    question_nums = set()
    for sub in submissions:
        for q in (sub.get('ai_feedback') or {}).get('questions', []):
            question_nums.add(q.get('question_num'))
    for sub in submissions:
        for q_num in (sub.get('teacher_feedback') or {}).get('questions', {}).keys():
            try:
                question_nums.add(int(q_num) if isinstance(q_num, str) and q_num.isdigit() else q_num)
            except (ValueError, TypeError):
                question_nums.add(q_num)
    if not question_nums and assignment.get('questions'):
        question_nums = set(range(1, len(assignment.get('questions', [])) + 1))
    question_labels = sorted(question_nums, key=lambda x: (isinstance(x, int), x))
    
    def _get_question_marks(sub, q_num):
        tf = sub.get('teacher_feedback') or {}
        af = sub.get('ai_feedback') or {}
        q_map = tf.get('questions', {}) or af.get('questions', [])
        if isinstance(q_map, list):
            for q in q_map:
                if q.get('question_num') == q_num:
                    m = q.get('marks_awarded') or q.get('marks')
                    mt = q.get('marks_total') or q.get('marks_total')
                    if m is not None and mt:
                        try:
                            return float(m), float(mt)
                        except (ValueError, TypeError):
                            pass
                    return None, None
        else:
            q_data = q_map.get(str(q_num)) or q_map.get(q_num)
            if q_data:
                m = q_data.get('marks') or q_data.get('marks_awarded')
                mt = q_data.get('marks_total')
                if m is not None:
                    try:
                        return float(m), float(mt) or 1.0
                    except (ValueError, TypeError):
                        pass
        return None, None
    
    heatmap_rows = []
    for item in sorted(student_submissions_list, key=lambda x: (x['student'].get('name', ''))):
        student = item['student']
        sub = item.get('submission')
        row_cells = []
        for q_num in question_labels:
            pct_val = None
            css = 'heatmap-missing'
            label = '—'
            if sub:
                m, mt = _get_question_marks(sub, q_num)
                if m is not None and mt and mt > 0:
                    pct_val = (m / mt) * 100
                    if pct_val >= 100:
                        css = 'heatmap-full'
                        label = '100%'
                    elif pct_val >= 50:
                        css = 'heatmap-partial'
                        label = f'{int(pct_val)}%'
                    else:
                        css = 'heatmap-low'
                        label = f'{int(pct_val)}%'
            row_cells.append({'pct': pct_val, 'label': label, 'css': css})
        heatmap_rows.append({
            'student': student,
            'submission': sub,
            'cells': row_cells,
            'percentage': item.get('percentage'),
            'final_marks': item.get('submission', {}).get('final_marks') if item.get('submission') else None
        })
    
    return {
        'topics_to_revisit': topics_to_revisit,
        'students_needing_attention': students_needing_attention,
        'support_approaches': support_approaches,
        'heatmap_question_labels': question_labels,
        'heatmap_rows': heatmap_rows,
    }


@app.route('/teacher/assignment/<assignment_id>/feedback-summary-report')
@teacher_required
def feedback_summary_report(assignment_id):
    """Generate feedback and summary report: topics to revisit, students needing attention, support approaches, heatmap."""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    if not assignment:
        return redirect(url_for('teacher_assignments'))
    
    all_students = _get_students_for_assignment(assignment, session['teacher_id'])
    submissions = list(Submission.find({
        'assignment_id': assignment_id,
        'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
    }))
    submission_map = {s['student_id']: s for s in submissions}
    total_marks = float(assignment.get('total_marks', 100) or 100)
    student_submissions_list = []
    for student in sorted(all_students, key=lambda x: x.get('name', '')):
        sub = submission_map.get(student['student_id'])
        pct = 0
        if sub and sub.get('final_marks') is not None:
            try:
                pct = (float(sub['final_marks']) / total_marks * 100) if total_marks > 0 else 0
            except (ValueError, TypeError):
                pass
        student_submissions_list.append({
            'student': student,
            'submission': sub,
            'status': sub.get('status') if sub else 'not_submitted',
            'percentage': pct
        })
    
    insights = analyze_class_insights(submissions)
    report = _build_feedback_summary_report(assignment, submissions, student_submissions_list, insights)
    
    return render_template('teacher_feedback_summary_report.html',
                         teacher=teacher,
                         assignment=assignment,
                         report=report,
                         stats={'total_students': len(all_students), 'submitted': len(submissions)})


@app.route('/teacher/assignment/<assignment_id>/heatmap-pdf')
@teacher_required
def download_heatmap_pdf(assignment_id):
    """Download heatmap and summary report as PDF."""
    from utils.pdf_generator import generate_heatmap_pdf
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    if not assignment:
        return 'Assignment not found', 404
    all_students = _get_students_for_assignment(assignment, session['teacher_id'])
    submissions = list(Submission.find({
        'assignment_id': assignment_id,
        'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
    }))
    submission_map = {s['student_id']: s for s in submissions}
    total_marks = float(assignment.get('total_marks', 100) or 100)
    student_submissions_list = []
    for student in sorted(all_students, key=lambda x: x.get('name', '')):
        sub = submission_map.get(student['student_id'])
        pct = 0
        if sub and sub.get('final_marks') is not None:
            try:
                pct = (float(sub['final_marks']) / total_marks * 100) if total_marks > 0 else 0
            except (ValueError, TypeError):
                pass
        student_submissions_list.append({
            'student': student,
            'submission': sub,
            'status': sub.get('status') if sub else 'not_submitted',
            'percentage': pct
        })
    insights = analyze_class_insights(submissions)
    report = _build_feedback_summary_report(assignment, submissions, student_submissions_list, insights)
    try:
        pdf_content = generate_heatmap_pdf(assignment, report, teacher=Teacher.find_one({'teacher_id': session['teacher_id']}))
        safe_title = (assignment.get('title') or 'report').replace(' ', '_')[:30]
        return Response(
            pdf_content,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="feedback_summary_heatmap_{safe_title}.pdf"'}
        )
    except Exception as e:
        logger.error(f"Error generating heatmap PDF: {e}")
        return str(e), 500


def _submission_display_marks(submission, total_possible):
    """
    Return (marks_float or None, percentage) for display.
    Uses final_marks if set; otherwise derives from ai_feedback for ai_reviewed submissions.
    """
    if not submission or total_possible <= 0:
        return None, 0
    fm = submission.get('final_marks')
    if fm is not None:
        try:
            m = float(fm)
            pct = (m / total_possible * 100) if total_possible > 0 else 0
            return m, pct
        except (ValueError, TypeError):
            pass
    af = submission.get('ai_feedback') or {}
    if submission.get('status') != 'ai_reviewed' or not af:
        return None, 0
    # Derive from AI feedback: rubric has criteria, standard has questions
    total = af.get('total_marks')
    if total is not None:
        try:
            m = float(total)
            pct = (m / total_possible * 100) if total_possible > 0 else 0
            return m, pct
        except (ValueError, TypeError):
            pass
    total = sum(c.get('marks_awarded', 0) for c in af.get('criteria', []))
    if total == 0:
        total = sum(q.get('marks_awarded', 0) for q in af.get('questions', []))
    try:
        m = float(total)
        pct = (m / total_possible * 100) if total_possible > 0 else 0
        return m, pct
    except (ValueError, TypeError):
        return None, 0


def _get_students_for_assignment(assignment, teacher_id):
    """Get list of students for an assignment (class or teaching group)."""
    target_type = assignment.get('target_type', 'class')
    target_class_id = assignment.get('target_class_id')
    target_group_id = assignment.get('target_group_id')
    if target_type == 'teaching_group' and target_group_id:
        teaching_group = TeachingGroup.find_one({'group_id': target_group_id})
        if teaching_group:
            student_ids = teaching_group.get('student_ids', [])
            return list(Student.find({'student_id': {'$in': student_ids}}))
        return []
    if target_type == 'class' and target_class_id:
        return list(Student.find({
            'class': target_class_id,
            'teachers': teacher_id
        }))
    return list(Student.find({'teachers': teacher_id}))


def _get_teacher_accessible_student_ids(teacher_id):
    """Get set of student_ids the teacher can access (for search, reset password, etc.)."""
    teacher = Teacher.find_one({'teacher_id': teacher_id})
    if not teacher:
        return set()
    teacher_classes = set(teacher.get('classes', []))
    teacher_student_ids = set()
    for student in Student.find({'teachers': teacher_id}):
        teacher_student_ids.add(student.get('student_id'))
        if student.get('class'):
            teacher_classes.add(student.get('class'))
    assignment_ids = [a['assignment_id'] for a in Assignment.find({'teacher_id': teacher_id})]
    if assignment_ids:
        for student_id in Submission.find({'assignment_id': {'$in': assignment_ids}}).distinct('student_id'):
            teacher_student_ids.add(student_id)
            s = Student.find_one({'student_id': student_id})
            if s and s.get('class'):
                teacher_classes.add(s.get('class'))
    for group in TeachingGroup.find({'teacher_id': teacher_id}):
        teacher_student_ids.update(group.get('student_ids', []))
    for student in Student.find({'$or': [{'class': {'$in': list(teacher_classes)}}, {'classes': {'$in': list(teacher_classes)}}]}):
        teacher_student_ids.add(student.get('student_id'))
    return teacher_student_ids


def _assignments_same_class_or_group(assignment, teacher_id):
    """Return assignments that target the same class or teaching group (for dropdown)."""
    target_type = assignment.get('target_type', 'class')
    target_class_id = assignment.get('target_class_id')
    target_group_id = assignment.get('target_group_id')
    if target_type == 'teaching_group' and target_group_id:
        return list(Assignment.find({
            'teacher_id': teacher_id,
            'target_type': 'teaching_group',
            'target_group_id': target_group_id
        }))
    if target_type == 'class' and target_class_id:
        return list(Assignment.find({
            'teacher_id': teacher_id,
            'target_type': 'class',
            'target_class_id': target_class_id
        }))
    # Fallback: only this assignment
    return [assignment]

@app.route('/teacher/assignment/<assignment_id>/manual-submission', methods=['GET', 'POST'])
@teacher_required
def manual_submission(assignment_id):
    """Record a manual (hard copy) submission: teacher selects student and uploads PDF or photos."""
    from gridfs import GridFS
    from utils.ai_marking import analyze_submission_images, analyze_essay_with_rubrics
    
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    if not assignment:
        return redirect(url_for('teacher_assignments'))
    
    all_students = _get_students_for_assignment(assignment, session['teacher_id'])
    
    if request.method == 'GET':
        # Show which students have a submission (any status) so we can label "already submitted (you can resubmit)"
        submissions = list(Submission.find({'assignment_id': assignment_id}).sort('submitted_at', -1))
        submitted_student_ids = set()
        for s in submissions:
            if s['student_id'] not in submitted_student_ids:
                submitted_student_ids.add(s['student_id'])
        return render_template('teacher_manual_submission.html',
                             teacher=teacher,
                             assignment=assignment,
                             students=all_students,
                             submitted_student_ids=submitted_student_ids)
    
    # POST: create submission
    student_id = request.form.get('student_id')
    files = request.files.getlist('files')
    if not student_id or not files or not any(f.filename for f in files):
        return render_template('teacher_manual_submission.html',
                             teacher=teacher,
                             assignment=assignment,
                             students=all_students,
                             submitted_student_ids=set(),
                             error='Please select a student and upload at least one file (PDF or images).')
    
    student_ids = [s['student_id'] for s in all_students]
    if student_id not in student_ids:
        return render_template('teacher_manual_submission.html',
                             teacher=teacher,
                             assignment=assignment,
                             students=all_students,
                             submitted_student_ids=set(),
                             error='Invalid student.')
    
    # Allow manual submission for any student: new submission or overwrite existing (resubmit)
    from bson import ObjectId
    existing_sub = Submission.find_one(
        {'assignment_id': assignment_id, 'student_id': student_id},
        sort=[('submitted_at', -1), ('created_at', -1)]
    )
    fs = GridFS(db.db)
    if existing_sub:
        submission_id = existing_sub['submission_id']
        # Delete old files from GridFS so new upload overwrites
        for old_fid in existing_sub.get('file_ids', []):
            try:
                fs.delete(ObjectId(old_fid))
            except Exception as e:
                logger.warning(f"Error deleting old submission file {old_fid}: {e}")
    else:
        submission_id = generate_submission_id()

    file_ids = []
    pages = []

    for i, file in enumerate(files):
        if not file.filename:
            continue
        file_data = file.read()
        if not file_data:
            continue
        ext = file.filename.lower().split('.')[-1]
        if ext == 'pdf':
            content_type = 'application/pdf'
            page_type = 'pdf'
        else:
            content_type = 'image/jpeg'
            page_type = 'image'
        file_id = fs.put(
            file_data,
            filename=f"{submission_id}_page_{i+1}.{ext}",
            content_type=content_type,
            submission_id=submission_id,
            page_num=i + 1
        )
        file_ids.append(str(file_id))
        pages.append({'type': page_type, 'data': file_data, 'page_num': len(pages) + 1})

    if not file_ids:
        return render_template('teacher_manual_submission.html',
                             teacher=teacher,
                             assignment=assignment,
                             students=all_students,
                             submitted_student_ids=set(),
                             error='No valid files could be read. Please upload PDF or image files.')

    submission_doc = {
        'submission_id': submission_id,
        'assignment_id': assignment_id,
        'student_id': student_id,
        'teacher_id': assignment['teacher_id'],
        'file_ids': file_ids,
        'file_type': 'pdf' if any(p['type'] == 'pdf' for p in pages) else 'image',
        'page_count': len(pages),
        'status': 'submitted',
        'submitted_at': datetime.utcnow(),
        'submitted_via': 'manual',
        'submitted_by_teacher': session['teacher_id'],
        'created_at': existing_sub['created_at'] if existing_sub else datetime.utcnow()
    }
    if existing_sub:
        # Unset rejection fields when resubmitting
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': submission_doc,
             '$unset': {'rejection_reason': '', 'rejected_at': '', 'rejected_by': ''}}
        )
    else:
        Submission.insert_one(submission_doc)
    
    # Generate AI feedback (same as student submit)
    try:
        marking_type = assignment.get('marking_type', 'standard')
        if marking_type == 'rubric':
            rubrics_content = None
            if assignment.get('rubrics_id'):
                try:
                    rubrics_file = fs.get(assignment['rubrics_id'])
                    rubrics_content = rubrics_file.read()
                except Exception:
                    pass
            ai_result = analyze_essay_with_rubrics(pages, assignment, rubrics_content, teacher)
        else:
            answer_key_content = None
            if assignment.get('answer_key_id'):
                try:
                    answer_file = fs.get(assignment['answer_key_id'])
                    answer_key_content = answer_file.read()
                except Exception:
                    pass
            ai_result = analyze_submission_images(pages, assignment, answer_key_content, teacher)
        
        is_413 = ai_result.get('error_code') == 'request_too_large' or (
            ai_result.get('error') and ('413' in str(ai_result.get('error')) or 'request_too_large' in str(ai_result.get('error')).lower())
        )
        if is_413:
            Submission.update_one(
                {'submission_id': submission_id},
                {'$set': {'ai_feedback': ai_result, 'status': 'rejected', 'rejection_reason': 'Submission too large for AI. You can still review manually.', 'rejected_at': datetime.utcnow(), 'rejected_by': 'system_413'}}
            )
        else:
            Submission.update_one(
                {'submission_id': submission_id},
                {'$set': {'ai_feedback': ai_result, 'status': 'ai_reviewed'}}
            )
    except Exception as e:
        logger.error(f"AI feedback error on manual submission: {e}")
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {'ai_feedback': {'error': str(e), 'questions': [], 'overall_feedback': f'Error: {e}'}}}
        )
    
    return redirect(url_for('review_submission', submission_id=submission_id))

def analyze_class_insights(submissions: list) -> dict:
    """Analyze AI feedback to identify class-wide patterns, misconceptions, and topics to review"""
    strengths = []
    improvements = []
    misconceptions = []
    
    question_stats = {}
    wrong_answers = {}  # Track common wrong answers per question
    
    for sub in submissions:
        ai_feedback = sub.get('ai_feedback', {})
        questions = ai_feedback.get('questions', [])
        
        for q in questions:
            raw = q.get('question_num', 0)
            try:
                q_num = int(raw) if raw not in (None, '') else 0
            except (TypeError, ValueError):
                q_num = 0
            if q_num not in question_stats:
                question_stats[q_num] = {
                    'correct': 0, 
                    'incorrect': 0, 
                    'total': 0,
                    'correct_answer': q.get('correct_answer', ''),
                    'feedbacks': []  # Collect feedback for pattern analysis
                }
                wrong_answers[q_num] = {}
            
            question_stats[q_num]['total'] += 1
            if q.get('is_correct') == True:
                question_stats[q_num]['correct'] += 1
            elif q.get('is_correct') == False:
                question_stats[q_num]['incorrect'] += 1
                # Track wrong answer patterns
                student_answer = str(q.get('student_answer', '')).strip().lower()[:100]  # Normalize
                if student_answer and student_answer != 'unclear':
                    wrong_answers[q_num][student_answer] = wrong_answers[q_num].get(student_answer, 0) + 1
            
            # Collect improvement feedback for misconception analysis
            if q.get('improvement'):
                question_stats[q_num]['feedbacks'].append(q.get('improvement'))
    
    def _question_sort_key(item):
        q_num, _ = item
        try:
            return (0, int(q_num))
        except (TypeError, ValueError):
            return (1, str(q_num))

    for q_num, stats in sorted(question_stats.items(), key=_question_sort_key):
        if stats['total'] > 0:
            correct_pct = stats['correct'] / stats['total'] * 100
            incorrect_pct = stats['incorrect'] / stats['total'] * 100
            
            if correct_pct >= 70:
                strengths.append({
                    'question': q_num,
                    'correct': stats['correct'],
                    'total': stats['total'],
                    'percentage': round(correct_pct)
                })
            
            if incorrect_pct >= 50:
                # Find common wrong answers
                common_wrong = []
                if q_num in wrong_answers:
                    sorted_wrong = sorted(wrong_answers[q_num].items(), key=lambda x: -x[1])
                    for answer, count in sorted_wrong[:3]:  # Top 3 common wrong answers
                        if count >= 2:  # At least 2 students gave this wrong answer
                            common_wrong.append({
                                'answer': answer[:50] + '...' if len(answer) > 50 else answer,
                                'count': count
                            })
                
                improvements.append({
                    'question': q_num,
                    'incorrect': stats['incorrect'],
                    'total': stats['total'],
                    'percentage': round(incorrect_pct),
                    'correct_answer': stats.get('correct_answer', ''),
                    'common_wrong': common_wrong
                })
                
                # Analyze feedbacks for misconception patterns
                if stats['feedbacks']:
                    misconceptions.append({
                        'question': q_num,
                        'sample_feedback': stats['feedbacks'][0][:150] + '...' if len(stats['feedbacks'][0]) > 150 else stats['feedbacks'][0],
                        'affected_count': stats['incorrect']
                    })
    
    # Sort by percentage
    strengths.sort(key=lambda x: -x['percentage'])
    improvements.sort(key=lambda x: -x['percentage'])
    misconceptions.sort(key=lambda x: -x['affected_count'])
    
    # Generate teaching recommendations
    recommendations = []
    if improvements:
        most_problematic = improvements[0]
        recommendations.append(f"Focus on Question {most_problematic['question']} - {most_problematic['percentage']}% of students need improvement")
    
    if len(improvements) >= 2:
        recommendations.append(f"Consider reviewing concepts in Questions {', '.join([str(i['question']) for i in improvements[:3]])}")
    
    if misconceptions:
        recommendations.append("Schedule a class discussion to address common misconceptions")
    
    return {
        'strengths': strengths[:5],
        'improvements': improvements[:5],
        'misconceptions': misconceptions[:5],
        'recommendations': recommendations
    }

@app.route('/teacher/assignment/<assignment_id>/report')
@teacher_required
def download_assignment_report(assignment_id):
    """Generate and download consolidated PDF report for assignment"""
    from utils.pdf_generator import generate_class_report_pdf
    
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    
    if not assignment:
        return 'Assignment not found', 404
    
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    # Get students based on the assignment's target type (class or teaching group)
    target_type = assignment.get('target_type', 'class')
    target_class_id = assignment.get('target_class_id')
    target_group_id = assignment.get('target_group_id')
    
    if target_type == 'teaching_group' and target_group_id:
        # Get students from the specific teaching group
        teaching_group = TeachingGroup.find_one({'group_id': target_group_id})
        if teaching_group:
            student_ids = teaching_group.get('student_ids', [])
            students = list(Student.find({'student_id': {'$in': student_ids}}))
        else:
            students = []
    elif target_type == 'class' and target_class_id:
        # Get students from the specific class who are also assigned to this teacher
        students = list(Student.find({
            'class': target_class_id,
            'teachers': session['teacher_id']
        }))
    else:
        # Fallback: get all students assigned to this teacher
        students = list(Student.find({'teachers': session['teacher_id']}))
    
    students_map = {s['student_id']: s for s in students}
    
    submissions = list(Submission.find({
        'assignment_id': assignment_id,
        'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
    }))
    
    try:
        pdf_content = generate_class_report_pdf(assignment, submissions, students_map, teacher)
        
        filename = f"report_{assignment['title'].replace(' ', '_')}_{datetime.utcnow().strftime('%Y%m%d')}.pdf"
        
        return Response(
            pdf_content,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        return f'Error generating report: {str(e)}', 500

@app.route('/teacher/assignments/create', methods=['GET', 'POST'])
@teacher_required
def create_assignment():
    """Create a new assignment"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    # Get classes for the teacher - use comprehensive detection like dashboard
    # Include: classes in teacher profile, students assigned to teacher, AND students with submissions
    teacher_classes = set(teacher.get('classes', []))
    
    # Add classes from students assigned to this teacher
    assigned_students = Student.find({'teachers': session['teacher_id']})
    for student in assigned_students:
        if student.get('class'):
            teacher_classes.add(student.get('class'))
    
    # Also find students who have submissions to this teacher's assignments
    teacher_assignments = Assignment.find({'teacher_id': session['teacher_id']})
    teacher_assignment_ids = [a['assignment_id'] for a in teacher_assignments]
    if teacher_assignment_ids:
        submissions = Submission.find({'assignment_id': {'$in': teacher_assignment_ids}})
        for sub in submissions:
            student = Student.find_one({'student_id': sub.get('student_id')})
            if student and student.get('class'):
                teacher_classes.add(student.get('class'))
    
    # Get class documents
    classes = list(Class.find({'class_id': {'$in': list(teacher_classes)}})) if teacher_classes else []
    
    # Get teaching groups for this teacher
    teaching_groups = list(TeachingGroup.find({'teacher_id': session['teacher_id']}))
    
    if request.method == 'POST':
        try:
            data = request.form
            
            # Get marking type (standard or rubric)
            marking_type = data.get('marking_type', 'standard')
            
            # Get uploaded files or Google Drive file IDs
            question_paper = request.files.get('question_paper')
            answer_key = request.files.get('answer_key')
            rubrics = request.files.get('rubrics')
            question_paper_drive_id = data.get('question_paper_drive_id')
            answer_key_drive_id = data.get('answer_key_drive_id')
            rubrics_drive_id = data.get('rubrics_drive_id')
            reference_materials_drive_id = data.get('reference_materials_drive_id')
            
            # Helper function to get file content from Drive or upload
            # For Drive files, we only download temporarily for text extraction, not for storage
            def get_file_content(file_obj, drive_id, file_type_name):
                if drive_id:
                    # Download from Google Drive temporarily (only for text extraction)
                    # We'll store the Drive ID as reference, not the file content
                    try:
                        from utils.google_drive import get_drive_service, DriveManager
                        service = get_drive_service()
                        if service:
                            manager = DriveManager(service)
                            content = manager.get_file_content(drive_id, export_as_pdf=True)
                            if content:
                                # Return content for text extraction, but mark as Drive file
                                return content, f"DRIVE:{drive_id}"
                            else:
                                raise Exception(f"Failed to download {file_type_name} from Google Drive")
                        else:
                            raise Exception("Google Drive not configured")
                    except Exception as e:
                        logger.error(f"Error downloading {file_type_name} from Drive: {e}")
                        raise Exception(f"Failed to download {file_type_name} from Google Drive: {str(e)}")
                elif file_obj and file_obj.filename:
                    # Use uploaded file
                    content = file_obj.read()
                    file_obj.seek(0)
                    return content, file_obj.filename
                return None, None
            
            # Validate required files based on marking type
            if marking_type == 'rubric':
                # For rubric-based: question paper and rubrics are required
                if not question_paper_drive_id and (not question_paper or not question_paper.filename):
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         teacher_modules=teacher_modules,
                                         error='Question paper PDF is required')
                if not rubrics_drive_id and (not rubrics or not rubrics.filename):
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         teacher_modules=teacher_modules,
                                         error='Rubrics PDF is required for rubric-based marking')
            else:
                # For standard: question paper and answer key are required
                if not question_paper_drive_id and (not question_paper or not question_paper.filename):
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         teacher_modules=teacher_modules,
                                         error='Question paper PDF is required')
                if not answer_key_drive_id and (not answer_key or not answer_key.filename):
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         teacher_modules=teacher_modules,
                                         error='Answer key PDF is required')
            
            assignment_id = generate_assignment_id()
            total_marks = int(data.get('total_marks', 100))
            assignment_title = data.get('title', 'Untitled')
            
            # Get file contents from Drive or uploads
            # For Drive files, we download temporarily only for text extraction
            try:
                question_paper_content, question_paper_name = get_file_content(question_paper, question_paper_drive_id, 'question paper')
                answer_key_content, answer_key_name = get_file_content(answer_key, answer_key_drive_id, 'answer key')
                reference_materials = request.files.get('reference_materials')
                reference_materials_content, reference_materials_name = get_file_content(reference_materials, reference_materials_drive_id, 'reference materials')
                rubrics_content, rubrics_name = get_file_content(rubrics, rubrics_drive_id, 'rubrics')
            except Exception as e:
                return render_template('teacher_create_assignment.html',
                                     teacher=teacher,
                                     classes=classes,
                                     teaching_groups=teaching_groups,
                                     teacher_modules=teacher_modules,
                                     error=str(e))
            
            # Extract text from PDFs for cost-effective AI processing
            question_paper_text = extract_text_from_pdf(question_paper_content) if question_paper_content else ""
            answer_key_text = extract_text_from_pdf(answer_key_content) if answer_key_content else ""
            reference_materials_text = extract_text_from_pdf(reference_materials_content) if reference_materials_content else ""
            rubrics_text = extract_text_from_pdf(rubrics_content) if rubrics_content else ""
            
            # Store files in GridFS (only for uploaded files, not Drive files)
            from gridfs import GridFS
            fs = GridFS(db.db)
            
            # Save question paper (only if uploaded, not from Drive)
            question_paper_id = None
            if question_paper_content and not question_paper_name.startswith('DRIVE:'):
                question_paper_id = fs.put(
                    question_paper_content,
                    filename=f"{assignment_id}_question.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='question_paper'
                )
            
            # Save answer key (only if uploaded, not from Drive)
            answer_key_id = None
            if answer_key_content and not answer_key_name.startswith('DRIVE:'):
                answer_key_id = fs.put(
                    answer_key_content,
                    filename=f"{assignment_id}_answer.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='answer_key'
                )
            
            # Save reference materials (only if uploaded, not from Drive)
            reference_materials_id = None
            if reference_materials_content and not (reference_materials_name and reference_materials_name.startswith('DRIVE:')):
                reference_materials_id = fs.put(
                    reference_materials_content,
                    filename=f"{assignment_id}_reference.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='reference_materials'
                )
            
            # Save rubrics (only if uploaded, not from Drive)
            rubrics_id = None
            if rubrics_content and not (rubrics_name and rubrics_name.startswith('DRIVE:')):
                rubrics_id = fs.put(
                    rubrics_content,
                    filename=f"{assignment_id}_rubrics.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='rubrics'
                )
            
            # Initialize Google Drive folder IDs and file references
            drive_folders = None
            drive_file_refs = {}
            
            # Store Drive file IDs as references (we don't copy files, just reference them)
            if question_paper_drive_id:
                drive_file_refs['question_paper_drive_id'] = question_paper_drive_id
            if answer_key_drive_id:
                drive_file_refs['answer_key_drive_id'] = answer_key_drive_id
            if reference_materials_drive_id:
                drive_file_refs['reference_materials_drive_id'] = reference_materials_drive_id
            if rubrics_drive_id:
                drive_file_refs['rubrics_drive_id'] = rubrics_drive_id
            
            # Create Google Drive folder structure for submissions (not for source files)
            if teacher.get('google_drive_folder_id'):
                try:
                    from utils.google_drive import create_assignment_folder_structure
                    
                    # Create folder structure for submissions only
                    drive_folders = create_assignment_folder_structure(
                        teacher=teacher,
                        assignment_title=assignment_title,
                        assignment_id=assignment_id
                    )
                    logger.info(f"Created submission folder structure for assignment {assignment_id}")
                except Exception as drive_error:
                    logger.warning(f"Google Drive folder creation failed (continuing anyway): {drive_error}")
            
            # Get teacher's default AI model if not specified
            teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
            default_model = teacher.get('default_ai_model', 'anthropic') if teacher else 'anthropic'
            ai_model = data.get('ai_model', default_model)
            
            # Get assignment target (class or teaching group)
            target_type = data.get('target_type', 'class')
            target_class_id = data.get('target_class_id', '').strip() or None
            target_group_id = data.get('target_group_id', '').strip() or None
            
            # Award marks: for standard only; rubric always uses marks (no change)
            award_marks = True
            if marking_type == 'standard':
                award_marks = data.get('award_marks', 'on') == 'on'
            
            # When to send feedback: teacher reviews first (default) or send AI feedback to student straight away
            send_ai_feedback_immediately = data.get('send_ai_feedback_immediately', 'off') == 'on'
            
            # Build assignment document
            assignment_doc = {
                'assignment_id': assignment_id,
                'teacher_id': session['teacher_id'],
                'title': assignment_title,
                'subject': data.get('subject', 'General'),
                'instructions': data.get('instructions', ''),
                'total_marks': total_marks,
                'marking_type': marking_type,  # 'standard' or 'rubric'
                'award_marks': award_marks,  # True = show marks; False = show Correct/Partial/Incorrect only (standard only)
                'send_ai_feedback_immediately': send_ai_feedback_immediately,  # True = student sees AI feedback right after submit; False = teacher reviews first
                'question_paper_id': question_paper_id,
                'answer_key_id': answer_key_id,
                'question_paper_name': question_paper.filename if question_paper and question_paper.filename else (question_paper_name.replace('DRIVE:', '') if question_paper_name and question_paper_name.startswith('DRIVE:') else None),
                'answer_key_name': answer_key.filename if answer_key and answer_key.filename else (answer_key_name.replace('DRIVE:', '') if answer_key_name and answer_key_name.startswith('DRIVE:') else None),
                # New optional document fields
                'reference_materials_id': reference_materials_id,
                'reference_materials_name': reference_materials.filename if reference_materials and reference_materials.filename else (reference_materials_name.replace('DRIVE:', '') if reference_materials_name and reference_materials_name.startswith('DRIVE:') else None),
                'rubrics_id': rubrics_id,
                'rubrics_name': rubrics.filename if rubrics and rubrics.filename else (rubrics_name.replace('DRIVE:', '') if rubrics_name and rubrics_name.startswith('DRIVE:') else None),
                # Extracted text for cost-effective AI processing
                'question_paper_text': question_paper_text,
                'answer_key_text': answer_key_text,
                'reference_materials_text': reference_materials_text,
                'rubrics_text': rubrics_text,
                'due_date': data.get('due_date') or None,
                'status': 'published' if data.get('publish') else 'draft',
                'ai_model': ai_model,
                'feedback_instructions': data.get('feedback_instructions', ''),
                'grading_instructions': data.get('grading_instructions', ''),
                'target_type': target_type,
                'target_class_id': target_class_id if target_type == 'class' else None,
                'target_group_id': target_group_id if target_type == 'teaching_group' else None,
                # Student AI help limits
                'enable_overall_review': data.get('enable_overall_review') == 'on',
                'overall_review_limit': int(data.get('overall_review_limit', 1)),
                'enable_question_help': data.get('enable_question_help') == 'on',
                'question_help_limit': int(data.get('question_help_limit', 5)),
                'notify_student_telegram': data.get('notify_student_telegram') == 'on',
                'linked_module_id': (data.get('linked_module_id') or '').strip() or None,  # Link to module tree for profile/mastery
                'created_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            }
            
            # Add Google Drive folder IDs if created (for submissions)
            if drive_folders:
                assignment_doc['drive_folders'] = drive_folders
            
            # Add Drive file references (we reference files, don't copy them)
            if drive_file_refs:
                assignment_doc['drive_file_refs'] = drive_file_refs
            
            Assignment.insert_one(assignment_doc)
            
            # Send push notifications if assignment is published
            if assignment_doc['status'] == 'published':
                try:
                    from utils.push_notifications import send_assignment_notification, is_push_configured
                    if is_push_configured():
                        push_result = send_assignment_notification(
                            db=db,
                            assignment=assignment_doc,
                            class_id=target_class_id,
                            teaching_group_id=target_group_id
                        )
                        logger.info(f"Push notifications sent for assignment {assignment_id}: {push_result}")
                except Exception as push_error:
                    logger.warning(f"Push notification failed (non-critical): {push_error}")
            
            return redirect(url_for('teacher_assignments'))
            
        except Exception as e:
            logger.error(f"Error creating assignment: {e}")
            return render_template('teacher_create_assignment.html',
                                 teacher=teacher,
                                 classes=classes,
                                 teaching_groups=teaching_groups,
                                 teacher_modules=teacher_modules,
                                 error=f'Failed to create assignment: {str(e)}')
    
    return render_template('teacher_create_assignment.html',
                         teacher=teacher,
                         classes=classes,
                         teaching_groups=teaching_groups,
                         teacher_modules=teacher_modules)

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
    
    # Get classes for the teacher - use comprehensive detection like dashboard
    teacher_classes = set(teacher.get('classes', []))
    
    # Add classes from students assigned to this teacher
    assigned_students = Student.find({'teachers': session['teacher_id']})
    for student in assigned_students:
        if student.get('class'):
            teacher_classes.add(student.get('class'))
    
    # Also find students who have submissions to this teacher's assignments
    teacher_assignments = Assignment.find({'teacher_id': session['teacher_id']})
    teacher_assignment_ids = [a['assignment_id'] for a in teacher_assignments]
    if teacher_assignment_ids:
        submissions = Submission.find({'assignment_id': {'$in': teacher_assignment_ids}})
        for sub in submissions:
            student = Student.find_one({'student_id': sub.get('student_id')})
            if student and student.get('class'):
                teacher_classes.add(student.get('class'))
    
    # Get class documents
    classes = list(Class.find({'class_id': {'$in': list(teacher_classes)}})) if teacher_classes else []
    
    # Get teaching groups for this teacher
    teaching_groups = list(TeachingGroup.find({'teacher_id': session['teacher_id']}))
    
    # Teacher's module trees (for linking assignment to module)
    teacher_modules = []
    if _teacher_has_module_access(session['teacher_id']):
        teacher_modules = list(
            Module.find({'teacher_id': session['teacher_id'], 'parent_id': None}).sort('title', 1)
        )
    
    if request.method == 'POST':
        try:
            data = request.form
            
            # Get teacher's default AI model if not specified
            default_model = teacher.get('default_ai_model', 'anthropic') if teacher else 'anthropic'
            ai_model = data.get('ai_model', default_model)
            
            # Get assignment target (class or teaching group)
            target_type = data.get('target_type', 'class')
            target_class_id = data.get('target_class_id', '').strip() or None
            target_group_id = data.get('target_group_id', '').strip() or None
            
            # Award marks: only for non-rubric (standard or legacy assignments); rubric stays as-is
            award_marks = assignment.get('award_marks', True)
            if assignment.get('marking_type') != 'rubric':
                award_marks = data.get('award_marks', 'on') == 'on'
            
            send_ai_feedback_immediately = data.get('send_ai_feedback_immediately', 'off') == 'on'
            
            update_data = {
                'title': data.get('title', assignment['title']),
                'subject': data.get('subject', assignment['subject']),
                'instructions': data.get('instructions', ''),
                'total_marks': int(data.get('total_marks', assignment.get('total_marks', 100))),
                'due_date': data.get('due_date') or None,
                'status': 'published' if data.get('publish') else 'draft',
                'ai_model': ai_model,
                'award_marks': award_marks,
                'send_ai_feedback_immediately': send_ai_feedback_immediately,
                'feedback_instructions': data.get('feedback_instructions', ''),
                'grading_instructions': data.get('grading_instructions', ''),
                'target_type': target_type,
                'target_class_id': target_class_id if target_type == 'class' else None,
                'target_group_id': target_group_id if target_type == 'teaching_group' else None,
                # Student AI help limits
                'enable_overall_review': data.get('enable_overall_review') == 'on',
                'overall_review_limit': int(data.get('overall_review_limit', 1)),
                'enable_question_help': data.get('enable_question_help') == 'on',
                'question_help_limit': int(data.get('question_help_limit', 5)),
                'notify_student_telegram': data.get('notify_student_telegram') == 'on',
                'linked_module_id': (data.get('linked_module_id') or '').strip() or None,
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
                    
                    # Read content and extract text
                    question_paper_content = question_paper.read()
                    question_paper_text = extract_text_from_pdf(question_paper_content)
                    
                    # Save new file
                    question_paper_id = fs.put(
                        question_paper_content,
                        filename=f"{assignment_id}_question.pdf",
                        content_type='application/pdf',
                        assignment_id=assignment_id,
                        file_type='question_paper'
                    )
                    update_data['question_paper_id'] = question_paper_id
                    update_data['question_paper_name'] = question_paper.filename
                    update_data['question_paper_text'] = question_paper_text
            
            answer_key = request.files.get('answer_key')
            if answer_key and answer_key.filename:
                if answer_key.filename.lower().endswith('.pdf'):
                    # Delete old file if exists
                    if assignment.get('answer_key_id'):
                        try:
                            fs.delete(assignment['answer_key_id'])
                        except:
                            pass
                    
                    # Read content and extract text
                    answer_key_content = answer_key.read()
                    answer_key_text = extract_text_from_pdf(answer_key_content)
                    
                    # Save new file
                    answer_key_id = fs.put(
                        answer_key_content,
                        filename=f"{assignment_id}_answer.pdf",
                        content_type='application/pdf',
                        assignment_id=assignment_id,
                        file_type='answer_key'
                    )
                    update_data['answer_key_id'] = answer_key_id
                    update_data['answer_key_name'] = answer_key.filename
                    update_data['answer_key_text'] = answer_key_text
            
            # Handle reference materials upload
            reference_materials = request.files.get('reference_materials')
            if reference_materials and reference_materials.filename:
                if reference_materials.filename.lower().endswith('.pdf'):
                    # Delete old file if exists
                    if assignment.get('reference_materials_id'):
                        try:
                            fs.delete(assignment['reference_materials_id'])
                        except:
                            pass
                    
                    # Read content and extract text
                    reference_materials_content = reference_materials.read()
                    reference_materials_text = extract_text_from_pdf(reference_materials_content)
                    
                    # Save new file
                    reference_materials_id = fs.put(
                        reference_materials_content,
                        filename=f"{assignment_id}_reference.pdf",
                        content_type='application/pdf',
                        assignment_id=assignment_id,
                        file_type='reference_materials'
                    )
                    update_data['reference_materials_id'] = reference_materials_id
                    update_data['reference_materials_name'] = reference_materials.filename
                    update_data['reference_materials_text'] = reference_materials_text
            
            # Handle rubrics upload
            rubrics = request.files.get('rubrics')
            if rubrics and rubrics.filename:
                if rubrics.filename.lower().endswith('.pdf'):
                    # Delete old file if exists
                    if assignment.get('rubrics_id'):
                        try:
                            fs.delete(assignment['rubrics_id'])
                        except:
                            pass
                    
                    # Read content and extract text
                    rubrics_content = rubrics.read()
                    rubrics_text = extract_text_from_pdf(rubrics_content)
                    
                    # Save new file
                    rubrics_id = fs.put(
                        rubrics_content,
                        filename=f"{assignment_id}_rubrics.pdf",
                        content_type='application/pdf',
                        assignment_id=assignment_id,
                        file_type='rubrics'
                    )
                    update_data['rubrics_id'] = rubrics_id
                    update_data['rubrics_name'] = rubrics.filename
                    update_data['rubrics_text'] = rubrics_text
            
            Assignment.update_one(
                {'assignment_id': assignment_id},
                {'$set': update_data}
            )
            
            return redirect(url_for('teacher_assignments'))
            
        except Exception as e:
            logger.error(f"Error updating assignment: {e}")
    
    return render_template('teacher_edit_assignment.html',
                         teacher=teacher,
                         assignment=assignment,
                         classes=classes,
                         teaching_groups=teaching_groups,
                         teacher_modules=teacher_modules)

@app.route('/teacher/assignments/<assignment_id>/file/<file_type>')
@teacher_required
def download_assignment_file(assignment_id, file_type):
    """Download assignment PDF file"""
    from gridfs import GridFS
    from bson import ObjectId
    
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    
    if not assignment:
        return 'Assignment not found', 404
    
    file_id_field = f"{file_type}_id"
    file_name_field = f"{file_type}_name"
    drive_ref_field = f"{file_type}_drive_id"
    
    # Check if file is from Google Drive (reference only, not copied)
    drive_file_refs = assignment.get('drive_file_refs', {})
    drive_file_id = drive_file_refs.get(drive_ref_field)
    
    if drive_file_id:
        # Fetch from Google Drive on-demand
        try:
            from utils.google_drive import get_drive_service, DriveManager
            service = get_drive_service()
            if service:
                manager = DriveManager(service)
                file_content = manager.get_file_content(drive_file_id, export_as_pdf=True)
                if file_content:
                    # Get file name from Drive
                    file_metadata = service.files().get(fileId=drive_file_id, fields="name").execute()
                    file_name = file_metadata.get('name', assignment.get(file_name_field, 'document.pdf'))
                    return Response(
                        file_content,
                        mimetype='application/pdf',
                        headers={
                            'Content-Disposition': f'inline; filename="{file_name}"'
                        }
                    )
            return 'File not found', 404
        except Exception as e:
            logger.error(f"Error fetching file from Google Drive: {e}")
            return 'File not found', 404
    
    # Otherwise, get from GridFS (uploaded file)
    if file_id_field not in assignment or not assignment[file_id_field]:
        return 'File not found', 404
    
    fs = GridFS(db.db)
    try:
        file_id = assignment[file_id_field]
        if isinstance(file_id, str):
            file_id = ObjectId(file_id)
        file_data = fs.get(file_id)
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
    """Delete an assignment and all related submissions"""
    try:
        from gridfs import GridFS
        
        assignment = Assignment.find_one({
            'assignment_id': assignment_id,
            'teacher_id': session['teacher_id']
        })
        
        if not assignment:
            return jsonify({'error': 'Assignment not found'}), 404
        
        fs = GridFS(db.db)
        
        # Delete all submissions for this assignment
        submissions = list(Submission.find({'assignment_id': assignment_id}))
        for submission in submissions:
            # Delete submission files from GridFS
            for file_id in submission.get('file_ids', []):
                try:
                    from bson import ObjectId
                    fs.delete(ObjectId(file_id))
                except Exception as e:
                    logger.warning(f"Error deleting submission file {file_id}: {e}")
        
        # Delete submissions from database
        submission_count = len(submissions)
        db.db.submissions.delete_many({'assignment_id': assignment_id})
        
        # Delete assignment files from GridFS
        if assignment.get('question_paper_id'):
            try:
                fs.delete(assignment['question_paper_id'])
            except Exception as e:
                logger.warning(f"Error deleting question paper: {e}")
        
        if assignment.get('answer_key_id'):
            try:
                fs.delete(assignment['answer_key_id'])
            except Exception as e:
                logger.warning(f"Error deleting answer key: {e}")
        
        # Delete the assignment
        db.db.assignments.delete_one({'assignment_id': assignment_id})
        
        return jsonify({
            'success': True,
            'message': f'Assignment deleted along with {submission_count} submission(s)'
        })
        
    except Exception as e:
        logger.error(f"Error deleting assignment: {e}")
        return jsonify({'error': 'Failed to delete'}), 500

def get_next_pending_submission(teacher_id, current_submission_id=None, assignment_id=None):
    """Get the next pending submission for a teacher.
    If assignment_id is given, only return a submission from that same assignment (same class/teaching group)."""
    if assignment_id:
        # Stay in same assignment: next pending submission for this assignment only
        assignment_ids = [assignment_id]
    else:
        assignments = list(Assignment.find({'teacher_id': teacher_id}))
        assignment_ids = [a['assignment_id'] for a in assignments]
    
    if not assignment_ids:
        return None
    
    # Only truly pending (waiting for teacher), exclude AI feedback already sent
    query = {
        'assignment_id': {'$in': assignment_ids},
        '$or': [
            {'status': 'submitted'},
            {'status': 'ai_reviewed', 'feedback_sent': {'$ne': True}}
        ]
    }
    if current_submission_id:
        query['submission_id'] = {'$ne': current_submission_id}
    
    next_submissions = list(Submission.find(query).sort('submitted_at', 1).limit(1))
    return next_submissions[0] if next_submissions else None

@app.route('/teacher/submissions')
@teacher_required
def teacher_submissions():
    """List all students in the assignment's class/group with submission status.
    Teacher can filter by Teaching Group or Class first, then select an assignment."""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    status_filter = request.args.get('status', 'all')
    assignment_filter = request.args.get('assignment', '')
    teaching_group_filter = request.args.get('teaching_group', '')
    class_filter = request.args.get('class_id', '')  # use class_id to avoid HTML reserved name
    
    # Teaching groups and classes for filter dropdowns
    teaching_groups = list(TeachingGroup.find({'teacher_id': session['teacher_id']}))
    all_teacher_assignments = list(Assignment.find({'teacher_id': session['teacher_id']}))
    class_ids = set()
    for a in all_teacher_assignments:
        if a.get('target_type') == 'class' and a.get('target_class_id'):
            class_ids.add(a['target_class_id'])
    for c in teacher.get('classes', []):
        class_ids.add(c)
    classes_for_dropdown = []
    for cid in sorted(class_ids):
        class_info = Class.find_one({'class_id': cid}) or {'class_id': cid}
        classes_for_dropdown.append({
            'class_id': cid,
            'name': class_info.get('name', cid)
        })
    
    selected_assignment = None
    assignments_for_dropdown = []
    student_rows = []
    
    if assignment_filter:
        selected_assignment = Assignment.find_one({
            'assignment_id': assignment_filter,
            'teacher_id': session['teacher_id']
        })
        if selected_assignment:
            assignments_for_dropdown = _assignments_same_class_or_group(
                selected_assignment, session['teacher_id']
            )
            # If TG or Class filter is set, restrict to that and clear selection if it doesn't match
            if teaching_group_filter:
                assignments_for_dropdown = [
                    a for a in assignments_for_dropdown
                    if a.get('target_type') == 'teaching_group' and a.get('target_group_id') == teaching_group_filter
                ]
            elif class_filter:
                assignments_for_dropdown = [
                    a for a in assignments_for_dropdown
                    if a.get('target_type') == 'class' and a.get('target_class_id') == class_filter
                ]
            aid_in_list = {a['assignment_id'] for a in assignments_for_dropdown}
            if selected_assignment['assignment_id'] not in aid_in_list:
                selected_assignment = None
                assignment_filter = ''
                student_rows = []
            else:
                all_students = _get_students_for_assignment(selected_assignment, session['teacher_id'])
                # Latest submission per student (so resubmit or manual submission after rejection shows current status, not Rejected)
                submissions_for_assignment = list(Submission.find(
                    {'assignment_id': assignment_filter}
                ).sort('submitted_at', -1))
                sub_by_student = {}
                for s in submissions_for_assignment:
                    sid = s['student_id']
                    if sid not in sub_by_student:
                        sub_by_student[sid] = s
                total_marks = float(selected_assignment.get('total_marks', 100) or 100)
                for student in sorted(all_students, key=lambda x: x.get('name', '')):
                    sub = sub_by_student.get(student['student_id'])
                    status = (sub.get('status') or 'submitted') if sub else 'not_submitted'
                    # Don't show "Rejected" for manual submissions or when student has resubmitted (we use latest only).
                    # Treat manual+rejected as pending so teacher sees "Pending Review" and can still review.
                    if sub and sub.get('submitted_via') == 'manual' and status == 'rejected':
                        status = 'ai_reviewed'
                    # Display status: AI Feedback Sent vs Pending Review (only when truly waiting for teacher)
                    feedback_sent = sub.get('feedback_sent', False) if sub else False
                    if status == 'ai_reviewed' and feedback_sent:
                        display_status = 'ai_feedback_sent'
                    elif status in ('submitted', 'ai_reviewed'):
                        display_status = 'pending'
                    else:
                        display_status = status
                    display_marks, percentage = _submission_display_marks(sub, total_marks) if sub else (None, 0)
                    row = {
                        'student': student,
                        'submission': sub,
                        'status': status,
                        'display_status': display_status,
                        'percentage': percentage,
                        'final_marks': display_marks,
                        'total_marks': int(total_marks)
                    }
                    if status_filter == 'all':
                        student_rows.append(row)
                    elif status_filter == 'pending' and display_status == 'pending':
                        student_rows.append(row)
                    elif status_filter == 'ai_feedback_sent' and display_status == 'ai_feedback_sent':
                        student_rows.append(row)
                    elif status_filter == 'approved' and status in ('reviewed', 'approved'):
                        student_rows.append(row)
                    elif status_filter == 'rejected' and status == 'rejected':
                        student_rows.append(row)
                    elif status_filter == 'not_submitted' and status == 'not_submitted':
                        student_rows.append(row)
    else:
        # No assignment selected: filter assignments by Teaching Group and/or Class if set
        if teaching_group_filter:
            assignments_for_dropdown = [
                a for a in all_teacher_assignments
                if a.get('target_type') == 'teaching_group' and a.get('target_group_id') == teaching_group_filter
            ]
        elif class_filter:
            assignments_for_dropdown = [
                a for a in all_teacher_assignments
                if a.get('target_type') == 'class' and a.get('target_class_id') == class_filter
            ]
        else:
            assignments_for_dropdown = all_teacher_assignments
    
    # When an assignment is selected, pre-fill TG/Class filters from that assignment for display
    if selected_assignment and not teaching_group_filter and not class_filter:
        if selected_assignment.get('target_type') == 'teaching_group':
            teaching_group_filter = selected_assignment.get('target_group_id') or ''
        elif selected_assignment.get('target_type') == 'class':
            class_filter = selected_assignment.get('target_class_id') or ''
    
    return render_template('teacher_submissions.html',
                         teacher=teacher,
                         student_rows=student_rows,
                         assignments=assignments_for_dropdown,
                         selected_assignment=selected_assignment,
                         status_filter=status_filter,
                         assignment_filter=assignment_filter,
                         teaching_groups=teaching_groups,
                         classes_for_dropdown=classes_for_dropdown,
                         teaching_group_filter=teaching_group_filter,
                         class_filter=class_filter)

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
    
    # Choose template based on marking type
    marking_type = assignment.get('marking_type', 'standard')
    if marking_type == 'rubric':
        return render_template('teacher_review_rubric.html',
                             teacher=teacher,
                             submission=submission,
                             assignment=assignment,
                             student=student,
                             ai_feedback=ai_feedback,
                             page_count=page_count)
    else:
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
@limiter.limit("200 per hour")  # generous limit for marking; auto-save fires often
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
            'total_marks_max': data.get('total_marks_max'),
            'edited_at': datetime.utcnow(),
            'edited_by': session['teacher_id']
        }
        
        update_data = {
            'teacher_feedback': teacher_feedback,
            'final_marks': data.get('total_marks'),
            'updated_at': datetime.utcnow()
        }
        
        # Save include_answer_key preference if provided
        if 'include_answer_key' in data:
            update_data['include_answer_key'] = data.get('include_answer_key', False)
        
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': update_data}
        )
        
        # Upload feedback PDF to Google Drive if configured
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        student = Student.find_one({'student_id': submission['student_id']})
        
        if teacher and teacher.get('google_drive_folder_id') and assignment.get('drive_folders', {}).get('submissions_folder_id'):
            try:
                from utils.pdf_generator import generate_review_pdf
                from utils.google_drive import upload_student_submission
                
                # Generate PDF with student work and feedback
                pdf_content = generate_review_pdf(submission, assignment, student, teacher)
                if pdf_content:
                    # Upload to submissions folder
                    drive_result = upload_student_submission(
                        teacher=teacher,
                        submissions_folder_id=assignment['drive_folders']['submissions_folder_id'],
                        submission_content=pdf_content,
                        filename=f"feedback_{submission_id}.pdf",
                        student_name=student.get('name'),
                        student_id=student.get('student_id')
                    )
                    if drive_result:
                        Submission.update_one(
                            {'submission_id': submission_id},
                            {'$set': {'feedback_drive_file': drive_result}}
                        )
                        logger.info(f"Uploaded feedback PDF for {submission_id} to Google Drive")
            except Exception as drive_error:
                logger.warning(f"Could not upload feedback PDF to Drive: {drive_error}")
        
        return jsonify({'success': True, 'message': 'Feedback saved'})
        
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/api/available-ai-models')
@teacher_required
def available_ai_models():
    """Return which AI models the teacher has API keys for (for Remark model picker)."""
    try:
        from utils.ai_marking import get_available_ai_models
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        models = get_available_ai_models(teacher)
        labels = {'anthropic': 'Anthropic (Claude)', 'openai': 'OpenAI (GPT)', 'deepseek': 'DeepSeek', 'google': 'Google Gemini'}
        return jsonify({
            'success': True,
            'models': models,
            'labels': labels
        })
    except Exception as e:
        logger.error(f"Error getting available AI models: {e}")
        return jsonify({'success': False, 'models': {}, 'labels': {}}), 500

@app.route('/teacher/review/<submission_id>/regenerate-ai-feedback', methods=['POST'])
@teacher_required
def regenerate_ai_feedback(submission_id):
    """Re-run AI feedback generation for a submission. Optional JSON body: { \"ai_model\": \"anthropic\" | \"openai\" | \"deepseek\" | \"google\" } to choose model."""
    from gridfs import GridFS
    from bson import ObjectId
    from utils.ai_marking import analyze_submission_images, analyze_essay_with_rubrics
    
    try:
        data = request.get_json() or {}
        override_ai_model = data.get('ai_model')  # optional: use this model for this run only
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'success': False, 'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        fs = GridFS(db.db)
        
        # Build pages from stored files
        file_ids = submission.get('file_ids', [])
        if not file_ids:
            return jsonify({'success': False, 'error': 'No submission files found'}), 400
        
        pages = []
        for i, fid in enumerate(file_ids):
            try:
                file_data = fs.get(ObjectId(fid))
                raw = file_data.read()
                content_type = (file_data.content_type or '').lower()
                if 'pdf' in content_type:
                    page_type = 'pdf'
                else:
                    page_type = 'image'
                pages.append({'type': page_type, 'data': raw, 'page_num': i + 1})
            except Exception as e:
                logger.warning(f"Could not read file {fid}: {e}")
                continue
        
        if not pages:
            return jsonify({'success': False, 'error': 'Could not read any submission files'}), 400
        
        marking_type = assignment.get('marking_type', 'standard')
        
        if marking_type == 'rubric':
            rubrics_content = None
            if assignment.get('rubrics_id'):
                try:
                    rubrics_file = fs.get(assignment['rubrics_id'])
                    rubrics_content = rubrics_file.read()
                except Exception:
                    pass
            ai_result = analyze_essay_with_rubrics(pages, assignment, rubrics_content, teacher, override_ai_model=override_ai_model)
        else:
            answer_key_content = None
            if assignment.get('answer_key_id'):
                try:
                    answer_file = fs.get(assignment['answer_key_id'])
                    answer_key_content = answer_file.read()
                except Exception:
                    pass
            ai_result = analyze_submission_images(pages, assignment, answer_key_content, teacher, override_ai_model=override_ai_model)
        
        # Determine status: ai_reviewed if we got valid feedback, else keep submitted so teacher can retry
        has_error = ai_result.get('error') or (
            marking_type == 'standard' and not ai_result.get('questions')
        ) or (
            marking_type == 'rubric' and not ai_result.get('criteria') and not ai_result.get('errors')
        )
        new_status = 'ai_reviewed' if not has_error else submission.get('status', 'submitted')
        
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'ai_feedback': ai_result,
                'status': new_status,
                'updated_at': datetime.utcnow()
            }}
        )
        
        if has_error:
            err_msg = ai_result.get('error') or ai_result.get('overall_feedback')
            if not err_msg:
                if marking_type == 'standard' and not ai_result.get('questions'):
                    err_msg = (
                        'AI did not return question-by-question feedback. '
                        'The model may have returned an unexpected format. '
                        'Try "Remark with another model" or ensure the submission and answer key are clear.'
                    )
                elif marking_type == 'rubric' and not ai_result.get('criteria') and not ai_result.get('errors'):
                    err_msg = (
                        'AI did not return rubric feedback. '
                        'Try "Remark with another model" or check that the submission is readable.'
                    )
                else:
                    err_msg = 'AI feedback could not be generated. Try "Remark with another model" or try again later.'
            # Log for debugging (avoid logging huge raw response)
            raw_preview = (ai_result.get('raw_response') or '')[:500] if isinstance(ai_result.get('raw_response'), str) else ''
            logger.warning(
                "Regenerate AI feedback treated as failed: has_error=True, error=%s, keys=%s, raw_preview=%s",
                ai_result.get('error'), list(ai_result.keys()), raw_preview
            )
            return jsonify({
                'success': False,
                'error': err_msg,
                'message': 'AI feedback failed. You can edit manually or try again.'
            }), 200
        return jsonify({'success': True, 'message': 'AI feedback generated successfully'})
        
    except Exception as e:
        logger.error(f"Error regenerating AI feedback: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/extract-answer-key', methods=['POST'])
@teacher_required
def extract_answer_key(submission_id):
    """Extract answers from an uploaded answer key file using AI"""
    try:
        from utils.ai_marking import extract_answers_from_key
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        # Get uploaded file
        if 'answer_key' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        
        file = request.files['answer_key']
        if not file.filename:
            return jsonify({'error': 'No file selected'}), 400
        
        # Determine file type
        filename = file.filename.lower()
        if filename.endswith('.pdf'):
            file_type = 'pdf'
        elif filename.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            file_type = 'image'
        else:
            return jsonify({'error': 'Unsupported file type. Please upload PDF or image.'}), 400
        
        file_content = file.read()
        
        # Get question count from AI feedback or default
        ai_feedback = submission.get('ai_feedback', {})
        questions = ai_feedback.get('questions', [])
        question_count = int(request.form.get('question_count', len(questions) or 10))
        
        # Get teacher for API key
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        
        # Extract answers using AI
        result = extract_answers_from_key(file_content, file_type, question_count, teacher, assignment)
        
        if 'error' in result and not result.get('answers'):
            return jsonify({'error': result['error']}), 500
        
        return jsonify({
            'success': True,
            'answers': result.get('answers', {}),
            'notes': result.get('notes', '')
        })
        
    except Exception as e:
        logger.error(f"Error extracting answer key: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/teacher/review/<submission_id>/send', methods=['POST'])
@limiter.limit("200 per hour")
@teacher_required
def send_feedback_to_student(submission_id):
    """Send feedback to student via Telegram"""
    try:
        # Get request data (may include include_answer_key flag)
        data = request.get_json() or {}
        include_answer_key = data.get('include_answer_key', False)
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        student = Student.find_one({'student_id': submission['student_id']})
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        
        # Update status and mark feedback as sent, including answer key preference
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'status': 'reviewed',
                'feedback_sent': True,
                'include_answer_key': include_answer_key,
                'reviewed_at': datetime.utcnow()
            }}
        )
        # Update student module mastery and learning profile when assignment is linked to a module
        submission_after = Submission.find_one({'submission_id': submission_id})
        if submission_after:
            _update_profile_and_mastery_from_assignment(submission['student_id'], assignment, submission_after)
        
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
        
        # Get next pending submission (same assignment only)
        next_submission = get_next_pending_submission(
            session['teacher_id'], submission_id, assignment_id=submission['assignment_id']
        )
        
        return jsonify({
            'success': True, 
            'message': 'Feedback sent to student',
            'next_submission_id': next_submission['submission_id'] if next_submission else None,
            'next_submission_url': f"/teacher/review/{next_submission['submission_id']}" if next_submission else None
        })
        
    except Exception as e:
        logger.error(f"Error sending feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/save-rubric', methods=['POST'])
@limiter.limit("200 per hour")  # generous limit for marking; auto-save fires often
@teacher_required
def save_rubric_feedback(submission_id):
    """Save teacher feedback for rubric-based essay marking"""
    try:
        data = request.get_json()
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        # Update teacher feedback for rubric-based marking
        teacher_feedback = {
            'criteria': data.get('criteria', {}),
            'errors': data.get('errors', []),
            'overall_feedback': data.get('overall_feedback', ''),
            'total_marks': data.get('total_marks'),
            'total_marks_max': data.get('total_marks_max'),
            'edited_at': datetime.utcnow(),
            'edited_by': session['teacher_id']
        }
        
        update_data = {
            'teacher_feedback': teacher_feedback,
            'final_marks': data.get('total_marks'),
            'updated_at': datetime.utcnow()
        }
        
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': update_data}
        )
        
        # Upload feedback PDF to Google Drive if configured
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        student = Student.find_one({'student_id': submission['student_id']})
        
        if teacher and teacher.get('google_drive_folder_id') and assignment.get('drive_folders', {}).get('submissions_folder_id'):
            try:
                from utils.pdf_generator import generate_rubric_review_pdf
                from utils.google_drive import upload_student_submission
                
                # Generate PDF with student work and feedback
                pdf_content = generate_rubric_review_pdf(submission, assignment, student, teacher)
                if pdf_content:
                    # Upload to submissions folder
                    drive_result = upload_student_submission(
                        teacher=teacher,
                        submissions_folder_id=assignment['drive_folders']['submissions_folder_id'],
                        submission_content=pdf_content,
                        filename=f"feedback_{submission_id}.pdf",
                        student_name=student.get('name'),
                        student_id=student.get('student_id')
                    )
                    if drive_result:
                        Submission.update_one(
                            {'submission_id': submission_id},
                            {'$set': {'feedback_drive_file': drive_result}}
                        )
                        logger.info(f"Uploaded rubric feedback PDF for {submission_id} to Google Drive")
            except Exception as drive_error:
                logger.warning(f"Could not upload feedback PDF to Drive: {drive_error}")
        
        return jsonify({'success': True, 'message': 'Rubric feedback saved'})
        
    except Exception as e:
        logger.error(f"Error saving rubric feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/send-rubric', methods=['POST'])
@limiter.limit("200 per hour")
@teacher_required
def send_rubric_feedback_to_student(submission_id):
    """Send rubric-based feedback to student via Telegram"""
    try:
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        student = Student.find_one({'student_id': submission['student_id']})
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        
        # Update status and mark feedback as sent (no answer key option for rubric-based)
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'status': 'reviewed',
                'feedback_sent': True,
                'reviewed_at': datetime.utcnow()
            }}
        )
        submission_after = Submission.find_one({'submission_id': submission_id})
        if submission_after:
            _update_profile_and_mastery_from_assignment(submission['student_id'], assignment, submission_after)
        
        # Send Telegram notification if student has linked account
        if student and student.get('telegram_id'):
            try:
                from telegram import Bot
                bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
                if bot_token:
                    import asyncio
                    
                    async def send_notification():
                        bot = Bot(token=bot_token)
                        
                        # Format feedback message for essay
                        feedback = submission.get('teacher_feedback', {})
                        marks = submission.get('final_marks', 'N/A')
                        total = assignment.get('total_marks', 100)
                        
                        message = (
                            f"📬 *Essay Feedback*\n\n"
                            f"📝 {assignment.get('title')}\n"
                            f"📊 Total Marks: *{marks}/{total}*\n\n"
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
        
        # Get next pending submission (same assignment only)
        next_submission = get_next_pending_submission(
            session['teacher_id'], submission_id, assignment_id=submission['assignment_id']
        )
        
        return jsonify({
            'success': True, 
            'message': 'Essay feedback sent to student',
            'next_submission_id': next_submission['submission_id'] if next_submission else None,
            'next_submission_url': f"/teacher/review/{next_submission['submission_id']}" if next_submission else None
        })
        
    except Exception as e:
        logger.error(f"Error sending rubric feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/reject', methods=['POST'])
@teacher_required
def reject_submission(submission_id):
    """Reject a submission and notify the student to resubmit"""
    try:
        data = request.get_json()
        rejection_reason = data.get('reason', 'Your submission has been rejected. Please resubmit.')
        
        submission = Submission.find_one({'submission_id': submission_id})
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        
        assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
        if not assignment or assignment['teacher_id'] != session['teacher_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        student = Student.find_one({'student_id': submission['student_id']})
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        
        # Update submission status to rejected
        Submission.update_one(
            {'submission_id': submission_id},
            {'$set': {
                'status': 'rejected',
                'rejection_reason': rejection_reason,
                'rejected_at': datetime.utcnow(),
                'rejected_by': session['teacher_id']
            }}
        )
        
        # Student will see the rejection on their dashboard when they next log in
        
        return jsonify({'success': True, 'message': 'Submission rejected'})
        
    except Exception as e:
        logger.error(f"Error rejecting submission: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/pdf-rubric')
@teacher_required
def download_rubric_feedback_pdf(submission_id):
    """Generate and download PDF feedback report for rubric-based essays"""
    from utils.pdf_generator import generate_rubric_review_pdf
    
    submission = Submission.find_one({'submission_id': submission_id})
    if not submission:
        return 'Not found', 404
    
    assignment = Assignment.find_one({'assignment_id': submission['assignment_id']})
    if not assignment or assignment['teacher_id'] != session['teacher_id']:
        return 'Unauthorized', 403
    
    student = Student.find_one({'student_id': submission['student_id']})
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    
    try:
        pdf_content = generate_rubric_review_pdf(submission, assignment, student, teacher)
        
        filename = f"essay_feedback_{student['student_id']}_{assignment['assignment_id']}.pdf"
        
        return Response(
            pdf_content,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error(f"Error generating rubric PDF: {e}")
        return f'Error generating PDF: {str(e)}', 500

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
            
            # Update name
            if data.get('name') and data['name'].strip():
                update_data['name'] = data['name'].strip()
            
            # Update AI API keys
            if data.get('anthropic_api_key'):
                encrypted = encrypt_api_key(data['anthropic_api_key'])
                if encrypted:
                    update_data['anthropic_api_key'] = encrypted
            
            if data.get('openai_api_key'):
                encrypted = encrypt_api_key(data['openai_api_key'])
                if encrypted:
                    update_data['openai_api_key'] = encrypted
            
            if data.get('deepseek_api_key'):
                encrypted = encrypt_api_key(data['deepseek_api_key'])
                if encrypted:
                    update_data['deepseek_api_key'] = encrypted
            
            if data.get('google_api_key'):
                encrypted = encrypt_api_key(data['google_api_key'])
                if encrypted:
                    update_data['google_api_key'] = encrypted
            
            # Update default AI model
            if data.get('default_ai_model'):
                update_data['default_ai_model'] = data['default_ai_model']
            
            # Update Google Drive source files folder (removed submissions folder - not saving student files)
            if data.get('google_drive_source_folder_id'):
                update_data['google_drive_source_folder_id'] = data['google_drive_source_folder_id']
            elif 'google_drive_source_folder_id' in data:
                update_data['google_drive_source_folder_id'] = None
            
            # Update subjects
            if data.get('subjects'):
                subjects = [s.strip() for s in data['subjects'].split(',') if s.strip()]
                update_data['subjects'] = subjects
            elif 'subjects' in data:
                update_data['subjects'] = []
            
            # Update messaging hours settings
            update_data['messaging_hours_enabled'] = 'messaging_hours_enabled' in data
            if data.get('messaging_start_time'):
                update_data['messaging_start_time'] = data['messaging_start_time']
            if data.get('messaging_end_time'):
                update_data['messaging_end_time'] = data['messaging_end_time']
            if data.get('outside_hours_message'):
                update_data['outside_hours_message'] = data['outside_hours_message'].strip()
            
            if update_data:
                update_data['updated_at'] = datetime.utcnow()
                Teacher.update_one(
                    {'teacher_id': session['teacher_id']},
                    {'$set': update_data}
                )
                
                # Refresh teacher data
                teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
            
            classes = list(db.db.classes.find())
            all_students = list(Student.find({}).sort('name', 1))
            my_students = list(Student.find({'teachers': session['teacher_id']}).sort('name', 1))
            return render_template('teacher_settings.html',
                                 teacher=teacher,
                                 classes=classes,
                                 all_students=all_students,
                                 my_students=my_students,
                                 success='Settings updated successfully')
            
        except Exception as e:
            logger.error(f"Error updating settings: {e}")
            classes = list(db.db.classes.find())
            all_students = list(Student.find({}).sort('name', 1))
            my_students = list(Student.find({'teachers': session['teacher_id']}).sort('name', 1))
            return render_template('teacher_settings.html',
                                 teacher=teacher,
                                 classes=classes,
                                 all_students=all_students,
                                 my_students=my_students,
                                 error='Failed to update settings')
    
    classes = list(db.db.classes.find())
    all_students = list(Student.find({}).sort('name', 1))
    my_students = list(Student.find({'teachers': session['teacher_id']}).sort('name', 1))
    return render_template('teacher_settings.html', 
                         teacher=teacher, 
                         classes=classes,
                         all_students=all_students,
                         my_students=my_students)

@app.route('/teacher/change_password', methods=['POST'])
@teacher_required
def teacher_change_password():
    """Teacher changes their own password"""
    try:
        data = request.get_json()
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        if not current_password or not new_password:
            return jsonify({'error': 'Current and new passwords required'}), 400
        
        ok, err = validate_password(new_password)
        if not ok:
            return jsonify({'error': err}), 400
        
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Verify current password
        if not verify_password(current_password, teacher.get('password_hash', '')):
            return jsonify({'error': 'Current password is incorrect'}), 400
        
        # Update password
        hashed = hash_password(new_password)
        Teacher.update_one(
            {'teacher_id': session['teacher_id']},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow()}}
        )
        
        return jsonify({'success': True, 'message': 'Password changed successfully'})
        
    except Exception as e:
        logger.error(f"Error changing teacher password: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/drive/service-account-info')
@teacher_required
def get_drive_service_account_info():
    """Get Google Drive service account information for folder sharing"""
    try:
        from utils.google_drive import get_service_account_email, is_drive_configured
        import os
        
        configured = is_drive_configured()
        email = get_service_account_email() if configured else None
        
        # Debug info
        has_file_env = bool(os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE'))
        has_json_env = bool(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))
        
        debug_info = f"FILE_ENV: {has_file_env}, JSON_ENV: {has_json_env}"
        
        return jsonify({
            'configured': configured,
            'email': email,
            'message': 'Share your Google Drive folder with this email address and grant Editor access.' if email else f'Google Drive service account not configured. Debug: {debug_info}'
        })
    except Exception as e:
        logger.error(f"Error getting service account info: {e}")
        return jsonify({'error': str(e), 'configured': False}), 500

@app.route('/teacher/assign_class', methods=['POST'])
@teacher_required
def teacher_assign_class():
    """Teacher assigns a class to themselves"""
    try:
        data = request.get_json()
        class_id = data.get('class_id')
        
        if not class_id:
            return jsonify({'error': 'Class ID required'}), 400
        
        # Check if class exists
        cls = db.db.classes.find_one({'class_id': class_id})
        if not cls:
            return jsonify({'error': 'Class not found'}), 404
        
        # Add class to teacher's classes list
        Teacher.update_one(
            {'teacher_id': session['teacher_id']},
            {'$addToSet': {'classes': class_id}, '$set': {'updated_at': datetime.utcnow()}}
        )
        
        # Add teacher to all students in this class (support both 'class' and 'classes' fields)
        Student.update_many(
            {'$or': [{'class': class_id}, {'classes': class_id}]},
            {'$addToSet': {'teachers': session['teacher_id']}}
        )
        
        return jsonify({'success': True, 'message': f'Class {class_id} assigned'})
        
    except Exception as e:
        logger.error(f"Error assigning class to teacher: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/remove_class', methods=['POST'])
@teacher_required
def teacher_remove_class():
    """Teacher removes a class from themselves"""
    try:
        data = request.get_json()
        class_id = data.get('class_id')
        
        if not class_id:
            return jsonify({'error': 'Class ID required'}), 400
        
        # Remove class from teacher's classes list
        Teacher.update_one(
            {'teacher_id': session['teacher_id']},
            {'$pull': {'classes': class_id}, '$set': {'updated_at': datetime.utcnow()}}
        )
        
        # Remove teacher from students in this class (support both 'class' and 'classes' fields)
        Student.update_many(
            {'$or': [{'class': class_id}, {'classes': class_id}], 'teachers': session['teacher_id']},
            {'$pull': {'teachers': session['teacher_id']}}
        )
        
        return jsonify({'success': True, 'message': f'Class {class_id} removed'})
        
    except Exception as e:
        logger.error(f"Error removing class from teacher: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/assign_student_to_class', methods=['POST'])
@teacher_required
def teacher_assign_student_to_class():
    """Teacher assigns an existing student to one of their classes"""
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        class_id = data.get('class_id')
        
        if not student_id or not class_id:
            return jsonify({'error': 'Student ID and Class ID required'}), 400
        
        # Verify teacher has this class
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        if not teacher or class_id not in teacher.get('classes', []):
            return jsonify({'error': 'You are not assigned to this class'}), 403
        
        # Check if student exists
        student = Student.find_one({'student_id': student_id})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Add class to student's classes array and add teacher
        Student.update_one(
            {'student_id': student_id},
            {
                '$addToSet': {
                    'classes': class_id,
                    'teachers': session['teacher_id']
                },
                '$set': {'updated_at': datetime.utcnow()}
            }
        )
        
        return jsonify({
            'success': True, 
            'message': f'Student {student.get("name")} assigned to class {class_id} and linked to you'
        })
        
    except Exception as e:
        logger.error(f"Error assigning student to class: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/remove_student', methods=['POST'])
@teacher_required
def teacher_remove_student():
    """Teacher removes a student from their list (unlinks teacher from student)"""
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        
        if not student_id:
            return jsonify({'error': 'Student ID required'}), 400
        
        # Remove this teacher from the student's teachers list
        Student.update_one(
            {'student_id': student_id},
            {
                '$pull': {'teachers': session['teacher_id']},
                '$set': {'updated_at': datetime.utcnow()}
            }
        )
        
        return jsonify({'success': True, 'message': f'Student {student_id} removed from your list'})
        
    except Exception as e:
        logger.error(f"Error removing student: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# MODULE ACCESS - Which teachers/classes can use learning modules (admin-set)
# ============================================================================

MODULE_ACCESS_CONFIG_ID = 'default'

def _get_module_access_config():
    """Return { teacher_ids: [], class_ids: [] } from admin allocation."""
    doc = db.db.module_access.find_one({'config_id': MODULE_ACCESS_CONFIG_ID})
    if not doc:
        return {'teacher_ids': [], 'class_ids': []}
    return {
        'teacher_ids': list(doc.get('teacher_ids') or []),
        'class_ids': list(doc.get('class_ids') or []),
    }

def _save_module_access_config(teacher_ids, class_ids):
    """Save which teachers and classes have access to learning modules."""
    db.db.module_access.update_one(
        {'config_id': MODULE_ACCESS_CONFIG_ID},
        {'$set': {
            'config_id': MODULE_ACCESS_CONFIG_ID,
            'teacher_ids': list(teacher_ids or []),
            'class_ids': list(class_ids or []),
            'updated_at': datetime.utcnow(),
        }},
        upsert=True,
    )

def _teacher_has_module_access(teacher_id):
    """True if this teacher is allocated access to create/manage learning modules."""
    config = _get_module_access_config()
    return teacher_id in config['teacher_ids']

def _student_has_module_access(student_id):
    """True if this student's class(es) are allocated access to learning modules."""
    student = Student.find_one({'student_id': student_id})
    if not student:
        return False
    config = _get_module_access_config()
    if not config['class_ids']:
        return False
    student_classes = student.get('classes', [])
    if not student_classes and student.get('class'):
        student_classes = [student['class']]
    return bool(set(student_classes) & set(config['class_ids']))


# ============================================================================
# PYTHON LAB ACCESS - Which classes/teaching groups can use Python Lab (admin-set)
# ============================================================================

PYTHON_LAB_ACCESS_CONFIG_ID = 'default'

def _get_python_lab_access_config():
    """Return { teacher_ids: [], class_ids: [], teaching_group_ids: [] } from admin allocation."""
    doc = db.db.python_lab_access.find_one({'config_id': PYTHON_LAB_ACCESS_CONFIG_ID})
    if not doc:
        return {'teacher_ids': [], 'class_ids': [], 'teaching_group_ids': []}
    return {
        'teacher_ids': list(doc.get('teacher_ids') or []),
        'class_ids': list(doc.get('class_ids') or []),
        'teaching_group_ids': list(doc.get('teaching_group_ids') or []),
    }

def _save_python_lab_access_config(teacher_ids, class_ids, teaching_group_ids):
    """Save which teachers, classes and teaching groups have access to Python Lab."""
    db.db.python_lab_access.update_one(
        {'config_id': PYTHON_LAB_ACCESS_CONFIG_ID},
        {'$set': {
            'config_id': PYTHON_LAB_ACCESS_CONFIG_ID,
            'teacher_ids': list(teacher_ids or []),
            'class_ids': list(class_ids or []),
            'teaching_group_ids': list(teaching_group_ids or []),
            'updated_at': datetime.utcnow(),
        }},
        upsert=True,
    )

def _teacher_has_python_lab_access(teacher_id):
    """True if this teacher is allocated access to Python Lab (admin-set)."""
    config = _get_python_lab_access_config()
    return teacher_id in (config.get('teacher_ids') or [])

def _student_has_python_lab_access(student_id):
    """True if this student is in an allowed class OR in an allowed teaching group."""
    config = _get_python_lab_access_config()
    student = Student.find_one({'student_id': student_id})
    if not student:
        return False
    # Check class access
    student_classes = student.get('classes', [])
    if not student_classes and student.get('class'):
        student_classes = [student.get('class')]
    if config['class_ids'] and set(student_classes) & set(config['class_ids']):
        return True
    # Check teaching group access
    if config['teaching_group_ids']:
        for gid in config['teaching_group_ids']:
            group = TeachingGroup.find_one({'group_id': gid})
            if group and student_id in group.get('student_ids', []):
                return True
    return False


@app.context_processor
def inject_module_access():
    """Make teacher_has_module_access, student_has_module_access, student_has_python_lab_access, teacher_has_python_lab_access available in all templates."""
    out = {'teacher_has_module_access': False, 'student_has_module_access': False, 'student_has_python_lab_access': False, 'teacher_has_python_lab_access': False}
    if session.get('teacher_id'):
        out['teacher_has_module_access'] = _teacher_has_module_access(session['teacher_id'])
        out['teacher_has_python_lab_access'] = _teacher_has_python_lab_access(session['teacher_id'])
    if session.get('student_id'):
        out['student_has_module_access'] = _student_has_module_access(session['student_id'])
        out['student_has_python_lab_access'] = _student_has_python_lab_access(session['student_id'])
    return out


# ============================================================================
# MY MODULES - HELPERS
# ============================================================================

def _generate_module_id():
    return f"MOD-{uuid.uuid4().hex[:8].upper()}"

def _generate_resource_id():
    return f"RES-{uuid.uuid4().hex[:8].upper()}"

def _generate_session_id():
    return f"SES-{uuid.uuid4().hex[:8].upper()}"

def _get_all_module_ids_in_tree(root_module_id):
    """Collect module_id and all descendant IDs for a root module."""
    ids = [root_module_id]
    root = Module.find_one({'module_id': root_module_id})
    if not root:
        return ids
    child_ids = root.get('children_ids', [])
    for cid in child_ids:
        ids.extend(_get_all_module_ids_in_tree(cid))
    return ids

def _save_module_tree(node, teacher_id, subject, year_level, parent_id=None, depth=0):
    """Recursively save module tree to database. Returns module_id."""
    module_id = _generate_module_id()
    children = node.pop('children', [])
    is_leaf = len(children) == 0

    position = _calculate_module_position(depth, len(children), parent_id)

    module_doc = {
        'module_id': module_id,
        'teacher_id': teacher_id,
        'subject': subject,
        'year_level': year_level,
        'title': node.get('title', 'Untitled'),
        'description': node.get('description', ''),
        'parent_id': parent_id,
        'children_ids': [],
        'depth': depth,
        'is_leaf': is_leaf,
        'position': position,
        'color': node.get('color', '#667eea'),
        'icon': node.get('icon', 'bi-book'),
        'learning_objectives': node.get('learning_objectives', []),
        'estimated_hours': node.get('estimated_hours', 0),
        'created_at': datetime.utcnow(),
        'updated_at': datetime.utcnow(),
        'status': 'draft',
    }
    Module.insert_one(module_doc)

    children_ids = []
    for i, child in enumerate(children):
        child_id = _save_module_tree(child, teacher_id, subject, year_level, parent_id=module_id, depth=depth + 1)
        children_ids.append(child_id)

    if children_ids:
        Module.update_one({'module_id': module_id}, {'$set': {'children_ids': children_ids}})
    return module_id

def _calculate_module_position(depth, sibling_count, parent_id):
    """Calculate 3D position for module visualization."""
    if depth == 0:
        return {'x': 0, 'y': 0, 'z': 0, 'angle': 0, 'distance': 0}
    parent = Module.find_one({'module_id': parent_id}) if parent_id else None
    parent_pos = parent.get('position', {'x': 0, 'y': 0, 'z': 0}) if parent else {'x': 0, 'y': 0, 'z': 0}
    base_distance = 100 * depth
    return {
        'x': parent_pos.get('x', 0),
        'y': depth * 30,
        'z': parent_pos.get('z', 0),
        'angle': 0,
        'distance': base_distance,
    }

def _update_student_mastery(student_id, module_id, change):
    """Update student's mastery score and propagate to parents."""
    current = StudentModuleMastery.find_one({'student_id': student_id, 'module_id': module_id})
    current_score = current.get('mastery_score', 0) if current else 0
    new_score = max(0, min(100, round(current_score + change)))

    if new_score >= 100:
        status = 'mastered'
    elif new_score > 0:
        status = 'in_progress'
    else:
        status = 'not_started'

    StudentModuleMastery.update_one(
        {'student_id': student_id, 'module_id': module_id},
        {
            '$set': {
                'mastery_score': new_score,
                'status': status,
                'updated_at': datetime.utcnow(),
                'last_activity': datetime.utcnow(),
            },
            '$inc': {'time_spent_minutes': 1},
        },
        upsert=True,
    )

    module = Module.find_one({'module_id': module_id})
    if module and module.get('parent_id'):
        _propagate_mastery_to_parent(student_id, module['parent_id'])

def _propagate_mastery_to_parent(student_id, parent_module_id):
    """Recalculate parent module mastery from children (min of children)."""
    parent = Module.find_one({'module_id': parent_module_id})
    if not parent:
        return
    children_ids = parent.get('children_ids', [])
    if not children_ids:
        return
    children_scores = []
    for cid in children_ids:
        cm = StudentModuleMastery.find_one({'student_id': student_id, 'module_id': cid})
        children_scores.append(cm.get('mastery_score', 0) if cm else 0)
    parent_score = min(children_scores) if children_scores else 0
    if parent_score >= 100:
        status = 'mastered'
    elif parent_score > 0 or any(s > 0 for s in children_scores):
        status = 'in_progress'
    else:
        status = 'not_started'
    StudentModuleMastery.update_one(
        {'student_id': student_id, 'module_id': parent_module_id},
        {
            '$set': {
                'mastery_score': parent_score,
                'status': status,
                'updated_at': datetime.utcnow(),
            }
        },
        upsert=True,
    )
    if parent.get('parent_id'):
        _propagate_mastery_to_parent(student_id, parent['parent_id'])

def _calculate_tree_mastery(root_module_id, student_id):
    """Overall mastery for a module tree (root node mastery)."""
    m = StudentModuleMastery.find_one({'student_id': student_id, 'module_id': root_module_id})
    return m.get('mastery_score', 0) if m else 0

def _update_profile_and_mastery_from_assignment(student_id, assignment, submission):
    """
    When an assignment is linked to a module and feedback has been sent, update the student's
    module mastery and learning profile from the assignment score. Builds profile from every assignment.
    """
    linked_module_id = assignment.get('linked_module_id')
    if not linked_module_id:
        return
    total_marks = assignment.get('total_marks') or 100
    if total_marks <= 0:
        return
    display_marks, percentage = _submission_display_marks(submission, total_marks)
    if display_marks is None and percentage <= 0:
        return
    try:
        # Update module mastery for the linked (root) module: set to assignment score %
        score_int = round(percentage)
        StudentModuleMastery.update_one(
            {'student_id': student_id, 'module_id': linked_module_id},
            {
                '$set': {
                    'mastery_score': min(100, max(0, score_int)),
                    'status': 'mastered' if score_int >= 100 else ('in_progress' if score_int > 0 else 'not_started'),
                    'updated_at': datetime.utcnow(),
                    'last_activity': datetime.utcnow(),
                },
                '$inc': {'time_spent_minutes': 1},
            },
            upsert=True,
        )
        # Optionally propagate to parent (root is top-level so no parent)
        module = Module.find_one({'module_id': linked_module_id})
        if module and module.get('parent_id'):
            _propagate_mastery_to_parent(student_id, module['parent_id'])

        # Update learning profile (strengths/weaknesses) by subject
        subject = assignment.get('subject') or 'General'
        topic = assignment.get('title') or (module.get('title') if module else 'Assignment')
        profile = StudentLearningProfile.find_one({'student_id': student_id, 'subject': subject})
        update_ops = {'$set': {'last_updated': datetime.utcnow()}}
        if percentage >= 80:
            entry = {'topic': topic, 'confidence': percentage / 100.0, 'recorded_at': datetime.utcnow().isoformat(), 'source': 'assignment'}
            if profile:
                update_ops.setdefault('$push', {})['strengths'] = entry
            else:
                update_ops.setdefault('$set', {})['strengths'] = [entry]
        elif percentage < 50:
            entry = {'topic': topic, 'notes': f'Assignment score {round(percentage)}%', 'recorded_at': datetime.utcnow().isoformat(), 'source': 'assignment'}
            if profile:
                update_ops.setdefault('$push', {})['weaknesses'] = entry
            else:
                update_ops.setdefault('$set', {})['weaknesses'] = [entry]
        if '$push' in update_ops or ('$set' in update_ops and any(k in update_ops['$set'] for k in ('strengths', 'weaknesses'))):
            StudentLearningProfile.update_one(
                {'student_id': student_id, 'subject': subject},
                update_ops,
                upsert=True,
            )
    except Exception as e:
        logger.warning("Error updating profile/mastery from assignment: %s", e)


def _update_student_profile(student_id, subject, updates):
    """Update student's learning profile with new insights."""
    profile = StudentLearningProfile.find_one({'student_id': student_id, 'subject': subject})
    update_ops = {'$set': {'last_updated': datetime.utcnow()}}
    if updates.get('new_strength'):
        st = updates['new_strength']
        if isinstance(st, dict):
            if profile:
                update_ops.setdefault('$push', {})['strengths'] = st
            else:
                update_ops.setdefault('$set', {})['strengths'] = [st]
    if updates.get('new_weakness'):
        w = updates['new_weakness']
        if isinstance(w, dict):
            if profile:
                update_ops.setdefault('$push', {})['weaknesses'] = w
            else:
                update_ops.setdefault('$set', {})['weaknesses'] = [w]
    if updates.get('new_mistake_pattern'):
        pat = updates['new_mistake_pattern']
        if isinstance(pat, str):
            entry = {'pattern': pat, 'frequency': 1}
            if profile:
                update_ops.setdefault('$push', {})['common_mistakes'] = entry
            else:
                update_ops.setdefault('$set', {})['common_mistakes'] = [entry]
    StudentLearningProfile.update_one(
        {'student_id': student_id, 'subject': subject},
        update_ops,
        upsert=True,
    )


# ============================================================================
# MY MODULES - TEACHER ROUTES
# ============================================================================

@app.route('/teacher/modules')
@teacher_required
def teacher_modules():
    """List all module trees created by this teacher."""
    if not _teacher_has_module_access(session['teacher_id']):
        return redirect(url_for('teacher_dashboard'))
    root_modules = list(
        Module.find({'teacher_id': session['teacher_id'], 'parent_id': None}).sort('created_at', -1)
    )
    for module in root_modules:
        module['total_modules'] = len(_get_all_module_ids_in_tree(module['module_id']))
        tb = ModuleTextbook.find_one({'module_id': module['module_id']})
        module['has_textbook'] = bool(tb) and rag_service.textbook_has_content(module['module_id'])
        module['textbook_name'] = (tb.get('name', '') if tb else '') or ''
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    return render_template('teacher_modules.html', modules=root_modules, teacher=teacher)

@app.route('/teacher/modules/create', methods=['GET', 'POST'])
@teacher_required
def create_module():
    """Create new module tree from syllabus upload."""
    if not _teacher_has_module_access(session['teacher_id']):
        return redirect(url_for('teacher_dashboard'))
    if request.method == 'POST':
        try:
            subject = request.form.get('subject', '').strip()
            year_level = request.form.get('year_level', '').strip()
            file = request.files.get('syllabus_file')

            if not file or not subject:
                return jsonify({'error': 'Missing required fields'}), 400

            file_content = file.read()
            file_type = 'pdf' if (file.filename or '').lower().endswith('.pdf') else 'docx'

            teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
            api_key = None
            if teacher and teacher.get('anthropic_api_key'):
                try:
                    api_key = decrypt_api_key(teacher['anthropic_api_key'])
                except Exception:
                    pass
            if not api_key:
                api_key = os.getenv('ANTHROPIC_API_KEY')

            result = generate_modules_from_syllabus(
                file_content=file_content,
                file_type=file_type,
                subject=subject,
                year_level=year_level,
                teacher_id=session['teacher_id'],
                api_key=api_key,
            )

            if 'error' in result:
                return jsonify({'error': result['error']}), 500

            root_node = result.get('root')
            if not root_node:
                return jsonify({'error': 'No module structure generated'}), 500

            root_module_id = _save_module_tree(
                root_node, session['teacher_id'], subject, year_level
            )
            return jsonify({
                'success': True,
                'module_id': root_module_id,
                'total_modules': result.get('total_modules', 0),
            })
        except Exception as e:
            logger.error("Error creating module: %s", e)
            return jsonify({'error': str(e)}), 500

    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    return render_template('teacher_create_module.html', teacher=teacher)

@app.route('/teacher/modules/<module_id>')
@teacher_required
def view_module(module_id):
    """View and edit module tree in 3D space."""
    if not _teacher_has_module_access(session['teacher_id']):
        return redirect(url_for('teacher_dashboard'))
    root_module = Module.find_one(
        {'module_id': module_id, 'teacher_id': session['teacher_id']}
    )
    if not root_module:
        return redirect(url_for('teacher_modules'))

    def build_tree(m):
        m = dict(m)
        m['children'] = [
            build_tree(Module.find_one({'module_id': cid}))
            for cid in m.get('children_ids', [])
            if Module.find_one({'module_id': cid})
        ]
        return m

    module_tree = build_tree(root_module)
    return render_template(
        'teacher_module_view.html',
        module=root_module,
        module_tree=module_tree,
        modules_json=json.dumps(module_tree, default=str),
    )

@app.route('/teacher/modules/<module_id>/node/<node_id>', methods=['PATCH', 'PUT'])
@teacher_required
def update_module_node(module_id, node_id):
    """Update a module node: title, description, learning_objectives, custom_prompt."""
    if not _teacher_has_module_access(session['teacher_id']):
        return jsonify({'error': 'Access denied'}), 403
    root = Module.find_one({'module_id': module_id, 'teacher_id': session['teacher_id']})
    if not root:
        return jsonify({'error': 'Module tree not found'}), 404
    mod = Module.find_one({'module_id': node_id, 'teacher_id': session['teacher_id']})
    if not mod:
        return jsonify({'error': 'Module node not found'}), 404
    try:
        data = request.get_json() or {}
        update = {'updated_at': datetime.utcnow()}
        if 'title' in data:
            update['title'] = (data.get('title') or '').strip() or mod.get('title', 'Untitled')
        if 'description' in data:
            update['description'] = data.get('description', '')
        if 'learning_objectives' in data:
            objs = data.get('learning_objectives')
            update['learning_objectives'] = objs if isinstance(objs, list) else []
        if 'custom_prompt' in data:
            update['custom_prompt'] = (data.get('custom_prompt') or '').strip()
        Module.update_one({'module_id': node_id, 'teacher_id': session['teacher_id']}, {'$set': update})
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Error updating module node: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/teacher/modules/<module_id>/node/<node_id>/resources', methods=['GET', 'POST'])
@teacher_required
def manage_module_resources(module_id, node_id):
    """Add/edit resources for a leaf module."""
    if not _teacher_has_module_access(session['teacher_id']):
        return jsonify({'error': 'Access denied'}), 403
    mod = Module.find_one({'module_id': node_id, 'teacher_id': session['teacher_id']})
    if not mod:
        return jsonify({'error': 'Module not found'}), 404

    if request.method == 'POST':
        try:
            resource_type = request.form.get('type', 'link')
            title = request.form.get('title', '').strip()
            resource_doc = {
                'resource_id': _generate_resource_id(),
                'module_id': node_id,
                'teacher_id': session['teacher_id'],
                'type': resource_type,
                'title': title,
                'description': request.form.get('description', ''),
                'order': ModuleResource.count({'module_id': node_id}) + 1,
                'created_at': datetime.utcnow(),
            }
            if resource_type == 'youtube':
                resource_doc['url'] = request.form.get('url', '')
            elif resource_type == 'pdf':
                # PDF: link (URL) and/or file upload (max 10 MB)
                resource_doc['url'] = request.form.get('url', '')
                f = request.files.get('file')
                if f and f.filename:
                    raw = f.read()
                    if len(raw) > 10 * 1024 * 1024:  # 10 MB
                        return jsonify({'error': 'PDF must be 10 MB or smaller'}), 400
                    resource_doc['content'] = base64.b64encode(raw).decode('utf-8')
                if not resource_doc.get('url') and not resource_doc.get('content'):
                    return jsonify({'error': 'Provide a PDF link (URL) or upload a PDF file'}), 400
            elif resource_type in ('link', 'slides'):
                resource_doc['url'] = request.form.get('url', '')
            ModuleResource.insert_one(resource_doc)
            return jsonify({'success': True, 'resource_id': resource_doc['resource_id']})
        except Exception as e:
            logger.error("Error adding resource: %s", e)
            return jsonify({'error': str(e)}), 500

    raw = list(ModuleResource.find({'module_id': node_id}).sort('order', 1))
    resources = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        doc = {
            'resource_id': r.get('resource_id'),
            'module_id': r.get('module_id'),
            'type': r.get('type', 'link'),
            'title': r.get('title', ''),
            'url': r.get('url', ''),
            'description': r.get('description', ''),
            'order': r.get('order', 0),
        }
        if r.get('created_at'):
            doc['created_at'] = r['created_at'].isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at'])
        resources.append(doc)
    return jsonify({'resources': resources})


@app.route('/teacher/modules/<module_id>/node/<node_id>/resources/<resource_id>', methods=['DELETE'])
@teacher_required
def delete_module_resource(module_id, node_id, resource_id):
    """Delete a resource from a leaf module."""
    if not _teacher_has_module_access(session['teacher_id']):
        return jsonify({'error': 'Access denied'}), 403
    mod = Module.find_one({'module_id': node_id, 'teacher_id': session['teacher_id']})
    if not mod:
        return jsonify({'error': 'Module not found'}), 404
    res = ModuleResource.find_one({'resource_id': resource_id, 'module_id': node_id})
    if not res:
        return jsonify({'error': 'Resource not found'}), 404
    ModuleResource.delete_one({'resource_id': resource_id, 'module_id': node_id})
    return jsonify({'success': True})


@app.route('/teacher/modules/<module_id>/delete', methods=['POST', 'DELETE'])
@teacher_required
def delete_module_tree(module_id):
    """Delete an entire module tree (root and all nodes, resources, mastery, sessions)."""
    if not _teacher_has_module_access(session['teacher_id']):
        return jsonify({'error': 'Access denied'}), 403
    root = Module.find_one({'module_id': module_id, 'teacher_id': session['teacher_id']})
    if not root:
        return jsonify({'error': 'Module not found'}), 404
    try:
        tree_ids = _get_all_module_ids_in_tree(module_id)
        ModuleResource.delete_many({'module_id': {'$in': tree_ids}})
        StudentModuleMastery.delete_many({'module_id': {'$in': tree_ids}})
        LearningSession.delete_many({'module_id': {'$in': tree_ids}})
        Module.delete_many({'module_id': {'$in': tree_ids}})
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Error deleting module tree: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/teacher/modules/<module_id>/publish', methods=['POST'])
@teacher_required
def publish_module(module_id):
    """Publish a module tree for students."""
    if not _teacher_has_module_access(session['teacher_id']):
        return jsonify({'error': 'Access denied'}), 403
    root = Module.find_one({'module_id': module_id, 'teacher_id': session['teacher_id']})
    if not root:
        return jsonify({'error': 'Module not found'}), 404
    ids = _get_all_module_ids_in_tree(module_id)
    for mid in ids:
        Module.update_one({'module_id': mid}, {'$set': {'status': 'published', 'updated_at': datetime.utcnow()}})
    return jsonify({'success': True})


@app.route('/teacher/modules/<module_id>/textbook', methods=['GET', 'POST', 'DELETE'])
@teacher_required
def module_textbook(module_id):
    """Get textbook status, upload a textbook PDF for RAG, or delete the textbook."""
    if not _teacher_has_module_access(session['teacher_id']):
        return jsonify({'error': 'Access denied'}), 403
    root = Module.find_one({'module_id': module_id, 'teacher_id': session['teacher_id']})
    if not root:
        return jsonify({'error': 'Module not found'}), 404

    if request.method == 'GET':
        doc = ModuleTextbook.find_one({'module_id': module_id})
        has_content = rag_service.textbook_has_content(module_id)
        return jsonify({
            'has_textbook': bool(doc) and has_content,
            'name': doc.get('name', '') if doc else '',
            'file_name': doc.get('file_name', '') if doc else '',
            'chunk_count': doc.get('chunk_count', 0) if doc else 0,
            'upload_count': doc.get('upload_count', 0) if doc else 0,
            'updated_at': doc.get('updated_at').isoformat() if doc and doc.get('updated_at') else None,
        })

    if request.method == 'DELETE':
        rag_service.delete_textbook(module_id)
        ModuleTextbook.delete_one({'module_id': module_id})
        return jsonify({'success': True})

    # POST: upload PDF
    if 'textbook_file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['textbook_file']
    if not f or not f.filename or not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Please upload a PDF file'}), 400
    title = (request.form.get('title') or f.filename or 'Textbook').strip()[:200]
    try:
        pdf_bytes = f.read()
        if len(pdf_bytes) < 100:
            return jsonify({'error': 'File is too small or empty'}), 400
        # Limit size to reduce OOM risk on memory-constrained deploys (e.g. Railway)
        max_textbook_mb = 15
        if len(pdf_bytes) > max_textbook_mb * 1024 * 1024:
            return jsonify({'error': f'PDF must be {max_textbook_mb} MB or smaller. Upload chapters separately.'}), 400
        result = rag_service.ingest_textbook(module_id, pdf_bytes, title=title, append=True)
        if not result.get('success'):
            return jsonify({'error': result.get('error', 'Ingest failed')}), 500
        total_chunks = result.get('total_chunk_count', result.get('chunk_count', 0))
        ModuleTextbook.update_one(
            {'module_id': module_id},
            {
                '$set': {
                    'module_id': module_id,
                    'name': title,
                    'file_name': f.filename,
                    'chunk_count': total_chunks,
                    'updated_at': datetime.utcnow(),
                },
                '$inc': {'upload_count': 1},
            },
            upsert=True,
        )
        return jsonify({
            'success': True,
            'chunk_count': result.get('chunk_count', 0),
            'total_chunk_count': total_chunks,
            'name': title,
        })
    except Exception as e:
        logger.exception("Error uploading textbook: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/teacher/modules/<module_id>/mastery')
@teacher_required
def module_class_mastery(module_id):
    """View class mastery overview for a module tree."""
    if not _teacher_has_module_access(session['teacher_id']):
        return redirect(url_for('teacher_dashboard'))
    root_module = Module.find_one(
        {'module_id': module_id, 'teacher_id': session['teacher_id']}
    )
    if not root_module:
        return redirect(url_for('teacher_modules'))

    tree_ids = _get_all_module_ids_in_tree(module_id)
    pipeline = [
        {'$match': {'module_id': {'$in': tree_ids}}},
        {
            '$group': {
                '_id': '$student_id',
                'avg_mastery': {'$avg': '$mastery_score'},
                'modules_started': {'$sum': 1},
                'total_time': {'$sum': '$time_spent_minutes'},
            },
        },
    ]
    student_mastery = list(StudentModuleMastery.aggregate(pipeline))
    for sm in student_mastery:
        student = Student.find_one({'student_id': sm['_id']})
        if student:
            sm['name'] = student.get('name', 'Unknown')
            sm['class'] = student.get('class', '')
    return render_template(
        'teacher_module_mastery.html',
        module=root_module,
        student_mastery=student_mastery,
    )


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
    
    # Get teacher's classes for initiating new conversations
    teacher_classes = set(teacher.get('classes', []))
    teacher_student_ids = set()
    
    # Find students who have this teacher assigned
    students_with_teacher = list(Student.find({'teachers': session['teacher_id']}))
    for student in students_with_teacher:
        teacher_student_ids.add(student.get('student_id'))
        if student.get('class'):
            teacher_classes.add(student.get('class'))
    
    # Also find students who have submissions to this teacher's assignments
    assignments = list(Assignment.find({'teacher_id': session['teacher_id']}))
    assignment_ids = [a['assignment_id'] for a in assignments]
    if assignment_ids:
        submission_student_ids = Submission.find({
            'assignment_id': {'$in': assignment_ids}
        }).distinct('student_id')
        
        for student_id in submission_student_ids:
            teacher_student_ids.add(student_id)
            student = Student.find_one({'student_id': student_id})
            if student and student.get('class'):
                teacher_classes.add(student.get('class'))
    
    # Get classes data
    classes_data = []
    for class_id in sorted(teacher_classes):
        class_info = Class.find_one({'class_id': class_id}) or {'class_id': class_id}
        classes_data.append({
            'class_id': class_id,
            'name': class_info.get('name', class_id)
        })
    
    # Get teaching groups
    teaching_groups = list(TeachingGroup.find({'teacher_id': session['teacher_id']}))
    for group in teaching_groups:
        group['name'] = group.get('name', group.get('group_id', 'Unknown'))
    
    return render_template('teacher_messages.html',
                         teacher=teacher,
                         students=students,
                         classes=classes_data,
                         teaching_groups=teaching_groups)

@app.route('/teacher/messages/<student_id>')
@teacher_required
def teacher_chat(student_id):
    """Chat with a specific student"""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    student = Student.find_one({'student_id': student_id})
    
    if not student:
        return redirect(url_for('teacher_messages'))
    
    # Verify teacher can message this student
    # Check if student is assigned to teacher or has submissions to teacher's assignments
    can_message = False
    
    # Check if student is assigned to teacher
    if session['teacher_id'] in student.get('teachers', []):
        can_message = True
    
    # Check if student has submissions to teacher's assignments
    if not can_message:
        assignments = list(Assignment.find({'teacher_id': session['teacher_id']}))
        assignment_ids = [a['assignment_id'] for a in assignments]
        if assignment_ids:
            has_submission = Submission.count({
                'student_id': student_id,
                'assignment_id': {'$in': assignment_ids}
            }) > 0
            if has_submission:
                can_message = True
    
    if not can_message:
        # Redirect with error message
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

@app.route('/teacher/messages/<student_id>/purge', methods=['POST'])
@teacher_required
def teacher_purge_conversation(student_id):
    """Delete all messages with a student"""
    try:
        # Verify student exists
        student = Student.find_one({'student_id': student_id})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Delete all messages between this teacher and student
        result = db.db.messages.delete_many({
            'student_id': student_id,
            'teacher_id': session['teacher_id']
        })
        
        return jsonify({
            'success': True,
            'deleted': result.deleted_count,
            'message': f'Deleted {result.deleted_count} messages'
        })
        
    except Exception as e:
        logger.error(f"Error purging conversation: {e}")
        return jsonify({'error': str(e)}), 500

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

@app.route('/api/teacher/drive/files', methods=['GET'])
@teacher_required
def list_drive_files():
    """List files from teacher's Google Drive source folder"""
    try:
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        if not teacher:
            return jsonify({'success': False, 'error': 'Teacher not found'}), 404
        
        source_folder_id = teacher.get('google_drive_source_folder_id')
        if not source_folder_id:
            return jsonify({'success': False, 'error': 'Source folder not configured. Please set it in Settings.'}), 400
        
        from utils.google_drive import get_drive_service, DriveManager
        service = get_drive_service()
        if not service:
            return jsonify({'success': False, 'error': 'Google Drive service not configured. Please contact administrator.'}), 500
        
        manager = DriveManager(service)
        
        # First verify we have access to the folder
        has_access, error_msg = manager.verify_folder_access(folder_id=source_folder_id)
        if not has_access:
            logger.error(f"Folder access verification failed: {error_msg}")
            return jsonify({
                'success': False,
                'error': error_msg
            }), 403
        
        # List PDFs, Google Docs, and Google Sheets
        mime_types = [
            'application/pdf',
            'application/vnd.google-apps.document',
            'application/vnd.google-apps.spreadsheet'
        ]
        
        logger.info(f"Listing files from folder {source_folder_id} for teacher {session['teacher_id']}")
        files = manager.list_files(folder_id=source_folder_id, mime_types=mime_types)
        logger.info(f"Found {len(files)} files in folder")
        
        # Format files for frontend
        formatted_files = []
        for file in files:
            mime_type = file.get('mimeType', '')
            file_type = 'pdf'
            if mime_type == 'application/vnd.google-apps.document':
                file_type = 'doc'
            elif mime_type == 'application/vnd.google-apps.spreadsheet':
                file_type = 'sheet'
            
            formatted_files.append({
                'id': file.get('id'),
                'name': file.get('name'),
                'type': file_type,
                'mimeType': mime_type,
                'size': file.get('size'),
                'modifiedTime': file.get('modifiedTime')
            })
        
        return jsonify({
            'success': True,
            'files': formatted_files,
            'count': len(formatted_files)
        })
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error listing Drive files: {error_msg}", exc_info=True)
        # Provide more helpful error messages
        if 'insufficient permissions' in error_msg.lower() or 'permission denied' in error_msg.lower():
            return jsonify({
                'success': False, 
                'error': 'Permission denied. Please ensure the folder is shared with the service account email with Editor access.'
            }), 403
        elif 'not found' in error_msg.lower():
            return jsonify({
                'success': False,
                'error': 'Folder not found. Please check the folder ID in Settings.'
            }), 404
        else:
            return jsonify({
                'success': False,
                'error': f'Error loading files: {error_msg}'
            }), 500

@app.route('/api/teacher/drive/test-folder-access', methods=['GET'])
@teacher_required
def test_folder_access():
    """Test access to the configured source folder - for debugging"""
    try:
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        if not teacher:
            return jsonify({'success': False, 'error': 'Teacher not found'}), 404
        
        source_folder_id = teacher.get('google_drive_source_folder_id')
        if not source_folder_id:
            return jsonify({
                'success': False,
                'error': 'Source folder not configured',
                'details': 'Please set the Source Files Folder ID in Settings'
            }), 400
        
        from utils.google_drive import get_drive_service, DriveManager, get_service_account_email
        service = get_drive_service()
        if not service:
            return jsonify({
                'success': False,
                'error': 'Google Drive service not configured',
                'details': 'Service account credentials are missing'
            }), 500
        
        service_account_email = get_service_account_email()
        
        # Log the folder ID being tested
        logger.info(f"Testing folder access for ID: {source_folder_id}")
        logger.info(f"Service account: {service_account_email}")
        
        manager = DriveManager(service)
        has_access, error_msg = manager.verify_folder_access(folder_id=source_folder_id)
        
        if has_access:
            # Try to list files to see if we can actually read them
            try:
                # First try without mime filter to see all files
                all_files = manager.list_files(folder_id=source_folder_id, mime_types=None)
                pdf_files = manager.list_files(folder_id=source_folder_id, mime_types=['application/pdf'])
                
                total_count = len(all_files) if all_files else 0
                pdf_count = len(pdf_files) if pdf_files else 0
                
                return jsonify({
                    'success': True,
                    'message': 'Folder access verified successfully',
                    'folder_id': source_folder_id,
                    'service_account': service_account_email,
                    'total_files': total_count,
                    'pdf_files': pdf_count,
                    'count': pdf_count,  # For backward compatibility
                    'details': f'Found {total_count} total files, {pdf_count} PDF files',
                    'file_names': [f.get('name', 'Unknown') for f in (all_files[:10] if all_files else [])]  # First 10 files
                })
            except Exception as list_error:
                error_str = str(list_error)
                logger.error(f"Error listing files: {error_str}", exc_info=True)
                return jsonify({
                    'success': False,
                    'error': 'Can access folder metadata but cannot list files',
                    'details': error_str,
                    'folder_id': source_folder_id,
                    'service_account': service_account_email,
                    'troubleshooting': [
                        'The service account can see the folder exists but cannot read its contents.',
                        'This usually means the service account needs "Editor" permission (not just "Viewer").',
                        'Try removing and re-adding the service account with Editor permission.',
                        'Wait 1-2 minutes after sharing before testing again.'
                    ]
                }), 500
        else:
                # Check server logs for the actual error - might be more specific
                logger.error(f"Folder access failed: {error_msg}")
                
                return jsonify({
                    'success': False,
                    'error': error_msg,
                    'folder_id': source_folder_id,
                    'folder_id_from_url': f'Verify URL: drive.google.com/drive/folders/{source_folder_id}',
                    'service_account': service_account_email,
                    'troubleshooting': [
                        '1. Right-click the folder in Google Drive',
                        '2. Click "Share"',
                        f'3. Paste this email: {service_account_email}',
                        '4. Set permission to "Editor" (NOT Viewer or Commenter)',
                        '5. UNCHECK "Notify people" checkbox',
                        '6. Click "Share"',
                        '7. Wait 1-2 MINUTES (not seconds) for permissions to propagate',
                        '8. Verify the service account appears in the sharing list',
                        '9. Click "Test Folder Access" again'
                    ],
                    'verification_steps': [
                        'To verify sharing: Right-click folder > Share > Check if the service account email appears in the list',
                        'If it does not appear, add it following the steps above',
                        'If it appears but still fails, try removing it and re-adding it',
                        'Check server logs for the actual Google API error message'
                    ],
                    'common_issues': [
                        'Google Workspace: Admin may need to enable API access',
                        'Shared Drives: Service account needs to be added to the Shared Drive, not just the folder',
                        'Permissions delay: Can take 1-2 minutes to propagate',
                        'Service account: Verify the credentials file is correct'
                    ]
                }), 403
        
    except Exception as e:
        logger.error(f"Error testing folder access: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Removed download_drive_file endpoint - files are now referenced directly, not downloaded
# Files are fetched on-demand when serving to students/teachers

@app.route('/api/teacher/get_students', methods=['GET'])
@teacher_required
def teacher_get_students():
    """Get students by class or teaching group for a teacher"""
    try:
        class_id = request.args.get('class_id')
        group_id = request.args.get('group_id')
        
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        teacher_student_ids = set()
        
        # Find students who have this teacher assigned
        students_with_teacher = list(Student.find({'teachers': session['teacher_id']}))
        for student in students_with_teacher:
            teacher_student_ids.add(student.get('student_id'))
        
        # Also find students who have submissions to this teacher's assignments
        assignments = list(Assignment.find({'teacher_id': session['teacher_id']}))
        assignment_ids = [a['assignment_id'] for a in assignments]
        if assignment_ids:
            submission_student_ids = Submission.find({
                'assignment_id': {'$in': assignment_ids}
            }).distinct('student_id')
            for student_id in submission_student_ids:
                teacher_student_ids.add(student_id)
        
        # Filter by class or teaching group
        if group_id:
            # Get students from teaching group
            teaching_group = TeachingGroup.find_one({
                'group_id': group_id,
                'teacher_id': session['teacher_id']
            })
            if not teaching_group:
                return jsonify({'error': 'Teaching group not found'}), 404
            
            group_student_ids = set(teaching_group.get('student_ids', []))
            student_ids = list(teacher_student_ids & group_student_ids)
            
            students = list(Student.find({
                'student_id': {'$in': student_ids}
            }).sort('name', 1))
        elif class_id:
            # Get students from class
            students = list(Student.find({
                'class': class_id,
                'student_id': {'$in': list(teacher_student_ids)}
            }).sort('name', 1))
        else:
            # Get all students for this teacher
            students = list(Student.find({
                'student_id': {'$in': list(teacher_student_ids)}
            }).sort('name', 1))
        
        return jsonify({
            'success': True,
            'students': [{
                'student_id': s.get('student_id'),
                'name': s.get('name'),
                'class': s.get('class') or (s.get('classes', [None])[0] if s.get('classes') else '')
            } for s in students]
        })
        
    except Exception as e:
        logger.error(f"Error getting students: {e}")
        return jsonify({'error': 'Failed to get students'}), 500

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
        'teaching_groups': TeachingGroup.count({}),
        'assignments': Assignment.count({'status': 'published'}),
        'submissions': Submission.count({})
    }
    
    teachers = list(Teacher.find({}))
    classes = list(Class.find({}))
    
    # Add student counts to teachers
    for t in teachers:
        t['student_count'] = Student.count({'teachers': t['teacher_id']})
    
    # Add student counts to classes (support both 'class' and 'classes')
    for c in classes:
        c['student_count'] = Student.count({'$or': [{'class': c['class_id']}, {'classes': c['class_id']}]})

    # Module access allocation (which teachers/classes can use learning modules)
    module_access = _get_module_access_config()
    # Python Lab access (which teachers/classes/teaching groups can use Python Lab)
    python_lab_access = _get_python_lab_access_config()
    teaching_groups = list(TeachingGroup.find({}))
    for g in teaching_groups:
        g['student_count'] = len(g.get('student_ids') or [])

    return render_template('admin_dashboard.html',
                         stats=stats,
                         teachers=teachers,
                         classes=classes,
                         teaching_groups=teaching_groups,
                         module_access_teacher_ids=module_access['teacher_ids'],
                         module_access_class_ids=module_access['class_ids'],
                         python_lab_teacher_ids=python_lab_access['teacher_ids'],
                         python_lab_class_ids=python_lab_access['class_ids'],
                         python_lab_teaching_group_ids=python_lab_access['teaching_group_ids'])


@app.route('/admin/module-access', methods=['POST'])
@admin_required
def admin_save_module_access():
    """Save which teachers and classes have access to learning modules."""
    try:
        data = request.get_json()
        teacher_ids = list(data.get('teacher_ids') or [])
        class_ids = list(data.get('class_ids') or [])
        _save_module_access_config(teacher_ids, class_ids)
        return jsonify({'success': True, 'message': 'Learning module access updated.'})
    except Exception as e:
        logger.error("Error saving module access: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/admin/python-lab-access', methods=['POST'])
@admin_required
def admin_save_python_lab_access():
    """Save which teachers, classes and teaching groups have access to Python Lab."""
    try:
        data = request.get_json()
        teacher_ids = list(data.get('teacher_ids') or [])
        class_ids = list(data.get('class_ids') or [])
        teaching_group_ids = list(data.get('teaching_group_ids') or [])
        _save_python_lab_access_config(teacher_ids, class_ids, teaching_group_ids)
        return jsonify({'success': True, 'message': 'Python Lab access updated.'})
    except Exception as e:
        logger.error("Error saving Python Lab access: %s", e)
        return jsonify({'error': str(e)}), 500


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
                is_default = (password == 'student123')
                
                Student.insert_one({
                    'student_id': student_id,
                    'name': s.get('name', student_id),
                    'class': s.get('class', ''),
                    'password_hash': hash_password(password),
                    'teachers': s.get('teachers', []),
                    'must_change_password': is_default,
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
        
        teacher_doc = {
            'teacher_id': teacher_id,
            'name': data.get('name', teacher_id),
            'password_hash': hash_password(password),
            'subjects': data.get('subjects', []),
            'classes': data.get('classes', []),
            'created_at': datetime.utcnow()
        }
        # Don't set telegram_id - let it be set when teacher verifies via Telegram
        # This avoids duplicate key errors with the sparse unique index
        Teacher.insert_one(teacher_doc)
        
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


@app.route('/admin/unassign_teacher', methods=['POST'])
@admin_required
def unassign_teacher():
    """Unassign a teacher from students in a class"""
    try:
        data = request.get_json()
        
        teacher_id = data.get('teacher_id')
        class_id = data.get('class_id')
        
        if not teacher_id:
            return jsonify({'error': 'Teacher ID required'}), 400
        
        if not class_id:
            return jsonify({'error': 'Class ID required'}), 400
        
        # Remove teacher from all students in this class
        result = Student.update_many(
            {'class': class_id},
            {'$pull': {'teachers': teacher_id}}
        )
        
        return jsonify({
            'success': True, 
            'updated': result.modified_count,
            'message': f'Unassigned {teacher_id} from {result.modified_count} students in class {class_id}'
        })
        
    except Exception as e:
        logger.error(f"Error unassigning teacher: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/assign_students_to_class', methods=['POST'])
@admin_required
def assign_students_to_class():
    """Assign selected students to a class"""
    try:
        data = request.get_json()
        
        student_ids = data.get('student_ids', [])
        class_id = data.get('class_id')
        
        if not student_ids:
            return jsonify({'error': 'No students selected'}), 400
        
        if not class_id:
            return jsonify({'error': 'No class selected'}), 400
        
        # Update students
        result = Student.update_many(
            {'student_id': {'$in': student_ids}},
            {'$set': {'class': class_id}}
        )
        
        return jsonify({'success': True, 'updated': result.modified_count})
        
    except Exception as e:
        logger.error(f"Error assigning students to class: {e}")
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
    """Get list of students with optional class filter and name search"""
    try:
        class_filter = request.args.get('class', '')
        search = request.args.get('search', '').strip()
        
        query = {}
        if class_filter == '__unassigned__':
            # Find students with no class assigned
            # Must have BOTH: no 'class' value AND no 'classes' value
            query['$and'] = [
                {'$or': [
                    {'class': {'$exists': False}},
                    {'class': None},
                    {'class': ''}
                ]},
                {'$or': [
                    {'classes': {'$exists': False}},
                    {'classes': None},
                    {'classes': []},
                    {'classes': {'$size': 0}}
                ]}
            ]
        elif class_filter:
            query['$or'] = [
                {'class': class_filter},
                {'classes': class_filter}
            ]
        
        if search:
            query['name'] = {'$regex': search, '$options': 'i'}
        
        students = list(Student.find(query))
        
        return jsonify({
            'success': True,
            'students': [{
                'student_id': s.get('student_id'),
                'name': s.get('name'),
                'class': s.get('class') or (s.get('classes', [None])[0] if s.get('classes') else ''),
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

@app.route('/admin/reset_student_password', methods=['POST'])
@admin_required
def reset_student_password():
    """Reset a student's password to their student ID"""
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        
        if not student_id:
            return jsonify({'error': 'Student ID required'}), 400
        
        # Find the student
        student = db.db.students.find_one({'student_id': student_id})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Reset password to student ID and require change on next login
        new_password = student_id
        hashed = hash_password(new_password)
        
        db.db.students.update_one(
            {'student_id': student_id},
            {'$set': {'password_hash': hashed, 'must_change_password': True}}
        )
        
        return jsonify({
            'success': True, 
            'new_password': new_password,
            'message': f'Password reset to: {new_password}'
        })
        
    except Exception as e:
        logger.error(f"Error resetting password: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/mass_reset_student_passwords', methods=['POST'])
@admin_required
def mass_reset_student_passwords():
    """Mass reset passwords for multiple students"""
    try:
        data = request.get_json()
        student_ids = data.get('student_ids', [])
        custom_password = data.get('password', '').strip()
        
        if not student_ids:
            return jsonify({'error': 'No students selected'}), 400
        
        # Default password if not specified
        default_password = custom_password if custom_password else 'student123'
        hashed = hash_password(default_password)
        
        # Update all selected students; require change on next login
        result = db.db.students.update_many(
            {'student_id': {'$in': student_ids}},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow(), 'must_change_password': True}}
        )
        
        return jsonify({
            'success': True,
            'count': result.modified_count,
            'password': default_password,
            'message': f'Reset {result.modified_count} student password(s) to: {default_password}'
        })
        
    except Exception as e:
        logger.error(f"Error mass resetting passwords: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/students/reset-password')
@teacher_required
def teacher_reset_student_password_page():
    """Page for teachers to search students and reset a student's password."""
    teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
    if not teacher:
        return redirect(url_for('teacher_login'))
    return render_template('teacher_reset_student_password.html', teacher=teacher)


@app.route('/teacher/api/students/search')
@teacher_required
def teacher_search_students():
    """Search students the teacher can access (by student_id or name)."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 1:
        return jsonify({'students': []})
    accessible_ids = _get_teacher_accessible_student_ids(session['teacher_id'])
    if not accessible_ids:
        return jsonify({'students': []})
    regex = {'$regex': q, '$options': 'i'}
    students = list(Student.find({
        'student_id': {'$in': list(accessible_ids)},
        '$or': [
            {'student_id': regex},
            {'name': regex}
        ]
    }).sort('name', 1).limit(50))
    return jsonify({
        'students': [
            {
                'student_id': s.get('student_id'),
                'name': s.get('name', ''),
                'class': s.get('class') or s.get('classes', [None])[0] if s.get('classes') else ''
            }
            for s in students
        ]
    })


@app.route('/teacher/reset_student_passwords', methods=['POST'])
@teacher_required
def teacher_reset_student_passwords():
    """Teacher resets passwords for their students"""
    try:
        data = request.get_json()
        student_ids = data.get('student_ids', [])
        custom_password = data.get('password', '').strip()
        
        if not student_ids:
            return jsonify({'error': 'No students selected'}), 400
        
        # Verify teacher has access to these students (classes, teachers list, or teaching groups)
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        teacher_classes = teacher.get('classes', [])
        teaching_group_student_ids = set()
        for group in TeachingGroup.find({'teacher_id': session['teacher_id']}):
            teaching_group_student_ids.update(group.get('student_ids', []))
        
        # Get students that belong to teacher's classes, teachers list, or teaching groups
        valid_students = list(Student.find({
            'student_id': {'$in': student_ids},
            '$or': [
                {'class': {'$in': teacher_classes}},
                {'classes': {'$in': teacher_classes}},
                {'teachers': session['teacher_id']},
                {'student_id': {'$in': list(teaching_group_student_ids)}}
            ]
        }))
        
        valid_ids = [s['student_id'] for s in valid_students]
        
        if not valid_ids:
            return jsonify({'error': 'No valid students found'}), 400
        
        # Default password if not specified; validate custom password
        default_password = custom_password if custom_password else 'student123'
        if custom_password:
            ok, err = validate_password(custom_password)
            if not ok:
                return jsonify({'error': err}), 400
        hashed = hash_password(default_password)
        
        # Update valid students; require change on next login
        result = db.db.students.update_many(
            {'student_id': {'$in': valid_ids}},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow(), 'must_change_password': True}}
        )
        
        return jsonify({
            'success': True,
            'count': result.modified_count,
            'password': default_password,
            'message': f'Reset {result.modified_count} student password(s) to: {default_password}'
        })
        
    except Exception as e:
        logger.error(f"Error resetting student passwords: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/student/change_password', methods=['POST'])
@login_required
def student_change_password():
    """Student changes their own password"""
    try:
        data = request.get_json()
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        if not current_password or not new_password:
            return jsonify({'error': 'Current and new passwords required'}), 400
        
        ok, err = validate_password(new_password)
        if not ok:
            return jsonify({'error': err}), 400
        
        student = Student.find_one({'student_id': session['student_id']})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Verify current password
        if not verify_password(current_password, student.get('password_hash', '')):
            return jsonify({'error': 'Current password is incorrect'}), 400
        
        # Update password and clear must_change_password
        hashed = hash_password(new_password)
        Student.update_one(
            {'student_id': session['student_id']},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow(), 'must_change_password': False}}
        )
        
        return jsonify({'success': True, 'message': 'Password changed successfully'})
        
    except Exception as e:
        logger.error(f"Error changing student password: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/reset_teacher_password', methods=['POST'])
@admin_required
def reset_teacher_password():
    """Reset a teacher's password to their teacher ID"""
    try:
        data = request.get_json()
        teacher_id = data.get('teacher_id')
        
        if not teacher_id:
            return jsonify({'error': 'Teacher ID required'}), 400
        
        # Find the teacher
        teacher = db.db.teachers.find_one({'teacher_id': teacher_id})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Reset password to teacher ID
        new_password = teacher_id
        hashed = hash_password(new_password)
        
        db.db.teachers.update_one(
            {'teacher_id': teacher_id},
            {'$set': {'password_hash': hashed}}
        )
        
        return jsonify({
            'success': True, 
            'new_password': new_password,
            'message': f'Password reset to: {new_password}'
        })
        
    except Exception as e:
        logger.error(f"Error resetting teacher password: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/update_teacher', methods=['POST'])
@admin_required
def admin_update_teacher():
    """Admin updates a teacher's information"""
    try:
        data = request.get_json()
        teacher_id = data.get('teacher_id')
        name = data.get('name')
        subjects = data.get('subjects', [])
        new_classes = data.get('classes', [])
        
        if not teacher_id:
            return jsonify({'error': 'Teacher ID required'}), 400
        
        if not name or not name.strip():
            return jsonify({'error': 'Name is required'}), 400
        
        # Find the teacher
        teacher = db.db.teachers.find_one({'teacher_id': teacher_id})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Get old classes for comparison
        old_classes = set(teacher.get('classes', []))
        new_classes_set = set(new_classes)
        
        # Classes that were removed
        removed_classes = old_classes - new_classes_set
        # Classes that were added
        added_classes = new_classes_set - old_classes
        
        # Update teacher
        update_data = {
            'name': name.strip(),
            'subjects': subjects,
            'classes': new_classes,
            'updated_at': datetime.utcnow()
        }
        
        db.db.teachers.update_one(
            {'teacher_id': teacher_id},
            {'$set': update_data}
        )
        
        # Update student-teacher relationships for removed classes
        if removed_classes:
            # Remove teacher from students in removed classes
            Student.update_many(
                {'$or': [
                    {'class': {'$in': list(removed_classes)}},
                    {'classes': {'$in': list(removed_classes)}}
                ]},
                {'$pull': {'teachers': teacher_id}}
            )
        
        # Update student-teacher relationships for added classes
        if added_classes:
            # Add teacher to students in added classes
            Student.update_many(
                {'$or': [
                    {'class': {'$in': list(added_classes)}},
                    {'classes': {'$in': list(added_classes)}}
                ]},
                {'$addToSet': {'teachers': teacher_id}}
            )
        
        return jsonify({
            'success': True,
            'message': f'Teacher {teacher_id} updated successfully'
        })
        
    except Exception as e:
        logger.error(f"Error updating teacher: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/update_student', methods=['POST'])
@admin_required
def admin_update_student():
    """Admin updates a student's information"""
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        name = data.get('name')
        student_class = data.get('class', '')
        teachers = data.get('teachers', [])
        
        if not student_id:
            return jsonify({'error': 'Student ID required'}), 400
        
        if not name or not name.strip():
            return jsonify({'error': 'Name is required'}), 400
        
        # Find the student
        student = db.db.students.find_one({'student_id': student_id})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Update student
        update_data = {
            'name': name.strip(),
            'class': student_class,
            'teachers': teachers,
            'updated_at': datetime.utcnow()
        }
        
        db.db.students.update_one(
            {'student_id': student_id},
            {'$set': update_data}
        )
        
        return jsonify({
            'success': True,
            'message': f'Student {student_id} updated successfully'
        })
        
    except Exception as e:
        logger.error(f"Error updating student: {e}")
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
        
        # Get teachers before deletion to log telegram IDs being freed
        teachers_to_delete = list(db.db.teachers.find({'teacher_id': {'$in': teacher_ids}}))
        telegram_ids_freed = [t.get('telegram_id') for t in teachers_to_delete if t.get('telegram_id')]
        
        # Remove teacher from all students
        Student.update_many(
            {'teachers': {'$in': teacher_ids}},
            {'$pull': {'teachers': {'$in': teacher_ids}}}
        )
        
        # Delete all messages involving these teachers
        db.db.messages.delete_many({'teacher_id': {'$in': teacher_ids}})
        
        # Delete all assignments by these teachers
        db.db.assignments.delete_many({'teacher_id': {'$in': teacher_ids}})
        
        # Delete all submissions for those assignments
        db.db.submissions.delete_many({'teacher_id': {'$in': teacher_ids}})
        
        # Delete teachers (this also removes their telegram_id association)
        result = db.db.teachers.delete_many({'teacher_id': {'$in': teacher_ids}})
        
        logger.info(f"Deleted {result.deleted_count} teacher(s). Telegram IDs freed: {telegram_ids_freed}")
        
        return jsonify({
            'success': True, 
            'deleted': result.deleted_count,
            'telegram_ids_freed': len(telegram_ids_freed),
            'message': f'Deleted {result.deleted_count} teacher(s). Telegram accounts can now be reused.'
        })
        
    except Exception as e:
        logger.error(f"Error deleting teachers: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/delete_class', methods=['POST'])
@admin_required
def delete_class():
    """Delete a class"""
    try:
        data = request.get_json()
        class_id = data.get('class_id')
        
        if not class_id:
            return jsonify({'error': 'No class specified'}), 400
        
        # Check if class exists
        existing = db.db.classes.find_one({'class_id': class_id})
        if not existing:
            return jsonify({'error': 'Class not found'}), 404
        
        # Remove class from students (don't delete students, just unassign)
        db.db.students.update_many(
            {'class': class_id},
            {'$set': {'class': ''}}
        )
        
        # Delete the class
        db.db.classes.delete_one({'class_id': class_id})
        
        return jsonify({'success': True, 'deleted': class_id})
        
    except Exception as e:
        logger.error(f"Error deleting class: {e}")
        return jsonify({'error': str(e)}), 500


# ================== TEACHING GROUPS API ==================

@app.route('/admin/api/teaching-groups')
@admin_required
def get_teaching_groups():
    """Get all teaching groups"""
    try:
        groups = list(TeachingGroup.find({}))
        
        # Enrich with teacher names and student counts
        for g in groups:
            g['_id'] = str(g['_id'])
            teacher = Teacher.find_one({'teacher_id': g.get('teacher_id')})
            g['teacher_name'] = teacher.get('name', g.get('teacher_id')) if teacher else g.get('teacher_id')
            g['student_count'] = len(g.get('student_ids', []))
        
        return jsonify({'success': True, 'groups': groups})
        
    except Exception as e:
        logger.error(f"Error getting teaching groups: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teaching-groups', methods=['POST'])
@admin_required
def create_teaching_group():
    """Create a new teaching group. Only name and teacher are required; class and students are optional."""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        class_id = data.get('class_id') or None  # Optional
        teacher_id = data.get('teacher_id')
        student_ids = data.get('student_ids', []) or []
        
        if not name or not teacher_id:
            return jsonify({'error': 'Group name and teacher are required'}), 400
        
        # Generate unique group ID
        import uuid
        group_id = f"TG-{uuid.uuid4().hex[:8].upper()}"
        
        group_doc = {
            'group_id': group_id,
            'name': name,
            'class_id': class_id,
            'teacher_id': teacher_id,
            'student_ids': student_ids,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }
        
        TeachingGroup.insert_one(group_doc)
        
        if student_ids:
            msg = f'Teaching group "{name}" created with {len(student_ids)} students'
        else:
            msg = f'Teaching group "{name}" created. Add students later via "Add Students to Teaching Group by Name".'
        
        return jsonify({
            'success': True,
            'group_id': group_id,
            'message': msg
        })
        
    except Exception as e:
        logger.error(f"Error creating teaching group: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teaching-groups/<group_id>')
@admin_required
def get_teaching_group(group_id):
    """Get details of a specific teaching group"""
    try:
        group = TeachingGroup.find_one({'group_id': group_id})
        
        if not group:
            return jsonify({'error': 'Teaching group not found'}), 404
        
        group['_id'] = str(group['_id'])
        
        # Get student details
        students = list(Student.find({'student_id': {'$in': group.get('student_ids', [])}}))
        group['students'] = [{'student_id': s['student_id'], 'name': s.get('name', s['student_id'])} for s in students]
        
        # Get teacher name
        teacher = Teacher.find_one({'teacher_id': group.get('teacher_id')})
        group['teacher_name'] = teacher.get('name', group.get('teacher_id')) if teacher else group.get('teacher_id')
        
        return jsonify({'success': True, 'group': group})
        
    except Exception as e:
        logger.error(f"Error getting teaching group: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teaching-groups/<group_id>', methods=['DELETE'])
@admin_required
def delete_teaching_group(group_id):
    """Delete a teaching group"""
    try:
        group = TeachingGroup.find_one({'group_id': group_id})
        
        if not group:
            return jsonify({'error': 'Teaching group not found'}), 404
        
        TeachingGroup.delete_one({'group_id': group_id})
        
        return jsonify({'success': True, 'message': 'Teaching group deleted'})
        
    except Exception as e:
        logger.error(f"Error deleting teaching group: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/prompts')
@admin_required
def get_prompts():
    """Get all AI help prompts"""
    from utils.ai_marking import get_ai_prompts, get_default_prompts
    
    try:
        prompts = get_ai_prompts(db)
        defaults = get_default_prompts()
        
        return jsonify({
            'success': True,
            'prompts': prompts,
            'defaults': defaults
        })
        
    except Exception as e:
        logger.error(f"Error getting prompts: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/prompts', methods=['POST'])
@admin_required
def update_prompts():
    """Update AI help prompts"""
    from utils.ai_marking import save_ai_prompts
    
    try:
        data = request.get_json()
        prompts = data.get('prompts', {})
        
        if not prompts:
            return jsonify({'error': 'No prompts provided'}), 400
        
        success = save_ai_prompts(db, prompts)
        
        if success:
            return jsonify({'success': True, 'message': 'Prompts updated successfully'})
        else:
            return jsonify({'error': 'Failed to save prompts'}), 500
        
    except Exception as e:
        logger.error(f"Error updating prompts: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/prompts/reset', methods=['POST'])
@admin_required
def reset_prompts():
    """Reset prompts to defaults"""
    try:
        # Delete custom prompts from database
        db.db.ai_prompts.delete_one({'_id': 'help_prompts'})
        
        return jsonify({'success': True, 'message': 'Prompts reset to defaults'})
        
    except Exception as e:
        logger.error(f"Error resetting prompts: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# ADMIN REPORTS AND DUPLICATE MANAGEMENT
# ============================================================================

@app.route('/admin/api/match-students-by-name', methods=['POST'])
@admin_required
def match_students_by_name():
    """Match a list of names to existing students in the system"""
    try:
        data = request.get_json()
        names = data.get('names', [])
        
        if not names:
            return jsonify({'error': 'No names provided'}), 400
        
        found = []
        not_found = []
        
        for name in names:
            name = name.strip()
            if not name:
                continue
            
            # Try exact match first (case-insensitive)
            student = Student.find_one({
                'name': {'$regex': f'^{name}$', '$options': 'i'}
            })
            
            if student:
                found.append({
                    'input_name': name,
                    'student_id': student['student_id'],
                    'name': student.get('name', ''),
                    'class': student.get('class', '')
                })
            else:
                # Try partial match
                student = Student.find_one({
                    'name': {'$regex': name, '$options': 'i'}
                })
                
                if student:
                    found.append({
                        'input_name': name,
                        'student_id': student['student_id'],
                        'name': student.get('name', ''),
                        'class': student.get('class', ''),
                        'partial_match': True
                    })
                else:
                    not_found.append({
                        'input_name': name,
                        'reason': 'No matching student found'
                    })
        
        return jsonify({
            'success': True,
            'found': found,
            'not_found': not_found
        })
        
    except Exception as e:
        logger.error(f"Error matching students: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teaching-groups/<group_id>/add-students', methods=['POST'])
@admin_required
def add_students_to_teaching_group(group_id):
    """Add students to an existing teaching group"""
    try:
        data = request.get_json()
        student_ids = data.get('student_ids', [])
        
        if not student_ids:
            return jsonify({'error': 'No students selected'}), 400
        
        # Get teaching group
        group = TeachingGroup.find_one({'group_id': group_id})
        if not group:
            return jsonify({'error': 'Teaching group not found'}), 404
        
        # Get current student IDs
        current_ids = set(group.get('student_ids', []))
        new_ids = set(student_ids)
        
        # Merge and deduplicate
        merged_ids = list(current_ids | new_ids)
        added_count = len(new_ids - current_ids)
        
        # Update the teaching group
        TeachingGroup.update_one(
            {'group_id': group_id},
            {'$set': {'student_ids': merged_ids, 'updated_at': datetime.utcnow()}}
        )
        
        return jsonify({
            'success': True,
            'added': added_count,
            'total': len(merged_ids),
            'already_in_group': len(new_ids & current_ids)
        })
        
    except Exception as e:
        logger.error(f"Error adding students to teaching group: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teaching-groups/<group_id>/students', methods=['PUT'])
@admin_required
def update_teaching_group_students(group_id):
    """Replace the student list of a teaching group (for edit: remove/add)"""
    try:
        data = request.get_json()
        student_ids = data.get('student_ids', [])
        
        if student_ids is None:
            return jsonify({'error': 'student_ids required'}), 400
        
        group = TeachingGroup.find_one({'group_id': group_id})
        if not group:
            return jsonify({'error': 'Teaching group not found'}), 404
        
        student_ids = list(student_ids) if isinstance(student_ids, list) else []
        
        TeachingGroup.update_one(
            {'group_id': group_id},
            {'$set': {'student_ids': student_ids, 'updated_at': datetime.utcnow()}}
        )
        
        return jsonify({
            'success': True,
            'total': len(student_ids),
            'message': f'Teaching group updated with {len(student_ids)} students'
        })
        
    except Exception as e:
        logger.error(f"Error updating teaching group students: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/bulk-assign-class', methods=['POST'])
@admin_required
def bulk_assign_students_to_class():
    """Assign multiple students to a class"""
    try:
        data = request.get_json()
        student_ids = data.get('student_ids', [])
        class_id = data.get('class_id')
        
        if not student_ids:
            return jsonify({'error': 'No students selected'}), 400
        
        if not class_id:
            return jsonify({'error': 'No class selected'}), 400
        
        # Verify class exists
        class_doc = Class.find_one({'class_id': class_id})
        if not class_doc:
            return jsonify({'error': 'Class not found'}), 404
        
        # Update all students
        result = Student.update_many(
            {'student_id': {'$in': student_ids}},
            {'$set': {'class': class_id}}
        )
        
        return jsonify({
            'success': True,
            'updated': result.modified_count,
            'class_id': class_id
        })
        
    except Exception as e:
        logger.error(f"Error bulk assigning students: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teacher/<teacher_id>/assignments')
@admin_required
def get_teacher_class_assignments(teacher_id):
    """Get all classes and teaching groups assigned to a teacher"""
    try:
        teacher = Teacher.find_one({'teacher_id': teacher_id})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Get classes where this teacher is assigned
        # A teacher is assigned to a class if students in that class have this teacher
        pipeline = [
            {'$match': {'teachers': teacher_id}},
            {'$group': {'_id': '$class', 'student_count': {'$sum': 1}}},
            {'$match': {'_id': {'$ne': None, '$ne': ''}}}
        ]
        class_results = list(db.db.students.aggregate(pipeline))
        
        classes = []
        for result in class_results:
            class_id = result['_id']
            class_doc = Class.find_one({'class_id': class_id})
            classes.append({
                'class_id': class_id,
                'name': class_doc.get('name', '') if class_doc else '',
                'student_count': result['student_count']
            })
        
        # Get teaching groups for this teacher
        teaching_groups = list(TeachingGroup.find({'teacher_id': teacher_id}))
        groups = []
        for g in teaching_groups:
            groups.append({
                'group_id': g['group_id'],
                'name': g.get('name', ''),
                'class_id': g.get('class_id', ''),
                'student_count': len(g.get('student_ids', []))
            })
        
        return jsonify({
            'success': True,
            'teacher': {
                'teacher_id': teacher_id,
                'name': teacher.get('name', teacher_id)
            },
            'classes': classes,
            'teaching_groups': groups
        })
        
    except Exception as e:
        logger.error(f"Error getting teacher assignments: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/class-report/<class_id>/pdf')
@admin_required
def generate_class_student_list(class_id):
    """Generate a PDF report with student ID and name for a class"""
    try:
        from utils.pdf_generator import generate_class_student_list_pdf
        
        # Get class info
        class_doc = Class.find_one({'class_id': class_id})
        if not class_doc:
            return jsonify({'error': 'Class not found'}), 404
        
        # Get students in this class
        students = list(Student.find({'class': class_id}))
        
        if not students:
            return jsonify({'error': 'No students found in this class'}), 404
        
        # Get optional teacher info (from query param)
        teacher = None
        teacher_id = request.args.get('teacher_id')
        if teacher_id:
            teacher = Teacher.find_one({'teacher_id': teacher_id})
        
        # Generate PDF
        pdf_bytes = generate_class_student_list_pdf(
            class_id=class_id,
            class_name=class_doc.get('name', ''),
            students=students,
            teacher=teacher
        )
        
        # Return PDF as download
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename=class_{class_id}_students.pdf'
            }
        )
        
    except Exception as e:
        logger.error(f"Error generating class report: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/teaching-group-report/<group_id>/pdf')
@admin_required
def generate_teaching_group_student_list(group_id):
    """Generate a PDF report with student ID and name for a teaching group"""
    try:
        from utils.pdf_generator import generate_class_student_list_pdf
        
        # Get teaching group
        group = TeachingGroup.find_one({'group_id': group_id})
        if not group:
            return jsonify({'error': 'Teaching group not found'}), 404
        
        # Get students in this group
        student_ids = group.get('student_ids', [])
        students = list(Student.find({'student_id': {'$in': student_ids}}))
        
        if not students:
            return jsonify({'error': 'No students found in this teaching group'}), 404
        
        # Get teacher info
        teacher = Teacher.find_one({'teacher_id': group.get('teacher_id')})
        
        # Generate PDF using the same function but with group name
        pdf_bytes = generate_class_student_list_pdf(
            class_id=group.get('name', group_id),
            class_name=f"Teaching Group - {group.get('class_id', '')}",
            students=students,
            teacher=teacher
        )
        
        # Return PDF as download
        safe_name = group.get('name', group_id).replace(' ', '_')
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename=teaching_group_{safe_name}_students.pdf'
            }
        )
        
    except Exception as e:
        logger.error(f"Error generating teaching group report: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/duplicates')
@admin_required
def get_duplicate_students():
    """Find students with duplicate names"""
    try:
        # Aggregate to find duplicate names
        pipeline = [
            {
                '$group': {
                    '_id': {'$toLower': '$name'},
                    'students': {'$push': {
                        'student_id': '$student_id',
                        'name': '$name',
                        'class': '$class',
                        'teachers': '$teachers',
                        'created_at': '$created_at'
                    }},
                    'count': {'$sum': 1}
                }
            },
            {
                '$match': {'count': {'$gt': 1}}
            },
            {
                '$sort': {'count': -1}
            }
        ]
        
        duplicates = list(db.db.students.aggregate(pipeline))
        
        # Format the response
        duplicate_groups = []
        for dup in duplicates:
            # Sort students by created_at (oldest first)
            students = dup['students']
            students.sort(key=lambda s: s.get('created_at') or datetime.min)
            duplicate_groups.append({
                'name': dup['_id'],
                'count': dup['count'],
                'students': students
            })
        
        return jsonify({
            'success': True,
            'total_groups': len(duplicate_groups),
            'duplicates': duplicate_groups
        })
        
    except Exception as e:
        logger.error(f"Error finding duplicates: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/duplicates/report/pdf')
@admin_required
def generate_duplicate_report():
    """Generate a PDF report of duplicate students"""
    try:
        from utils.pdf_generator import generate_duplicate_report_pdf
        
        # Get duplicates
        pipeline = [
            {
                '$group': {
                    '_id': {'$toLower': '$name'},
                    'students': {'$push': {
                        'student_id': '$student_id',
                        'name': '$name',
                        'class': '$class',
                        'teachers': '$teachers',
                        'created_at': '$created_at'
                    }},
                    'count': {'$sum': 1}
                }
            },
            {
                '$match': {'count': {'$gt': 1}}
            },
            {
                '$sort': {'count': -1}
            }
        ]
        
        duplicates = list(db.db.students.aggregate(pipeline))
        
        # Format for PDF
        duplicate_groups = [dup['students'] for dup in duplicates]
        
        # Generate PDF
        pdf_bytes = generate_duplicate_report_pdf(duplicate_groups)
        
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename=duplicate_students_report.pdf'
            }
        )
        
    except Exception as e:
        logger.error(f"Error generating duplicate report: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/duplicates/resolve', methods=['POST'])
@admin_required
def resolve_duplicates():
    """
    Resolve duplicate students by keeping the older account and removing newer ones.
    Updates all references (submissions, messages, teaching groups) to use the kept ID.
    """
    try:
        data = request.get_json()
        duplicate_groups = data.get('groups', [])
        
        if not duplicate_groups:
            return jsonify({'error': 'No duplicate groups provided'}), 400
        
        resolved_mappings = []
        affected_teacher_ids = set()
        
        for group in duplicate_groups:
            if len(group) < 2:
                continue
            
            # Sort by created_at to find the oldest (the one to keep)
            sorted_group = sorted(group, key=lambda s: s.get('created_at') or datetime.min)
            keep_student = sorted_group[0]
            remove_students = sorted_group[1:]
            
            keep_id = keep_student.get('student_id')
            
            for remove_student in remove_students:
                remove_id = remove_student.get('student_id')
                
                if not remove_id or not keep_id:
                    continue
                
                # Track affected teachers
                teachers = remove_student.get('teachers', [])
                for t in teachers:
                    affected_teacher_ids.add(t)
                
                # Also add teachers from the kept student
                keep_teachers = keep_student.get('teachers', [])
                for t in keep_teachers:
                    affected_teacher_ids.add(t)
                
                # Update submissions to use the kept ID
                Submission.update_many(
                    {'student_id': remove_id},
                    {'$set': {'student_id': keep_id, 'original_student_id': remove_id}}
                )
                
                # Update messages
                Message.update_many(
                    {'student_id': remove_id},
                    {'$set': {'student_id': keep_id}}
                )
                
                # Update teaching groups
                db.db.teaching_groups.update_many(
                    {'student_ids': remove_id},
                    {'$set': {'student_ids.$': keep_id}}
                )
                
                # Merge teachers list to the kept account
                Student.update_one(
                    {'student_id': keep_id},
                    {'$addToSet': {'teachers': {'$each': teachers}}}
                )
                
                # Delete the duplicate student account
                db.db.students.delete_one({'student_id': remove_id})
                
                resolved_mappings.append({
                    'name': remove_student.get('name'),
                    'kept_id': keep_id,
                    'removed_id': remove_id,
                    'class': remove_student.get('class', '')
                })
        
        # Get affected teachers for the report
        affected_teachers = list(Teacher.find({'teacher_id': {'$in': list(affected_teacher_ids)}}))
        
        return jsonify({
            'success': True,
            'resolved_count': len(resolved_mappings),
            'resolved_mappings': resolved_mappings,
            'affected_teachers': [{'teacher_id': t['teacher_id'], 'name': t.get('name')} for t in affected_teachers]
        })
        
    except Exception as e:
        logger.error(f"Error resolving duplicates: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/duplicates/affected-teachers-report/pdf', methods=['POST'])
@admin_required
def generate_affected_teachers_report():
    """Generate a PDF report for teachers affected by duplicate resolution"""
    try:
        from utils.pdf_generator import generate_affected_teachers_report_pdf
        
        data = request.get_json()
        resolved_mappings = data.get('resolved_mappings', [])
        affected_teacher_ids = data.get('affected_teacher_ids', [])
        
        if not resolved_mappings:
            return jsonify({'error': 'No resolved mappings provided'}), 400
        
        # Get teacher details
        affected_teachers = list(Teacher.find({'teacher_id': {'$in': affected_teacher_ids}}))
        
        # Generate PDF
        pdf_bytes = generate_affected_teachers_report_pdf(affected_teachers, resolved_mappings)
        
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename=affected_teachers_report.pdf'
            }
        )
        
    except Exception as e:
        logger.error(f"Error generating affected teachers report: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/class-to-teaching-group', methods=['POST'])
@admin_required
def convert_class_to_teaching_group():
    """Convert a class to a teaching group"""
    try:
        data = request.get_json()
        class_id = data.get('class_id')
        teacher_id = data.get('teacher_id')
        group_name = data.get('group_name')
        
        if not class_id or not teacher_id:
            return jsonify({'error': 'Class ID and Teacher ID are required'}), 400
        
        # Get class info
        class_doc = Class.find_one({'class_id': class_id})
        if not class_doc:
            return jsonify({'error': 'Class not found'}), 404
        
        # Get teacher info
        teacher = Teacher.find_one({'teacher_id': teacher_id})
        if not teacher:
            return jsonify({'error': 'Teacher not found'}), 404
        
        # Get all students in the class
        students = list(Student.find({'class': class_id}))
        if not students:
            return jsonify({'error': 'No students found in this class'}), 404
        
        student_ids = [s['student_id'] for s in students]
        
        # Generate unique group ID
        import uuid
        group_id = f"TG-{uuid.uuid4().hex[:8].upper()}"
        
        # Use provided name or generate one
        if not group_name:
            group_name = f"{class_id} - {teacher.get('name', teacher_id)}"
        
        # Create teaching group
        group_doc = {
            'group_id': group_id,
            'name': group_name,
            'class_id': class_id,
            'teacher_id': teacher_id,
            'student_ids': student_ids,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            'converted_from_class': True
        }
        
        TeachingGroup.insert_one(group_doc)
        
        return jsonify({
            'success': True,
            'group_id': group_id,
            'group_name': group_name,
            'student_count': len(student_ids),
            'message': f'Teaching group "{group_name}" created with {len(student_ids)} students from class {class_id}'
        })
        
    except Exception as e:
        logger.error(f"Error converting class to teaching group: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/admin/import_students_with_reconciliation', methods=['POST'])
@admin_required
def import_students_with_reconciliation():
    """
    Import students with duplicate name detection.
    If a student with the same name exists, use the existing (older) ID instead of creating a new one.
    """
    try:
        data = request.get_json()
        students_data = data.get('students', [])
        reconcile_by_name = data.get('reconcile_by_name', True)
        
        if not students_data:
            return jsonify({'error': 'No student data provided'}), 400
        
        imported = 0
        updated = 0
        reconciled = []
        errors = []
        
        for s in students_data:
            try:
                student_id = s.get('student_id', '').upper().strip()
                name = s.get('name', '').strip()
                
                if not student_id:
                    errors.append(f"Missing student_id for {name or 'unknown'}")
                    continue
                
                if not name:
                    errors.append(f"Missing name for {student_id}")
                    continue
                
                # Check if student ID already exists
                existing_by_id = Student.find_one({'student_id': student_id})
                
                if existing_by_id:
                    # Student ID exists, update if needed
                    update_fields = {}
                    if s.get('class'):
                        update_fields['class'] = s.get('class')
                    if s.get('teachers'):
                        update_fields['teachers'] = s.get('teachers', [])
                    
                    if update_fields:
                        Student.update_one(
                            {'student_id': student_id},
                            {'$set': update_fields}
                        )
                        updated += 1
                    else:
                        errors.append(f"{student_id} already exists (no updates)")
                    continue
                
                # Check for existing student with same name (for reconciliation)
                if reconcile_by_name:
                    existing_by_name = Student.find_one({
                        'name': {'$regex': f'^{name}$', '$options': 'i'}
                    })
                    
                    if existing_by_name:
                        # Found existing student with same name - reconcile
                        existing_id = existing_by_name['student_id']
                        
                        # Update the existing student's class/teachers if provided
                        update_fields = {}
                        if s.get('class') and s.get('class') != existing_by_name.get('class'):
                            update_fields['class'] = s.get('class')
                        if s.get('teachers'):
                            # Merge teachers
                            existing_teachers = existing_by_name.get('teachers', [])
                            new_teachers = list(set(existing_teachers + s.get('teachers', [])))
                            if new_teachers != existing_teachers:
                                update_fields['teachers'] = new_teachers
                        
                        if update_fields:
                            Student.update_one(
                                {'student_id': existing_id},
                                {'$set': update_fields}
                            )
                        
                        reconciled.append({
                            'attempted_id': student_id,
                            'existing_id': existing_id,
                            'name': name
                        })
                        continue
                
                # No conflicts - create new student
                password = s.get('password', 'student123')
                is_default = (password == 'student123')
                
                Student.insert_one({
                    'student_id': student_id,
                    'name': name,
                    'class': s.get('class', ''),
                    'password_hash': hash_password(password),
                    'teachers': s.get('teachers', []),
                    'must_change_password': is_default,
                    'created_at': datetime.utcnow()
                })
                
                imported += 1
                
            except Exception as e:
                errors.append(f"Error with {s.get('student_id', 'unknown')}: {str(e)}")
        
        return jsonify({
            'success': True,
            'imported': imported,
            'updated': updated,
            'reconciled': reconciled,
            'reconciled_count': len(reconciled),
            'errors': errors
        })
        
    except Exception as e:
        logger.error(f"Error importing students with reconciliation: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# PWA AND PUSH NOTIFICATION ROUTES
# ============================================================================

@app.route('/service-worker.js')
def service_worker():
    """Serve service worker from root for proper scope"""
    return send_file('static/service-worker.js', mimetype='application/javascript')

@app.route('/offline.html')
def offline_page():
    """Offline fallback page"""
    return render_template('offline.html')

@app.route('/api/push/vapid-public-key')
def get_vapid_public_key():
    """Get the VAPID public key for push subscription"""
    from utils.push_notifications import get_vapid_public_key, is_push_configured
    
    if not is_push_configured():
        return jsonify({'error': 'Push notifications not configured'}), 503
    
    return jsonify({'publicKey': get_vapid_public_key()})

@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def subscribe_push():
    """Save a student's push subscription"""
    try:
        data = request.get_json()
        subscription = data.get('subscription')
        
        if not subscription:
            return jsonify({'error': 'No subscription provided'}), 400
        
        # Save subscription to student document
        Student.update_one(
            {'student_id': session['student_id']},
            {'$set': {
                'push_subscription': subscription,
                'push_subscribed_at': datetime.utcnow()
            }}
        )
        
        logger.info(f"Push subscription saved for student {session['student_id']}")
        return jsonify({'success': True, 'message': 'Subscribed to notifications'})
        
    except Exception as e:
        logger.error(f"Error saving push subscription: {e}")
        return jsonify({'error': 'Failed to subscribe'}), 500

@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def unsubscribe_push():
    """Remove a student's push subscription"""
    try:
        Student.update_one(
            {'student_id': session['student_id']},
            {'$unset': {'push_subscription': '', 'push_subscribed_at': ''}}
        )
        
        logger.info(f"Push subscription removed for student {session['student_id']}")
        return jsonify({'success': True, 'message': 'Unsubscribed from notifications'})
        
    except Exception as e:
        logger.error(f"Error removing push subscription: {e}")
        return jsonify({'error': 'Failed to unsubscribe'}), 500

@app.route('/api/push/status')
@login_required
def push_status():
    """Check if student has push notifications enabled"""
    try:
        student = Student.find_one({'student_id': session['student_id']})
        has_subscription = bool(student and student.get('push_subscription'))
        
        from utils.push_notifications import is_push_configured
        
        return jsonify({
            'subscribed': has_subscription,
            'configured': is_push_configured()
        })
        
    except Exception as e:
        return jsonify({'subscribed': False, 'configured': False})

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
