from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
import os
import io
from datetime import datetime, timedelta, timezone
from models import db, Student, Teacher, Message, Class, TeachingGroup, Assignment, Submission
from utils.auth import hash_password, verify_password, generate_assignment_id, generate_submission_id, encrypt_api_key, decrypt_api_key
from utils.ai_marking import get_teacher_ai_service, mark_submission
from utils.google_drive import get_teacher_drive_manager, upload_assignment_file
from utils.pdf_generator import generate_feedback_pdf
from utils.notifications import notify_submission_ready
import logging
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
            
            # If password_hash is missing or empty, set it to default 'student123'
            if not password_hash:
                logger.warning(f"Student {user_id_upper} has no password_hash, setting default")
                default_hash = hash_password('student123')
                Student.update_one(
                    {'student_id': user_id_upper},
                    {'$set': {'password_hash': default_hash}}
                )
                password_hash = default_hash
            
            # Try to verify password
            password_valid = False
            try:
                password_valid = verify_password(password, password_hash)
            except Exception as e:
                logger.error(f"Error verifying password for {user_id_upper}: {e}")
                # If verification fails due to corrupted hash, try default password
                if password == 'student123':
                    logger.warning(f"Password hash may be corrupted for {user_id_upper}, resetting to default")
                    default_hash = hash_password('student123')
                    Student.update_one(
                        {'student_id': user_id_upper},
                        {'$set': {'password_hash': default_hash}}
                    )
                    password_valid = True
            
            # If password verification failed but user entered default password, reset hash
            if not password_valid and password == 'student123':
                logger.warning(f"Password verification failed for {user_id_upper} with default password, resetting hash")
                default_hash = hash_password('student123')
                Student.update_one(
                    {'student_id': user_id_upper},
                    {'$set': {'password_hash': default_hash}}
                )
                password_valid = True
            
            if password_valid:
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
    # Get all teacher IDs including those from teaching groups
    teacher_ids = get_student_teacher_ids(session['student_id'])
    
    all_assignments = list(Assignment.find({
        'teacher_id': {'$in': teacher_ids},
        'subject': subject,
        'status': 'published'
    }).sort('created_at', -1))
    
    # Filter assignments based on target class/teaching group
    assignments = [a for a in all_assignments if can_student_access_assignment(student, a)]
    
    # Add submission status and teacher info for each
    for a in assignments:
        submission = Submission.find_one({
            'assignment_id': a['assignment_id'],
            'student_id': session['student_id']
        })
        a['submission'] = submission
        
        # Add teacher info
        teacher = Teacher.find_one({'teacher_id': a['teacher_id']})
        a['teacher_name'] = teacher.get('name', a['teacher_id']) if teacher else a['teacher_id']
    
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
    
    # Check if student has access to this teacher (directly or via teaching group)
    teacher_ids = get_student_teacher_ids(session['student_id'])
    if assignment.get('teacher_id') not in teacher_ids:
        return redirect(url_for('assignments_list'))
    
    # Check if student can access this assignment based on target class/teaching group
    if not can_student_access_assignment(student, assignment):
        return redirect(url_for('assignments_list'))
    
    # Get existing submission
    existing_submission = Submission.find_one({
        'assignment_id': assignment_id,
        'student_id': session['student_id'],
        'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
    })
    
    teacher = Teacher.find_one({'teacher_id': assignment['teacher_id']})
    
    return render_template('assignment_view.html',
                         student=student,
                         assignment=assignment,
                         existing_submission=existing_submission,
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
        
        return jsonify({
            'success': True,
            'help': help_response
        })
        
    except Exception as e:
        logger.error(f"Error getting question help: {e}")
        return jsonify({'error': 'Failed to get help. Please try again.'}), 500

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
    from utils.ai_marking import analyze_submission_images
    
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
        
        # Check for existing submission
        existing = Submission.find_one({
            'assignment_id': assignment_id,
            'student_id': session['student_id'],
            'status': {'$in': ['submitted', 'ai_reviewed', 'reviewed']}
        })
        
        if existing:
            return jsonify({'error': 'Already submitted'}), 400
        
        submission_id = generate_submission_id()
        fs = GridFS(db.db)
        
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
        
        # Create submission
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
            'created_at': datetime.utcnow()
        }
        
        Submission.insert_one(submission)
        
        # Generate AI feedback
        try:
            # Get answer key
            answer_key_content = None
            if assignment.get('answer_key_id'):
                try:
                    answer_file = fs.get(assignment['answer_key_id'])
                    answer_key_content = answer_file.read()
                except:
                    pass
            
            ai_result = analyze_submission_images(pages, assignment, answer_key_content, teacher)
            
            Submission.update_one(
                {'submission_id': submission_id},
                {'$set': {
                    'ai_feedback': ai_result,
                    'status': 'ai_reviewed'
                }}
            )
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
        
        return jsonify({
            'success': True,
            'feedback': feedback
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
    
    if file_id_field not in assignment or not assignment[file_id_field]:
        logger.error(f"File ID field '{file_id_field}' not found in assignment {assignment_id}. Fields: {list(assignment.keys())}")
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
    """Download feedback PDF for student"""
    from utils.pdf_generator import generate_review_pdf
    
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
    assignment_id = request.args.get('assignment_id')
    student_ids = request.args.getlist('student_ids[]')
    
    if not assignment_id or not student_ids:
        return jsonify({'error': 'Missing parameters'}), 400
    
    # Verify teacher owns this assignment
    assignment = Assignment.find_one({
        'assignment_id': assignment_id,
        'teacher_id': session['teacher_id']
    })
    
    if not assignment:
        return jsonify({'error': 'Assignment not found'}), 404
    
    # Get submissions for these students
    statuses = {}
    for student_id in student_ids:
        submission = Submission.find_one({
            'assignment_id': assignment_id,
            'student_id': student_id
        })
        
        if submission:
            status = submission.get('status', 'submitted')
            # Pending Review: student has submitted (including AI auto-reviewed)
            if status in ['submitted', 'ai_reviewed']:
                statuses[student_id] = {'status': 'pending', 'label': 'Pending Review', 'class': 'warning'}
            # Returned: teacher has reviewed and returned
            elif status in ['reviewed', 'approved']:
                statuses[student_id] = {'status': 'returned', 'label': 'Returned', 'class': 'success'}
            else:
                statuses[student_id] = {'status': status, 'label': status.title(), 'class': 'secondary'}
        else:
            statuses[student_id] = {'status': 'none', 'label': 'No Submission', 'class': 'light'}
    
    return jsonify({'success': True, 'statuses': statuses})

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
    
    # Calculate statistics
    reviewed_count = len([s for s in submissions if s['status'] == 'reviewed'])
    pending_count = len(submissions) - reviewed_count
    
    # Convert final_marks to float, handling string values
    scores = []
    for s in submissions:
        fm = s.get('final_marks')
        if fm is not None:
            try:
                scores.append(float(fm))
            except (ValueError, TypeError):
                pass  # Skip invalid values
    
    total_marks = float(assignment.get('total_marks', 100) or 100)
    
    avg_marks = sum(scores) / len(scores) if scores else 0
    avg_score = (avg_marks / total_marks * 100) if total_marks > 0 else 0
    pass_count = len([s for s in scores if s >= total_marks * 0.5])
    pass_rate = (pass_count / len(scores) * 100) if scores else 0
    
    stats = {
        'total_students': total_students,
        'submitted': len(submissions),
        'reviewed': reviewed_count,
        'pending': pending_count,
        'avg_marks': avg_marks,
        'avg_score': avg_score,
        'pass_rate': round(pass_rate)
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
    
    # Build student submission list
    student_submissions = []
    for student in sorted(all_students, key=lambda x: x.get('name', '')):
        sub = submission_map.get(student['student_id'])
        percentage = 0
        if sub and sub.get('final_marks') is not None:
            try:
                fm = float(sub['final_marks'])
                percentage = (fm / total_marks * 100) if total_marks > 0 else 0
            except (ValueError, TypeError):
                percentage = 0
        
        student_submissions.append({
            'student': student,
            'submission': sub,
            'status': sub['status'] if sub else 'not_submitted',
            'percentage': percentage
        })
    
    # Sort: pending first, then by score descending
    student_submissions.sort(key=lambda x: (
        0 if x['status'] in ['submitted', 'ai_reviewed'] else 1,
        -x['percentage']
    ))
    
    return render_template('teacher_assignment_summary.html',
                         teacher=teacher,
                         assignment=assignment,
                         stats=stats,
                         score_distribution=score_distribution,
                         insights=insights,
                         student_submissions=student_submissions)

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
            q_num = q.get('question_num', 0)
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
    
    for q_num, stats in sorted(question_stats.items()):
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
            
            # Get uploaded files
            question_paper = request.files.get('question_paper')
            answer_key = request.files.get('answer_key')
            rubrics = request.files.get('rubrics')
            
            # Validate required files based on marking type
            if marking_type == 'rubric':
                # For rubric-based: question paper and rubrics are required
                if not question_paper:
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         error='Question paper PDF is required')
                if not rubrics or not rubrics.filename:
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         error='Rubrics PDF is required for rubric-based marking')
            else:
                # For standard: question paper and answer key are required
                if not question_paper or not answer_key:
                    return render_template('teacher_create_assignment.html',
                                         teacher=teacher,
                                         classes=classes,
                                         teaching_groups=teaching_groups,
                                         error='Both question paper and answer key PDFs are required')
            
            # Validate file types
            if not question_paper.filename.lower().endswith('.pdf'):
                return render_template('teacher_create_assignment.html',
                                     teacher=teacher,
                                     classes=classes,
                                     teaching_groups=teaching_groups,
                                     error='Question paper must be a PDF file')
            
            if answer_key and answer_key.filename and not answer_key.filename.lower().endswith('.pdf'):
                return render_template('teacher_create_assignment.html',
                                     teacher=teacher,
                                     classes=classes,
                                     teaching_groups=teaching_groups,
                                     error='Answer key must be a PDF file')
            
            assignment_id = generate_assignment_id()
            total_marks = int(data.get('total_marks', 100))
            assignment_title = data.get('title', 'Untitled')
            
            # Get optional files
            reference_materials = request.files.get('reference_materials')
            # Note: rubrics was already fetched above during validation
            
            # Read file contents (need to read before storing in GridFS since we also upload to Drive)
            question_paper_content = question_paper.read()
            question_paper.seek(0)  # Reset for GridFS
            
            # Answer key may be optional for rubric-based marking
            answer_key_content = None
            if answer_key and answer_key.filename:
                answer_key_content = answer_key.read()
                answer_key.seek(0)  # Reset for GridFS
            
            # Read optional file contents
            reference_materials_content = None
            if reference_materials and reference_materials.filename and reference_materials.filename.lower().endswith('.pdf'):
                reference_materials_content = reference_materials.read()
                reference_materials.seek(0)
            
            rubrics_content = None
            if rubrics and rubrics.filename and rubrics.filename.lower().endswith('.pdf'):
                rubrics_content = rubrics.read()
                rubrics.seek(0)
            
            # Extract text from PDFs for cost-effective AI processing
            question_paper_text = extract_text_from_pdf(question_paper_content)
            answer_key_text = extract_text_from_pdf(answer_key_content) if answer_key_content else ""
            reference_materials_text = extract_text_from_pdf(reference_materials_content) if reference_materials_content else ""
            rubrics_text = extract_text_from_pdf(rubrics_content) if rubrics_content else ""
            
            # Store files in GridFS
            from gridfs import GridFS
            fs = GridFS(db.db)
            
            # Save question paper
            question_paper_id = fs.put(
                question_paper_content,
                filename=f"{assignment_id}_question.pdf",
                content_type='application/pdf',
                assignment_id=assignment_id,
                file_type='question_paper'
            )
            
            # Save answer key (optional for rubric-based marking)
            answer_key_id = None
            if answer_key_content:
                answer_key_id = fs.put(
                    answer_key_content,
                    filename=f"{assignment_id}_answer.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='answer_key'
                )
            
            # Save reference materials if provided
            reference_materials_id = None
            if reference_materials_content:
                reference_materials_id = fs.put(
                    reference_materials_content,
                    filename=f"{assignment_id}_reference.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='reference_materials'
                )
            
            # Save rubrics if provided
            rubrics_id = None
            if rubrics_content:
                rubrics_id = fs.put(
                    rubrics_content,
                    filename=f"{assignment_id}_rubrics.pdf",
                    content_type='application/pdf',
                    assignment_id=assignment_id,
                    file_type='rubrics'
                )
            
            # Initialize Google Drive folder IDs
            drive_folders = None
            drive_files = None
            
            # Create Google Drive folder structure if teacher has Drive configured
            if teacher.get('google_drive_folder_id'):
                try:
                    from utils.google_drive import create_assignment_folder_structure, upload_question_papers
                    
                    # Create folder structure
                    drive_folders = create_assignment_folder_structure(
                        teacher=teacher,
                        assignment_title=assignment_title,
                        assignment_id=assignment_id
                    )
                    
                    # Upload question papers to Drive
                    if drive_folders and drive_folders.get('question_papers_folder_id'):
                        drive_files = upload_question_papers(
                            teacher=teacher,
                            question_papers_folder_id=drive_folders['question_papers_folder_id'],
                            question_paper_content=question_paper_content,
                            question_paper_name=question_paper.filename,
                            answer_key_content=answer_key_content,
                            answer_key_name=answer_key.filename
                        )
                        logger.info(f"Uploaded question papers to Google Drive for assignment {assignment_id}")
                except Exception as drive_error:
                    logger.warning(f"Google Drive upload failed (continuing anyway): {drive_error}")
            
            # Get teacher's default AI model if not specified
            teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
            default_model = teacher.get('default_ai_model', 'anthropic') if teacher else 'anthropic'
            ai_model = data.get('ai_model', default_model)
            
            # Get assignment target (class or teaching group)
            target_type = data.get('target_type', 'class')
            target_class_id = data.get('target_class_id', '').strip() or None
            target_group_id = data.get('target_group_id', '').strip() or None
            
            # Build assignment document
            assignment_doc = {
                'assignment_id': assignment_id,
                'teacher_id': session['teacher_id'],
                'title': assignment_title,
                'subject': data.get('subject', 'General'),
                'instructions': data.get('instructions', ''),
                'total_marks': total_marks,
                'marking_type': marking_type,  # 'standard' or 'rubric'
                'question_paper_id': question_paper_id,
                'answer_key_id': answer_key_id,
                'question_paper_name': question_paper.filename,
                'answer_key_name': answer_key.filename if answer_key and answer_key.filename else None,
                # New optional document fields
                'reference_materials_id': reference_materials_id,
                'reference_materials_name': reference_materials.filename if reference_materials_content else None,
                'rubrics_id': rubrics_id,
                'rubrics_name': rubrics.filename if rubrics_content else None,
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
                'created_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            }
            
            # Add Google Drive folder IDs if created
            if drive_folders:
                assignment_doc['drive_folders'] = drive_folders
            if drive_files:
                assignment_doc['drive_files'] = drive_files
            
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
                                 error=f'Failed to create assignment: {str(e)}')
    
    return render_template('teacher_create_assignment.html',
                         teacher=teacher,
                         classes=classes,
                         teaching_groups=teaching_groups)

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
            
            update_data = {
                'title': data.get('title', assignment['title']),
                'subject': data.get('subject', assignment['subject']),
                'instructions': data.get('instructions', ''),
                'total_marks': int(data.get('total_marks', assignment.get('total_marks', 100))),
                'due_date': data.get('due_date') or None,
                'status': 'published' if data.get('publish') else 'draft',
                'ai_model': ai_model,
                'feedback_instructions': data.get('feedback_instructions', ''),
                'grading_instructions': data.get('grading_instructions', ''),
                'target_type': target_type,
                'target_class_id': target_class_id if target_type == 'class' else None,
                'target_group_id': target_group_id if target_type == 'teaching_group' else None,
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
                         teaching_groups=teaching_groups)

@app.route('/teacher/assignments/<assignment_id>/file/<file_type>')
@teacher_required
def download_assignment_file(assignment_id, file_type):
    """Download assignment PDF file"""
    from gridfs import GridFS
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
        
        return jsonify({'success': True, 'message': 'Feedback saved'})
        
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        return jsonify({'error': str(e)}), 500

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
        result = extract_answers_from_key(file_content, file_type, question_count, teacher)
        
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

@app.route('/teacher/review/<submission_id>/save-rubric', methods=['POST'])
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
        
        return jsonify({'success': True, 'message': 'Rubric feedback saved'})
        
    except Exception as e:
        logger.error(f"Error saving rubric feedback: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/teacher/review/<submission_id>/send-rubric', methods=['POST'])
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
        
        return jsonify({'success': True, 'message': 'Essay feedback sent to student'})
        
    except Exception as e:
        logger.error(f"Error sending rubric feedback: {e}")
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
            
            # Update Google Drive folder
            if data.get('google_drive_folder_id'):
                update_data['google_drive_folder_id'] = data['google_drive_folder_id']
            
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
    """Get list of students with optional class filter"""
    try:
        class_filter = request.args.get('class', '')
        
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
        
        # Reset password to student ID
        new_password = student_id
        hashed = hash_password(new_password)
        
        db.db.students.update_one(
            {'student_id': student_id},
            {'$set': {'password_hash': hashed}}
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
        
        # Update all selected students
        result = db.db.students.update_many(
            {'student_id': {'$in': student_ids}},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow()}}
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
        
        # Verify teacher has access to these students
        teacher = Teacher.find_one({'teacher_id': session['teacher_id']})
        teacher_classes = teacher.get('classes', [])
        
        # Get students that belong to teacher's classes
        valid_students = list(Student.find({
            'student_id': {'$in': student_ids},
            '$or': [
                {'class': {'$in': teacher_classes}},
                {'classes': {'$in': teacher_classes}},
                {'teachers': session['teacher_id']}
            ]
        }))
        
        valid_ids = [s['student_id'] for s in valid_students]
        
        if not valid_ids:
            return jsonify({'error': 'No valid students found'}), 400
        
        # Default password if not specified
        default_password = custom_password if custom_password else 'student123'
        hashed = hash_password(default_password)
        
        # Update valid students
        result = db.db.students.update_many(
            {'student_id': {'$in': valid_ids}},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow()}}
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
        
        if len(new_password) < 4:
            return jsonify({'error': 'New password must be at least 4 characters'}), 400
        
        student = Student.find_one({'student_id': session['student_id']})
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        # Verify current password
        if not verify_password(current_password, student.get('password_hash', '')):
            return jsonify({'error': 'Current password is incorrect'}), 400
        
        # Update password
        hashed = hash_password(new_password)
        Student.update_one(
            {'student_id': session['student_id']},
            {'$set': {'password_hash': hashed, 'updated_at': datetime.utcnow()}}
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
    """Create a new teaching group"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        class_id = data.get('class_id')
        teacher_id = data.get('teacher_id')
        student_ids = data.get('student_ids', [])
        
        if not name or not class_id or not teacher_id:
            return jsonify({'error': 'Name, class, and teacher are required'}), 400
        
        if not student_ids:
            return jsonify({'error': 'At least one student must be selected'}), 400
        
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
        
        return jsonify({
            'success': True,
            'group_id': group_id,
            'message': f'Teaching group "{name}" created with {len(student_ids)} students'
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
                
                Student.insert_one({
                    'student_id': student_id,
                    'name': name,
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
