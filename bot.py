import os
import logging
import re
import base64
import io
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId
from gridfs import GridFS

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')

# Conversation states
SELECTING_ASSIGNMENT, UPLOADING_PAGES, CONFIRMING_SUBMIT = range(3)

# Initialize MongoDB connection
client = None
db = None
fs = None

def init_db():
    global client, db, fs
    mongo_uri = MONGODB_URI or os.getenv('MONGO_URL')
    if mongo_uri:
        client = MongoClient(mongo_uri)
        db_name = os.getenv('MONGODB_DB', 'school_portal')
        db = client.get_database(db_name)
        fs = GridFS(db)
        logger.info("Connected to MongoDB")
    else:
        logger.error("MONGODB_URI or MONGO_URL not set")

def generate_submission_id():
    """Generate a unique submission ID"""
    import random
    import string
    return 'SUB' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and show options"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Check if this is a linked student
    student = None
    if db:
        student = db.students.find_one({'telegram_id': chat_id})
    
    if student:
        welcome_message = f"""👋 Welcome back, {student.get('name', 'Student')}!

📚 *Your Commands:*
/assignments - View your assignments
/submit - Submit an assignment
/mystatus - Check your submission status
/help - Show help

📸 *Quick Submit:*
Just send a photo or PDF of your work and I'll guide you through the submission!"""
    else:
        welcome_message = f"""👋 Welcome to the School Portal Bot!

Your Telegram ID: `{chat_id}`

*For Students:*
Use `/link <student_id>` to connect your account.

*For Teachers:*
Use `/verify <teacher_id>` to link your account.

*Commands:*
/link <student_id> - Link student account
/verify <teacher_id> - Link teacher account
/help - Show help"""
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def link_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link Telegram ID to student account"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide your student ID.\n\n"
            "Usage: `/link S001`",
            parse_mode='Markdown'
        )
        return
    
    student_id = context.args[0].upper()
    chat_id = update.effective_chat.id
    
    student = db.students.find_one({'student_id': student_id})
    
    if not student:
        await update.message.reply_text(f"❌ Student ID `{student_id}` not found.", parse_mode='Markdown')
        return
    
    # Check if already linked
    if student.get('telegram_id') and student['telegram_id'] != chat_id:
        await update.message.reply_text("⚠️ This student ID is already linked to another Telegram account.")
        return
    
    # Link the account
    db.students.update_one(
        {'student_id': student_id},
        {'$set': {'telegram_id': chat_id, 'telegram_linked_at': datetime.utcnow()}}
    )
    
    await update.message.reply_text(
        f"✅ Account linked successfully!\n\n"
        f"Welcome, {student.get('name', 'Student')}!\n"
        f"Class: {student.get('class', 'N/A')}\n\n"
        "You can now:\n"
        "📚 /assignments - View assignments\n"
        "📤 /submit - Submit work\n"
        "📸 Send photos/PDFs to submit"
    )

