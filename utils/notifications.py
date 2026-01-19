import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def notify_submission_ready(submission: dict, assignment: dict, student: dict, teacher: dict):
    """
    Notify teacher when a student submission is ready for review
    
    This function imports bot_handler to avoid circular imports
    """
    try:
        from bot_handler import send_notification
        
        telegram_id = teacher.get('telegram_id')
        if not telegram_id:
            logger.warning(f"Teacher {teacher.get('teacher_id')} has no Telegram ID linked")
            return False
        
        data = {
            'student_name': student.get('name', 'Unknown'),
            'student_class': student.get('class'),
            'student_id': student.get('student_id', 'N/A'),
            'assignment_title': assignment.get('title', 'Untitled'),
            'subject': assignment.get('subject', 'N/A'),
            'submitted_at': submission.get('submitted_at', datetime.utcnow()).strftime('%d %b %Y, %H:%M') if isinstance(submission.get('submitted_at'), datetime) else str(submission.get('submitted_at', 'Just now')),
            'submission_id': submission.get('submission_id', '')
        }
        
        return send_notification(telegram_id, 'new_submission', data)
        
    except ImportError:
        logger.warning("bot_handler not available for notifications")
        return False
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        return False

def notify_feedback_ready(submission: dict, assignment: dict, student: dict):
    """
    Notify student when their submission has been reviewed
    
    Note: This would require students to have Telegram linked as well,
    or could be implemented as an email notification
    """
    try:
        from bot_handler import send_notification
        
        # For now, we'll log this - in a full implementation,
        # students could also link their Telegram accounts
        logger.info(f"Feedback ready for {student.get('name')} on {assignment.get('title')}")
        
        # Could implement email notification here
        # email = student.get('email')
        # if email:
        #     send_email_notification(email, submission, assignment)
        
        return True
        
    except Exception as e:
        logger.error(f"Error sending feedback notification: {e}")
        return False

def notify_new_message(teacher: dict, student: dict, message: str):
    """
    Notify teacher of a new message from student
    """
    try:
        from bot_handler import send_to_teacher
        
        telegram_id = teacher.get('telegram_id')
        if not telegram_id:
            logger.warning(f"Teacher {teacher.get('teacher_id')} has no Telegram ID linked")
            return False
        
        return send_to_teacher(
            telegram_id=telegram_id,
            student_name=student.get('name', 'Unknown'),
            message=message,
            teacher_id=teacher.get('teacher_id', ''),
            student_class=student.get('class')
        )
        
    except ImportError:
        logger.warning("bot_handler not available for notifications")
        return False
    except Exception as e:
        logger.error(f"Error sending message notification: {e}")
        return False

def notify_assignment_published(assignment: dict, students: list, teacher: dict):
    """
    Notify students when a new assignment is published
    
    This would require student Telegram integration
    """
    logger.info(f"New assignment '{assignment.get('title')}' published by {teacher.get('name')}")
    logger.info(f"Would notify {len(students)} students")
    
    # In a full implementation, this could:
    # 1. Send Telegram messages to students who have linked accounts
    # 2. Send email notifications
    # 3. Push notifications via a mobile app
    
    return True
