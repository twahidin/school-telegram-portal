import os
import logging
from telegram import Bot
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

def send_to_teacher(telegram_id: int, student_name: str, message: str, teacher_id: str, student_class: str = None):
    """Send a message from a student to a teacher via Telegram"""
    if not bot:
        logger.error("Bot token not configured")
        return False
    
    try:
        if student_class:
            formatted_message = f"📱 {student_name} ({student_class}): {message}"
        else:
            formatted_message = f"📱 {student_name}: {message}"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            bot.send_message(chat_id=telegram_id, text=formatted_message)
        )
        loop.close()
        logger.info(f"Sent message from {student_name} to teacher {teacher_id}")
        return True
    except Exception as e:
        logger.error(f"Error sending message to teacher: {e}")
        return False

def send_notification(telegram_id: int, notification_type: str, data: dict):
    """Send a notification to a teacher via Telegram"""
    if not bot:
        logger.error("Bot token not configured")
        return False
    
    try:
        if notification_type == 'new_submission':
            web_url = os.getenv('WEB_URL', 'http://localhost:5000')
            student_display = data.get('student_name', 'Unknown')
            if data.get('student_class'):
                student_display = f"{student_display} ({data.get('student_class')})"
            message = f"""📚 *New Assignment Submission*

👤 Student: {student_display}
📝 Assignment: {data.get('assignment_title', 'Untitled')}
📖 Subject: {data.get('subject', 'N/A')}
🕐 Submitted: {data.get('submitted_at', 'Just now')}

🔗 [Review Submission]({web_url}/teacher/submissions/{data.get('submission_id', '')}/review)"""
        
        elif notification_type == 'new_message':
            student_display = data.get('student_name', 'Unknown')
            if data.get('student_class'):
                student_display = f"{student_display} ({data.get('student_class')})"
            message = f"""💬 *New Message*

👤 From: {student_display}
📝 Message: {data.get('message', '')}"""
        
        elif notification_type == 'assignment_reminder':
            message = f"""⏰ *Assignment Reminder*

📝 Assignment: {data.get('assignment_title', 'Untitled')}
📅 Due: {data.get('due_date', 'N/A')}
📊 Pending submissions: {data.get('pending_count', 0)}"""
        
        else:
            message = f"📢 Notification: {notification_type}\n{str(data)}"
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            bot.send_message(
                chat_id=telegram_id, 
                text=message, 
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
        )
        loop.close()
        logger.info(f"Sent {notification_type} notification to {telegram_id}")
        return True
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        return False

def send_reply_to_student(telegram_id: int, teacher_name: str, message: str):
    """Send a reply from teacher to student (if student has Telegram linked)"""
    if not bot:
        logger.error("Bot token not configured")
        return False
    
    try:
        formatted_message = f"📩 Reply from {teacher_name}:\n\n{message}"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            bot.send_message(chat_id=telegram_id, text=formatted_message)
        )
        loop.close()
        return True
    except Exception as e:
        logger.error(f"Error sending reply to student: {e}")
        return False