async def list_student_assignments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available assignments for the student"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return
    
    chat_id = update.effective_chat.id
    student = db.students.find_one({'telegram_id': chat_id})
    
    if not student:
        await update.message.reply_text(
            "⚠️ Your account is not linked.\nUse `/link <student_id>` first.",
            parse_mode='Markdown'
        )
        return
    
    # Get assignments from student's teachers
    teacher_ids = student.get('teachers', [])
    assignments = list(db.assignments.find({
        'teacher_id': {'$in': teacher_ids},
        'status': 'published'
    }).sort('created_at', -1).limit(10))
    
    if not assignments:
        await update.message.reply_text("📚 No assignments available yet.")
        return
    
    # Check submission status for each
    message = "📚 *Your Assignments*\n\n"
    
    for a in assignments:
        submission = db.submissions.find_one({
            'assignment_id': a['assignment_id'],
            'student_id': student['student_id']
        })
        
        if submission:
            if submission['status'] == 'reviewed':
                status = "✅ Reviewed"
            elif submission['status'] == 'ai_reviewed':
                status = "🤖 AI Reviewed"
            else:
                status = "📤 Submitted"
        else:
            status = "⏳ Pending"
        
        due_str = ""
        if a.get('due_date'):
            due_str = f" | Due: {a['due_date']}"
        
        message += f"*{a.get('title', 'Untitled')}*\n"
        message += f"  📖 {a.get('subject', 'General')}{due_str}\n"
        message += f"  Status: {status}\n"
        message += f"  ID: `{a['assignment_id']}`\n\n"
    
    message += "To submit: `/submit <assignment_id>`\nOr just send a photo/PDF!"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def start_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the submission process"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return ConversationHandler.END
    
    chat_id = update.effective_chat.id
    student = db.students.find_one({'telegram_id': chat_id})
    
    if not student:
        await update.message.reply_text(
            "⚠️ Your account is not linked.\nUse `/link <student_id>` first.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Get available assignments
    teacher_ids = student.get('teachers', [])
    assignments = list(db.assignments.find({
        'teacher_id': {'$in': teacher_ids},
        'status': 'published'
    }).sort('created_at', -1).limit(10))
    
    if not assignments:
        await update.message.reply_text("📚 No assignments available to submit.")
        return ConversationHandler.END
    
    # Create inline keyboard with assignments
    keyboard = []
    for a in assignments:
        # Check if already submitted
        existing = db.submissions.find_one({
            'assignment_id': a['assignment_id'],
            'student_id': student['student_id']
        })
        label = f"{'✅ ' if existing else ''}{a.get('title', 'Untitled')}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"assign_{a['assignment_id']}")])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📚 *Select Assignment to Submit*\n\n"
        "Choose the assignment you want to submit:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    context.user_data['student'] = student
    return SELECTING_ASSIGNMENT

async def assignment_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle assignment selection"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        await query.edit_message_text("❌ Submission cancelled.")
        return ConversationHandler.END
    
    assignment_id = query.data.replace("assign_", "")
    assignment = db.assignments.find_one({'assignment_id': assignment_id})
    
    if not assignment:
        await query.edit_message_text("❌ Assignment not found.")
        return ConversationHandler.END
    
    context.user_data['assignment'] = assignment
    context.user_data['pages'] = []
    
    await query.edit_message_text(
        f"📝 *{assignment.get('title', 'Assignment')}*\n"
        f"Subject: {assignment.get('subject', 'General')}\n"
        f"Total Marks: {assignment.get('total_marks', 100)}\n\n"
        "📸 *Now send your work:*\n"
        "• Send photos one by one (each page)\n"
        "• Or send a PDF file\n\n"
        "When done, type /done to submit.\n"
        "Type /cancel to cancel.",
        parse_mode='Markdown'
    )
    
    return UPLOADING_PAGES

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a photo submission"""
    if 'assignment' not in context.user_data:
        # Direct photo without starting submission flow
        chat_id = update.effective_chat.id
        student = db.students.find_one({'telegram_id': chat_id})
        
        if not student:
            await update.message.reply_text(
                "⚠️ Please link your account first with `/link <student_id>`",
                parse_mode='Markdown'
            )
            return
        
        # Prompt to select assignment
        await update.message.reply_text(
            "📸 Got your photo!\n\n"
            "Please use `/submit` to select which assignment this is for."
        )
        return
    
    # Get the photo with best quality
    photo = update.message.photo[-1]
    file = await photo.get_file()
    
    # Download the photo
    photo_bytes = await file.download_as_bytearray()
    
    # Store in context
    page_num = len(context.user_data['pages']) + 1
    context.user_data['pages'].append({
        'type': 'image',
        'data': bytes(photo_bytes),
        'page_num': page_num,
        'file_id': photo.file_id
    })
    
    await update.message.reply_text(
        f"✅ Page {page_num} received!\n\n"
        f"📄 Total pages: {page_num}\n\n"
        "• Send more photos\n"
        "• Or type /done to submit"
    )
    
    return UPLOADING_PAGES

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a PDF submission"""
    document = update.message.document
    
    if not document.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("⚠️ Please send a PDF file or photos.")
        return UPLOADING_PAGES if 'assignment' in context.user_data else None
    
    chat_id = update.effective_chat.id
    student = db.students.find_one({'telegram_id': chat_id})
    
    if not student:
        await update.message.reply_text(
            "⚠️ Please link your account first with `/link <student_id>`",
            parse_mode='Markdown'
        )
        return ConversationHandler.END if 'assignment' in context.user_data else None
    
    if 'assignment' not in context.user_data:
        context.user_data['student'] = student
        context.user_data['pages'] = []
        
        # Get available assignments
        teacher_ids = student.get('teachers', [])
        assignments = list(db.assignments.find({
            'teacher_id': {'$in': teacher_ids},
            'status': 'published'
        }).sort('created_at', -1).limit(10))
        
        if not assignments:
            await update.message.reply_text("📚 No assignments available.")
            return None
        
        # Download PDF first
        file = await document.get_file()
        pdf_bytes = await file.download_as_bytearray()
        
        context.user_data['pages'].append({
            'type': 'pdf',
            'data': bytes(pdf_bytes),
            'filename': document.file_name
        })
        
        # Ask for assignment selection
        keyboard = []
        for a in assignments:
            keyboard.append([InlineKeyboardButton(a.get('title', 'Untitled'), callback_data=f"pdfassign_{a['assignment_id']}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📄 PDF received!\n\n"
            "Select the assignment for this submission:",
            reply_markup=reply_markup
        )
        
        return SELECTING_ASSIGNMENT
    
    # Already in submission flow
    file = await document.get_file()
    pdf_bytes = await file.download_as_bytearray()
    
    context.user_data['pages'] = [{
        'type': 'pdf',
        'data': bytes(pdf_bytes),
        'filename': document.file_name
    }]
    
    await update.message.reply_text(
        f"✅ PDF received: {document.file_name}\n\n"
        "Type /done to submit or /cancel to cancel."
    )
    
    return UPLOADING_PAGES

async def pdf_assignment_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle assignment selection after PDF upload"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        await query.edit_message_text("❌ Submission cancelled.")
        context.user_data.clear()
        return ConversationHandler.END
    
    assignment_id = query.data.replace("pdfassign_", "")
    assignment = db.assignments.find_one({'assignment_id': assignment_id})
    
    if not assignment:
        await query.edit_message_text("❌ Assignment not found.")
        return ConversationHandler.END
    
    context.user_data['assignment'] = assignment
    
    await query.edit_message_text(
        f"📝 *{assignment.get('title', 'Assignment')}*\n\n"
        "✅ PDF ready to submit.\n\n"
        "Type /done to submit or /cancel to cancel.",
        parse_mode='Markdown'
    )
    
    return UPLOADING_PAGES

async def finish_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finalize and process the submission"""
    if 'assignment' not in context.user_data or not context.user_data.get('pages'):
        await update.message.reply_text("❌ No submission in progress or no files uploaded.")
        return ConversationHandler.END
    
    student = context.user_data['student']
    assignment = context.user_data['assignment']
    pages = context.user_data['pages']
    
    await update.message.reply_text("⏳ Processing your submission...")
    
    try:
        submission_id = generate_submission_id()
        
        # Store files in GridFS
        file_ids = []
        for i, page in enumerate(pages):
            file_id = fs.put(
                page['data'],
                filename=f"{submission_id}_page_{i+1}.{'pdf' if page['type'] == 'pdf' else 'jpg'}",
                content_type='application/pdf' if page['type'] == 'pdf' else 'image/jpeg',
                submission_id=submission_id,
                page_num=i + 1
            )
            file_ids.append(str(file_id))
        
        # Create submission record
        submission = {
            'submission_id': submission_id,
            'student_id': student['student_id'],
            'assignment_id': assignment['assignment_id'],
            'teacher_id': assignment['teacher_id'],
            'file_ids': file_ids,
            'file_type': pages[0]['type'],
            'page_count': len(pages),
            'status': 'submitted',
            'submitted_at': datetime.utcnow(),
            'submitted_via': 'telegram',
            'ai_feedback': None,
            'teacher_feedback': None
        }
        
        db.submissions.insert_one(submission)
        
        # Get AI feedback
        await update.message.reply_text("🤖 Generating AI feedback...")
        
        try:
            from utils.ai_marking import analyze_submission_images
            teacher = db.teachers.find_one({'teacher_id': assignment['teacher_id']})
            
            # Get answer key content
            answer_key_content = None
            if assignment.get('answer_key_id'):
                try:
                    answer_file = fs.get(assignment['answer_key_id'])
                    answer_key_content = answer_file.read()
                except:
                    pass
            
            ai_result = analyze_submission_images(
                pages,
                assignment,
                answer_key_content,
                teacher
            )
            
            # Update submission with AI feedback
            db.submissions.update_one(
                {'submission_id': submission_id},
                {'$set': {
                    'ai_feedback': ai_result,
                    'status': 'ai_reviewed'
                }}
            )
            
            # Format feedback message
            feedback_msg = format_ai_feedback(ai_result, assignment)
            
            await update.message.reply_text(
                f"✅ *Submission Complete!*\n\n"
                f"📝 Assignment: {assignment.get('title')}\n"
                f"🆔 Submission ID: `{submission_id}`\n\n"
                f"---\n"
                f"🤖 *AI Feedback:*\n\n{feedback_msg}\n\n"
                f"---\n"
                f"⏳ Waiting for teacher review.",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"AI feedback error: {e}")
            await update.message.reply_text(
                f"✅ *Submission Complete!*\n\n"
                f"📝 Assignment: {assignment.get('title')}\n"
                f"🆔 Submission ID: `{submission_id}`\n\n"
                f"⏳ Waiting for teacher review.\n"
                f"(AI feedback unavailable)",
                parse_mode='Markdown'
            )
        
        # Notify teacher
        await notify_teacher_submission(assignment['teacher_id'], student, assignment, submission_id)
        
    except Exception as e:
        logger.error(f"Submission error: {e}")
        await update.message.reply_text(f"❌ Error submitting: {str(e)}")
    
    context.user_data.clear()
    return ConversationHandler.END

def format_ai_feedback(ai_result: dict, assignment: dict) -> str:
    """Format AI feedback for Telegram message"""
    if not ai_result or ai_result.get('error'):
        return "Unable to generate feedback."
    
    msg = ""
    
    # Question-by-question feedback
    questions = ai_result.get('questions', [])
    for q in questions:
        status = "✅" if q.get('is_correct') else "❌" if q.get('is_correct') == False else "❓"
        msg += f"\n{status} *Q{q.get('question_num', '?')}*"
        if q.get('marks_awarded') is not None:
            msg += f" ({q.get('marks_awarded')}/{q.get('marks_total', '?')})"
        msg += "\n"
        
        if q.get('feedback'):
            msg += f"   {q.get('feedback')}\n"
        if q.get('needs_review'):
            msg += f"   ⚠️ _Needs teacher review_\n"
    
    # Overall
    if ai_result.get('total_marks'):
        msg += f"\n📊 *Total: {ai_result.get('total_marks')}/{assignment.get('total_marks', 100)}*"
    
    if ai_result.get('overall_feedback'):
        msg += f"\n\n💡 {ai_result.get('overall_feedback')}"
    
    return msg or "Processing complete."

async def notify_teacher_submission(teacher_id: str, student: dict, assignment: dict, submission_id: str):
    """Notify teacher of new submission"""
    teacher = db.teachers.find_one({'teacher_id': teacher_id})
    if not teacher or not teacher.get('telegram_id'):
        return
    
    try:
        bot = Bot(token=BOT_TOKEN)
        web_url = os.getenv('WEB_URL', 'http://localhost:5000')
        
        message = (
            f"📬 *New Submission*\n\n"
            f"👤 Student: {student.get('name')} ({student.get('student_id')})\n"
            f"📝 Assignment: {assignment.get('title')}\n"
            f"🕐 Time: {datetime.utcnow().strftime('%d %b %H:%M')}\n\n"
            f"[Review Submission]({web_url}/teacher/review/{submission_id})"
        )
        
        await bot.send_message(
            chat_id=teacher['telegram_id'],
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to notify teacher: {e}")

async def cancel_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the submission process"""
    context.user_data.clear()
    await update.message.reply_text("❌ Submission cancelled.")
    return ConversationHandler.END

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check student's submission status"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return
    
    chat_id = update.effective_chat.id
    student = db.students.find_one({'telegram_id': chat_id})
    
    if not student:
        await update.message.reply_text("⚠️ Account not linked. Use `/link <student_id>`", parse_mode='Markdown')
        return
    
    # Get recent submissions
    submissions = list(db.submissions.find({
        'student_id': student['student_id']
    }).sort('submitted_at', -1).limit(5))
    
    if not submissions:
        await update.message.reply_text("📚 No submissions yet.")
        return
    
    message = "📊 *Your Recent Submissions*\n\n"
    
    for sub in submissions:
        assignment = db.assignments.find_one({'assignment_id': sub['assignment_id']})
        title = assignment.get('title', 'Assignment') if assignment else 'Unknown'
        
        status_emoji = {
            'submitted': '📤',
            'ai_reviewed': '🤖',
            'reviewed': '✅',
            'returned': '📬'
        }.get(sub['status'], '❓')
        
        marks = ""
        if sub.get('final_marks') is not None:
            marks = f" | Marks: {sub['final_marks']}"
        
        time_str = sub.get('submitted_at', datetime.utcnow()).strftime('%d %b')
        
        message += f"{status_emoji} *{title}*\n"
        message += f"   {time_str}{marks}\n\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# ============== TEACHER COMMANDS ==============

async def verify_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link Telegram ID to teacher account"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide your teacher ID.\n\n"
            "Usage: /verify your_teacher_id"
        )
        return
    
    teacher_id_input = context.args[0]
    chat_id = update.effective_chat.id
    
    teacher = db.teachers.find_one({
        'teacher_id': {'$regex': f'^{re.escape(teacher_id_input)}$', '$options': 'i'}
    })
    
    if not teacher:
        all_teachers = list(db.teachers.find({}, {'teacher_id': 1}))
        teacher_list = ", ".join([t.get('teacher_id', '') for t in all_teachers[:5]])
        
        await update.message.reply_text(
            f"❌ Teacher ID '{teacher_id_input}' not found.\n\n"
            f"Available: {teacher_list if teacher_list else 'None'}"
        )
        return
    
    teacher_id = teacher['teacher_id']
    
    existing = db.teachers.find_one({'telegram_id': chat_id})
    if existing and existing['teacher_id'] != teacher_id:
        await update.message.reply_text(f"⚠️ Already linked to '{existing['teacher_id']}'.")
        return
    
    db.teachers.update_one(
        {'teacher_id': teacher_id},
        {'$set': {'telegram_id': chat_id, 'telegram_verified_at': datetime.utcnow()}}
    )
    
    await update.message.reply_text(
        f"✅ *Verification Complete!*\n\n"
        f"Welcome, {teacher.get('name', 'Teacher')}!\n\n"
        "You will receive:\n"
        "📬 Submission notifications\n"
        "📱 Student messages",
        parse_mode='Markdown'
    )

async def list_students(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show teacher's students"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return
    
    chat_id = update.effective_chat.id
    teacher = db.teachers.find_one({'telegram_id': chat_id})
    
    if not teacher:
        await update.message.reply_text("⚠️ Not linked. Use `/verify <teacher_id>`", parse_mode='Markdown')
        return
    
    students = list(db.students.find({'teachers': teacher['teacher_id']}))
    
    if not students:
        await update.message.reply_text("📚 No students assigned.")
        return
    
    by_class = {}
    for s in students:
        cls = s.get('class', 'Unknown')
        if cls not in by_class:
            by_class[cls] = []
        by_class[cls].append(s)
    
    message = f"👨‍🏫 *Your Students* ({len(students)})\n\n"
    
    for cls in sorted(by_class.keys()):
        message += f"📖 *{cls}*\n"
        for s in sorted(by_class[cls], key=lambda x: x.get('name', '')):
            linked = "📱" if s.get('telegram_id') else ""
            message += f"  • {s.get('name', 'Unknown')} {linked}\n"
        message += "\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def list_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending submissions"""
    if db is None:
        await update.message.reply_text("❌ Database not connected.")
        return
    
    chat_id = update.effective_chat.id
    teacher = db.teachers.find_one({'telegram_id': chat_id})
    
    if not teacher:
        await update.message.reply_text("⚠️ Not linked. Use `/verify <teacher_id>`", parse_mode='Markdown')
        return
    
    assignments = list(db.assignments.find({'teacher_id': teacher['teacher_id']}))
    assignment_ids = [a['assignment_id'] for a in assignments]
    assignment_map = {a['assignment_id']: a for a in assignments}
    
    pending = list(db.submissions.find({
        'assignment_id': {'$in': assignment_ids},
        'status': {'$in': ['submitted', 'ai_reviewed']}
    }).sort('submitted_at', -1).limit(20))
    
    if not pending:
        await update.message.reply_text("✅ No pending submissions!")
        return
    
    web_url = os.getenv('WEB_URL', 'http://localhost:5000')
    message = f"📝 *Pending Reviews* ({len(pending)})\n\n"
    
    for sub in pending:
        assignment = assignment_map.get(sub['assignment_id'], {})
        student = db.students.find_one({'student_id': sub['student_id']})
        student_name = student.get('name', 'Unknown') if student else 'Unknown'
        
        status_emoji = '🤖' if sub['status'] == 'ai_reviewed' else '⏳'
        
        message += f"{status_emoji} *{assignment.get('title', 'Assignment')}*\n"
        message += f"   👤 {student_name}\n"
        message += f"   [Review]({web_url}/teacher/review/{sub['submission_id']})\n\n"
    
    await update.message.reply_text(message, parse_mode='Markdown', disable_web_page_preview=True)

async def handle_teacher_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process replies to student messages"""
    if db is None:
        return
    
    chat_id = update.effective_chat.id
    teacher = db.teachers.find_one({'telegram_id': chat_id})
    
    if not teacher:
        return
    
    if not update.message.reply_to_message:
        return
    
    original_text = update.message.reply_to_message.text
    if not original_text or '📱' not in original_text:
        return
    
    match = re.match(r'📱\s*([^:]+):', original_text)
    if not match:
        return
    
    student_name = match.group(1).strip()
    reply_text = update.message.text
    
    student = db.students.find_one({'name': {'$regex': student_name, '$options': 'i'}})
    
    if not student:
        await update.message.reply_text(f"⚠️ Student '{student_name}' not found.")
        return
    
    db.messages.insert_one({
        'student_id': student['student_id'],
        'teacher_id': teacher['teacher_id'],
        'message': reply_text,
        'from_student': False,
        'timestamp': datetime.utcnow(),
        'read': False,
        'sent_via': 'telegram'
    })
    
    # Notify student if they have Telegram linked
    if student.get('telegram_id'):
        try:
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id=student['telegram_id'],
                text=f"📨 *Message from {teacher.get('name', 'Teacher')}:*\n\n{reply_text}",
                parse_mode='Markdown'
            )
        except:
            pass
    
    await update.message.reply_text(f"✅ Reply sent to {student_name}!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help"""
    chat_id = update.effective_chat.id
    
    # Check if student or teacher
    is_student = db and db.students.find_one({'telegram_id': chat_id})
    is_teacher = db and db.teachers.find_one({'telegram_id': chat_id})
    
    if is_student:
        help_text = """📚 *Student Commands*

/assignments - View your assignments
/submit - Submit an assignment
/mystatus - Check submission status
/help - Show this help

*Quick Submit:*
Send a photo or PDF directly!"""
    elif is_teacher:
        help_text = """👨‍🏫 *Teacher Commands*

/students - View your students
/submissions - Pending submissions
/help - Show this help

*Reply to messages* to respond to students!"""
    else:
        help_text = """📚 *School Portal Bot*

/link <student_id> - Link student account
/verify <teacher_id> - Link teacher account
/help - Show this help"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unknown commands"""
    await update.message.reply_text("❓ Unknown command. Use /help")

def main():
    """Initialize and run the bot"""
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return
    
    init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Submission conversation handler
    submission_handler = ConversationHandler(
        entry_points=[
            CommandHandler("submit", start_submission),
            MessageHandler(filters.Document.PDF, receive_document),
        ],
        states={
            SELECTING_ASSIGNMENT: [
                CallbackQueryHandler(assignment_selected, pattern="^assign_"),
                CallbackQueryHandler(pdf_assignment_selected, pattern="^pdfassign_"),
                CallbackQueryHandler(cancel_submission, pattern="^cancel$"),
            ],
            UPLOADING_PAGES: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Document.PDF, receive_document),
                CommandHandler("done", finish_submission),
                CommandHandler("cancel", cancel_submission),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_submission),
        ],
        per_message=False
    )
    
    application.add_handler(submission_handler)
    
    # Other handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("link", link_student))
    application.add_handler(CommandHandler("verify", verify_teacher))
    application.add_handler(CommandHandler("assignments", list_student_assignments))
    application.add_handler(CommandHandler("mystatus", check_status))
    application.add_handler(CommandHandler("students", list_students))
    application.add_handler(CommandHandler("submissions", list_submissions))
    application.add_handler(CommandHandler("help", help_command))
    
    # Handle teacher replies
    application.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND,
        handle_teacher_reply
    ))
    
    # Handle direct photos (outside conversation)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, receive_photo))
    
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown))
    
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
