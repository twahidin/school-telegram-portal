# Utility modules for School Portal
from utils.auth import (
    hash_password,
    verify_password,
    generate_assignment_id,
    generate_submission_id,
    generate_student_id,
    generate_teacher_id,
    encrypt_api_key,
    decrypt_api_key,
    generate_token
)

from utils.ai_marking import (
    get_teacher_ai_service,
    mark_submission,
    get_quick_feedback
)

from utils.google_drive import (
    get_drive_service,
    get_teacher_drive_manager,
    upload_assignment_file,
    DriveManager
)

from utils.pdf_generator import (
    generate_feedback_pdf,
    generate_assignment_pdf
)

from utils.notifications import (
    notify_submission_ready,
    notify_feedback_ready,
    notify_new_message,
    notify_assignment_published
)

from utils.push_notifications import (
    send_push_notification,
    send_assignment_notification,
    send_feedback_notification,
    send_message_notification,
    get_vapid_public_key,
    is_push_configured
)

__all__ = [
    # Auth
    'hash_password',
    'verify_password',
    'generate_assignment_id',
    'generate_submission_id',
    'generate_student_id',
    'generate_teacher_id',
    'encrypt_api_key',
    'decrypt_api_key',
    'generate_token',
    # AI Marking
    'get_teacher_ai_service',
    'mark_submission',
    'get_quick_feedback',
    # Google Drive
    'get_drive_service',
    'get_teacher_drive_manager',
    'upload_assignment_file',
    'DriveManager',
    # PDF Generator
    'generate_feedback_pdf',
    'generate_assignment_pdf',
    # Notifications
    'notify_submission_ready',
    'notify_feedback_ready',
    'notify_new_message',
    'notify_assignment_published',
    # Push Notifications
    'send_push_notification',
    'send_assignment_notification',
    'send_feedback_notification',
    'send_message_notification',
    'get_vapid_public_key',
    'is_push_configured'
]
