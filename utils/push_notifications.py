"""
Push Notifications utility for sending web push notifications to students.

Uses the Web Push Protocol with VAPID authentication.
Students must subscribe via their browser/PWA to receive notifications.
"""

import os
import json
import logging
from pywebpush import webpush, WebPushException

logger = logging.getLogger(__name__)

# VAPID keys - generate these once and store in environment variables
# Generate with: from py_vapid import Vapid; v = Vapid(); v.generate_keys(); print(v.private_pem(), v.public_key)
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY')
VAPID_CLAIMS = {
    "sub": os.getenv('VAPID_EMAIL', 'mailto:admin@school.edu')
}


def get_vapid_public_key() -> str:
    """Return the VAPID public key for client-side subscription"""
    return VAPID_PUBLIC_KEY


def is_push_configured() -> bool:
    """Check if push notifications are properly configured"""
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


def send_push_notification(subscription_info: dict, title: str, body: str, 
                           url: str = None, tag: str = None, data: dict = None) -> bool:
    """
    Send a push notification to a single subscriber.
    
    Args:
        subscription_info: The push subscription object from the browser
            {
                "endpoint": "https://...",
                "keys": {
                    "p256dh": "...",
                    "auth": "..."
                }
            }
        title: Notification title
        body: Notification body text
        url: URL to open when notification is clicked
        tag: Notification tag (for grouping/replacing)
        data: Additional data to pass to the service worker
    
    Returns:
        True if sent successfully, False otherwise
    """
    if not is_push_configured():
        logger.warning("Push notifications not configured - missing VAPID keys")
        return False
    
    if not subscription_info:
        logger.warning("No subscription info provided")
        return False
    
    payload = {
        "title": title,
        "body": body,
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/badge-72.png",
        "tag": tag or "school-notification",
        "data": {
            "url": url or "/",
            **(data or {})
        }
    }
    
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
        logger.info(f"Push notification sent: {title}")
        return True
        
    except WebPushException as e:
        logger.error(f"Push notification failed: {e}")
        # If subscription is invalid (410 Gone), return special code
        if e.response and e.response.status_code == 410:
            logger.info("Subscription expired or invalid")
            return None  # Signal to remove this subscription
        return False
        
    except Exception as e:
        logger.error(f"Unexpected error sending push: {e}")
        return False


def send_assignment_notification(db, assignment: dict, class_id: str = None, 
                                  teaching_group_id: str = None) -> dict:
    """
    Send push notifications to all students for a new assignment.
    
    Args:
        db: Database instance
        assignment: Assignment document
        class_id: Optional class ID to filter students
        teaching_group_id: Optional teaching group ID to filter students
    
    Returns:
        dict with counts: {"sent": N, "failed": N, "expired": N}
    """
    if not is_push_configured():
        logger.warning("Push notifications not configured")
        return {"sent": 0, "failed": 0, "expired": 0, "error": "Not configured"}
    
    students_col = db.db['students']
    
    # Build query to find students with push subscriptions
    query = {"push_subscription": {"$exists": True, "$ne": None}}
    
    if teaching_group_id:
        # Get students in teaching group
        groups_col = db.db['teaching_groups']
        group = groups_col.find_one({"group_id": teaching_group_id})
        if group and group.get('student_ids'):
            query["student_id"] = {"$in": group['student_ids']}
    elif class_id:
        # Get students in class
        query["$or"] = [
            {"class_id": class_id},
            {"classes": class_id}
        ]
    
    students = list(students_col.find(query))
    
    if not students:
        logger.info("No students with push subscriptions found")
        return {"sent": 0, "failed": 0, "expired": 0}
    
    # Prepare notification content
    title = f"New Assignment: {assignment.get('title', 'Untitled')}"
    body = f"{assignment.get('subject', 'Assignment')} - Due: {assignment.get('due_date', 'No deadline')}"
    url = f"/assignments/{assignment.get('assignment_id')}"
    tag = f"assignment-{assignment.get('assignment_id')}"
    
    results = {"sent": 0, "failed": 0, "expired": 0}
    expired_subscriptions = []
    
    for student in students:
        subscription = student.get('push_subscription')
        if not subscription:
            continue
            
        # Parse subscription if it's stored as string
        if isinstance(subscription, str):
            try:
                subscription = json.loads(subscription)
            except:
                continue
        
        result = send_push_notification(
            subscription_info=subscription,
            title=title,
            body=body,
            url=url,
            tag=tag
        )
        
        if result is True:
            results["sent"] += 1
        elif result is None:
            # Subscription expired
            results["expired"] += 1
            expired_subscriptions.append(student['student_id'])
        else:
            results["failed"] += 1
    
    # Clean up expired subscriptions
    if expired_subscriptions:
        students_col.update_many(
            {"student_id": {"$in": expired_subscriptions}},
            {"$unset": {"push_subscription": ""}}
        )
        logger.info(f"Removed {len(expired_subscriptions)} expired push subscriptions")
    
    logger.info(f"Assignment notification results: {results}")
    return results


def send_feedback_notification(db, student_id: str, assignment: dict, 
                                submission: dict) -> bool:
    """
    Send push notification to a student when their submission is marked.
    
    Args:
        db: Database instance
        student_id: Student's ID
        assignment: Assignment document
        submission: Submission document with marks
    
    Returns:
        True if sent successfully
    """
    students_col = db.db['students']
    student = students_col.find_one({"student_id": student_id})
    
    if not student or not student.get('push_subscription'):
        return False
    
    subscription = student['push_subscription']
    if isinstance(subscription, str):
        try:
            subscription = json.loads(subscription)
        except:
            return False
    
    score = submission.get('ai_marks', {}).get('total_score', '?')
    total = assignment.get('total_marks', 100)
    
    title = "Your work has been marked!"
    body = f"{assignment.get('title', 'Assignment')}: {score}/{total}"
    url = f"/submissions/{submission.get('submission_id')}"
    
    result = send_push_notification(
        subscription_info=subscription,
        title=title,
        body=body,
        url=url,
        tag=f"feedback-{submission.get('submission_id')}"
    )
    
    # Clean up if expired
    if result is None:
        students_col.update_one(
            {"student_id": student_id},
            {"$unset": {"push_subscription": ""}}
        )
    
    return result is True


def send_message_notification(db, student_id: str, teacher_name: str, 
                               preview: str = None) -> bool:
    """
    Send push notification to a student when they receive a message.
    
    Args:
        db: Database instance
        student_id: Student's ID
        teacher_name: Name of the teacher who sent the message
        preview: Optional message preview
    
    Returns:
        True if sent successfully
    """
    students_col = db.db['students']
    student = students_col.find_one({"student_id": student_id})
    
    if not student or not student.get('push_subscription'):
        return False
    
    subscription = student['push_subscription']
    if isinstance(subscription, str):
        try:
            subscription = json.loads(subscription)
        except:
            return False
    
    title = f"Message from {teacher_name}"
    body = preview[:100] + "..." if preview and len(preview) > 100 else (preview or "You have a new message")
    
    result = send_push_notification(
        subscription_info=subscription,
        title=title,
        body=body,
        url="/",
        tag=f"message-{teacher_name}"
    )
    
    if result is None:
        students_col.update_one(
            {"student_id": student_id},
            {"$unset": {"push_subscription": ""}}
        )
    
    return result is True
