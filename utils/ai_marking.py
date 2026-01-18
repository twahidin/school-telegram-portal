import os
import logging
import base64
import json
import re
from anthropic import Anthropic
from utils.auth import decrypt_api_key
from datetime import datetime

logger = logging.getLogger(__name__)

def get_teacher_ai_service(teacher):
    """Get AI service configured for a specific teacher"""
    api_key = None
    
    if teacher and teacher.get('anthropic_api_key'):
        api_key = decrypt_api_key(teacher['anthropic_api_key'])
    
    if not api_key:
        api_key = os.getenv('ANTHROPIC_API_KEY')
    
    if not api_key:
        logger.warning("No Anthropic API key available")
        return None
    
    try:
        return Anthropic(api_key=api_key)
    except Exception as e:
        logger.error(f"Error creating Anthropic client: {e}")
        return None

def analyze_submission_images(pages: list, assignment: dict, answer_key_content: bytes = None, teacher: dict = None) -> dict:
    """
    Analyze student submission images/PDF and generate feedback
    
    Args:
        pages: List of page dictionaries with 'type' and 'data' keys
        assignment: Assignment document with details
        answer_key_content: Optional bytes of answer key PDF
        teacher: Teacher document for API key
    
    Returns:
        Dictionary with structured feedback
    """
    client = get_teacher_ai_service(teacher)
    if not client:
        return {
            'error': 'AI service not available',
            'questions': [],
            'overall_feedback': 'AI feedback unavailable - no API key configured'
        }
    
    try:
        # Build content array with images
        content = []
        
        # System context
        system_prompt = f"""You are an experienced teacher marking student assignments.

Assignment: {assignment.get('title', 'Assignment')}
Subject: {assignment.get('subject', 'General')}
Total Marks: {assignment.get('total_marks', 100)}

Your task is to analyze the student's handwritten/typed submission and provide detailed feedback.

IMPORTANT RULES:
1. If handwriting is unclear or illegible, mark that question as "needs_review": true and leave feedback blank
2. If an answer appears empty or you cannot determine the content, mark as "needs_review": true
3. Be constructive and encouraging in feedback
4. Compare against the answer key if provided
5. Award partial marks where appropriate

Respond ONLY with valid JSON in this exact format:
{{
    "questions": [
        {{
            "question_num": 1,
            "student_answer": "transcribed answer or 'UNCLEAR' if illegible",
            "is_correct": true/false/null,
            "marks_awarded": number or null,
            "marks_total": number,
            "feedback": "specific feedback or empty string if needs review",
            "improvement": "what to improve or empty string",
            "needs_review": true/false
        }}
    ],
    "total_marks": number or null,
    "overall_feedback": "general feedback",
    "confidence": "high/medium/low",
    "review_notes": "notes for teacher about unclear sections"
}}"""

        # Add answer key if available
        if answer_key_content:
            content.append({
                "type": "text",
                "text": "ANSWER KEY (use for marking):"
            })
            
            # If PDF, encode as base64 for vision
            answer_key_b64 = base64.standard_b64encode(answer_key_content).decode('utf-8')
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": answer_key_b64
                }
            })
        
        content.append({
            "type": "text",
            "text": "\nSTUDENT SUBMISSION:"
        })
        
        # Add student submission pages
        for i, page in enumerate(pages):
            if page['type'] == 'image':
                # Image submission
                image_b64 = base64.standard_b64encode(page['data']).decode('utf-8')
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64
                    }
                })
                content.append({
                    "type": "text",
                    "text": f"(Page {i+1})"
                })
            elif page['type'] == 'pdf':
                # PDF submission
                pdf_b64 = base64.standard_b64encode(page['data']).decode('utf-8')
                content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64
                    }
                })
        
        content.append({
            "type": "text",
            "text": "\nAnalyze this submission and provide JSON feedback:"
        })
        
        # Make API call
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[
                {
                    "role": "user",
                    "content": content
                }
            ],
            system=system_prompt
        )
        
        response_text = message.content[0].text
        
        # Parse JSON response
        result = parse_ai_response(response_text)
        result['generated_at'] = datetime.utcnow().isoformat()
        result['raw_response'] = response_text
        
        return result
        
    except Exception as e:
        logger.error(f"Error analyzing submission: {e}")
        return {
            'error': str(e),
            'questions': [],
            'overall_feedback': f'Error generating feedback: {str(e)}'
        }

def parse_ai_response(response_text: str) -> dict:
    """Parse AI response into structured format"""
    try:
        # Try to extract JSON from response
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            return json.loads(json_match.group())
        return {'error': 'Could not parse response', 'raw': response_text}
    except json.JSONDecodeError:
        return {'error': 'Invalid JSON', 'raw': response_text}

def analyze_single_page(page_data: bytes, page_type: str, assignment: dict, teacher: dict = None) -> dict:
    """
    Analyze a single page for quick feedback (during upload)
    
    Returns quick feedback for initial review
    """
    client = get_teacher_ai_service(teacher)
    if not client:
        return {'error': 'AI not available'}
    
    try:
        content = []
        
        if page_type == 'image':
            image_b64 = base64.standard_b64encode(page_data).decode('utf-8')
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64
                }
            })
        else:
            pdf_b64 = base64.standard_b64encode(page_data).decode('utf-8')
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64
                }
            })
        
        content.append({
            "type": "text",
            "text": f"""Assignment: {assignment.get('title', 'Assignment')}
Subject: {assignment.get('subject')}

Quickly identify which questions are visible and note any obvious errors.
Format: Brief table of questions found with status (correct/incorrect/unclear)"""
        })
        
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": content}]
        )
        
        return {
            'preview_feedback': message.content[0].text,
            'generated_at': datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in quick analysis: {e}")
        return {'error': str(e)}

def mark_submission(submission: dict, assignment: dict, teacher: dict = None) -> dict:
    """
    Legacy function - Use AI to mark a text-based student submission
    """
    client = get_teacher_ai_service(teacher)
    if not client:
        return {
            'error': 'AI service not available',
            'questions': {},
            'overall': 'Unable to generate AI feedback - no API key configured'
        }
    
    try:
        questions_text = ""
        for i, q in enumerate(assignment.get('questions', []), 1):
            answer = submission.get('answers', {}).get(str(i), submission.get('answers', {}).get(f'q{i}', 'No answer provided'))
            questions_text += f"""
Question {i}: {q.get('question', q.get('text', ''))}
Marks: {q.get('marks', 0)}
{"Model Answer: " + q.get('model_answer', '') if q.get('model_answer') else ""}
Student Answer: {answer}
---
"""
        
        prompt = f"""You are an experienced teacher marking a student assignment. 
Please evaluate the following submission and provide constructive feedback.

Assignment: {assignment.get('title', 'Untitled')}
Subject: {assignment.get('subject', 'General')}
Total Marks: {assignment.get('total_marks', 0)}

{questions_text}

For each question, provide:
1. A score out of the available marks
2. What the student did well
3. Areas for improvement
4. Specific suggestions for better answers

Then provide an overall summary.

Format your response as structured feedback."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        feedback_text = message.content[0].text
        
        return {
            'raw_feedback': feedback_text,
            'questions': {},
            'overall': feedback_text,
            'generated_at': datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error generating AI feedback: {e}")
        return {
            'error': str(e),
            'questions': {},
            'overall': f'Error generating feedback: {str(e)}'
        }

def get_quick_feedback(answer: str, question: str, model_answer: str = None, teacher: dict = None) -> str:
    """Get quick feedback on a single text answer"""
    client = get_teacher_ai_service(teacher)
    if not client:
        return "AI feedback not available"
    
    try:
        prompt = f"""Provide brief, constructive feedback (2-3 sentences) on this student answer.

Question: {question}
{"Model Answer: " + model_answer if model_answer else ""}
Student Answer: {answer}

Give specific, helpful feedback focusing on what's good and what could be improved."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return message.content[0].text
        
    except Exception as e:
        logger.error(f"Error getting quick feedback: {e}")
        return f"Unable to generate feedback: {str(e)}"

def generate_feedback_summary(submission: dict, assignment: dict, ai_feedback: dict, teacher_edits: dict = None) -> dict:
    """
    Generate a final feedback summary combining AI and teacher feedback
    
    Returns structured data for PDF generation
    """
    questions = []
    total_marks = 0
    total_possible = assignment.get('total_marks', 100)
    
    ai_questions = ai_feedback.get('questions', [])
    
    for i, q in enumerate(ai_questions):
        question_data = {
            'question_num': q.get('question_num', i + 1),
            'student_answer': q.get('student_answer', ''),
            'correct_answer': '',  # From answer key if available
            'is_correct': q.get('is_correct'),
            'marks_awarded': q.get('marks_awarded', 0),
            'marks_total': q.get('marks_total', 0),
            'feedback': q.get('feedback', ''),
            'improvement': q.get('improvement', ''),
            'needs_review': q.get('needs_review', False)
        }
        
        # Apply teacher edits if available
        if teacher_edits and str(i) in teacher_edits:
            edit = teacher_edits[str(i)]
            if 'marks_awarded' in edit:
                question_data['marks_awarded'] = edit['marks_awarded']
            if 'feedback' in edit:
                question_data['feedback'] = edit['feedback']
            if 'improvement' in edit:
                question_data['improvement'] = edit['improvement']
            question_data['needs_review'] = False  # Teacher has reviewed
        
        if question_data['marks_awarded'] is not None:
            total_marks += question_data['marks_awarded']
        
        questions.append(question_data)
    
    return {
        'questions': questions,
        'total_marks': total_marks,
        'total_possible': total_possible,
        'percentage': round((total_marks / total_possible * 100), 1) if total_possible > 0 else 0,
        'overall_feedback': teacher_edits.get('overall_feedback') if teacher_edits else ai_feedback.get('overall_feedback', ''),
        'generated_at': datetime.utcnow().isoformat()
    }
