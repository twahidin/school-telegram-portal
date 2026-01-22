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
        assignment: Assignment document with details (including extracted text fields)
        answer_key_content: Optional bytes of answer key PDF (fallback if no extracted text)
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
        
        # Build additional context from reference materials and rubrics
        additional_context = ""
        
        # Add reference materials text if available (for literature, history, etc.)
        reference_materials_text = assignment.get('reference_materials_text', '')
        if reference_materials_text:
            additional_context += f"""

REFERENCE MATERIALS (use this content to evaluate student answers):
{reference_materials_text}
"""
        
        # Add rubrics text if available (for essays, subjective answers)
        rubrics_text = assignment.get('rubrics_text', '')
        if rubrics_text:
            additional_context += f"""

GRADING RUBRICS (use these criteria to evaluate and score answers):
{rubrics_text}
"""
        
        # Add teacher's custom instructions
        feedback_instructions = assignment.get('feedback_instructions', '')
        grading_instructions = assignment.get('grading_instructions', '')
        custom_instructions = ""
        if feedback_instructions:
            custom_instructions += f"\n\nFEEDBACK STYLE INSTRUCTIONS: {feedback_instructions}"
        if grading_instructions:
            custom_instructions += f"\n\nGRADING INSTRUCTIONS: {grading_instructions}"
        
        # System context
        system_prompt = f"""You are an experienced teacher marking student assignments.

Assignment: {assignment.get('title', 'Assignment')}
Subject: {assignment.get('subject', 'General')}
Total Marks: {assignment.get('total_marks', 100)}
{additional_context}
Your task is to analyze the student's handwritten/typed submission and provide detailed feedback.
{custom_instructions}

IMPORTANT RULES:
1. If handwriting is unclear or illegible, mark that question as "needs_review": true and leave feedback blank
2. If an answer appears empty or you cannot determine the content, mark as "needs_review": true
3. Be constructive and encouraging in feedback
4. Compare against the answer key if provided
5. Award partial marks where appropriate
6. If rubrics are provided, use them to evaluate subjective answers (essays, literature, etc.)
7. If reference materials are provided, use them to verify factual accuracy and context
8. ALWAYS extract and include the correct answer from the answer key for each question

Respond ONLY with valid JSON in this exact format:
{{
    "questions": [
        {{
            "question_num": 1,
            "student_answer": "transcribed answer or 'UNCLEAR' if illegible",
            "correct_answer": "the correct answer from the answer key (use 'Refer to answer key for diagram' if it contains diagrams/images)",
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

        # Add answer key - ALWAYS use PDF vision for accuracy (critical for marking)
        # Extracted text is stored but not used here to ensure we don't miss 
        # formulas, diagrams, tables, or complex layouts in the answer key
        if answer_key_content:
            content.append({
                "type": "text",
                "text": "ANSWER KEY (use for marking):"
            })
            
            # Always use PDF vision for answer key - accuracy over cost savings
            answer_key_b64 = base64.standard_b64encode(answer_key_content).decode('utf-8')
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": answer_key_b64
                }
            })
            logger.info("Using PDF vision for answer key (prioritizing accuracy for marking)")
        
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

def get_preview_feedback(pages: list, assignment: dict, feedback_type: str = 'overall', teacher: dict = None) -> dict:
    """
    Get preview feedback for student work without final submission.
    Adjusts scaffolding based on how much work is completed.
    
    Args:
        pages: List of page dictionaries with 'type' and 'data' keys
        assignment: Assignment document with details
        feedback_type: 'overall', 'hints', or 'check'
        teacher: Teacher document for API key
    
    Returns:
        Dictionary with feedback based on type requested
    """
    client = get_teacher_ai_service(teacher)
    if not client:
        return {
            'error': 'AI service not available',
            'feedback': 'AI feedback unavailable - no API key configured'
        }
    
    try:
        content = []
        
        # Different prompts based on feedback type
        if feedback_type == 'overall':
            system_prompt = f"""You are a helpful teaching assistant reviewing a student's work before final submission.

Assignment: {assignment.get('title', 'Assignment')}
Subject: {assignment.get('subject', 'General')}
Total Marks: {assignment.get('total_marks', 100)}

TASK: Provide overall feedback and areas to improve.

IMPORTANT RULES FOR BLANK/INCOMPLETE SUBMISSIONS:
- If the submission appears mostly BLANK (many unanswered questions), DO NOT provide detailed hints or scaffolding.
- For blank submissions, simply note which questions need to be attempted and encourage the student to try first.
- Only provide specific improvement areas for questions that have actual attempts.

Respond with JSON:
{{
    "overall": "2-3 sentence overall assessment of their work so far",
    "areas_to_improve": ["specific area 1", "specific area 2", "specific area 3"],
    "warning": "optional warning if submission is mostly blank or incomplete"
}}"""

        elif feedback_type == 'hints':
            system_prompt = f"""You are a helpful teaching assistant providing hints for stuck students.

Assignment: {assignment.get('title', 'Assignment')}
Subject: {assignment.get('subject', 'General')}
Total Marks: {assignment.get('total_marks', 100)}

TASK: Provide starting hints for questions where the student seems stuck.

CRITICAL RULES - READ CAREFULLY:
1. If a question is LEFT BLANK or has no attempt, DO NOT give hints for it. The student must attempt it first.
2. Only provide hints for questions where the student has STARTED but seems stuck or confused.
3. For blank questions, respond: "Please attempt this question first before requesting hints."
4. Hints should guide thinking, NOT give away answers.
5. If most questions are blank, provide only general study advice, not specific hints.

Respond with JSON:
{{
    "hints": ["hint for Q1 if attempted", "hint for Q2 if attempted"],
    "feedback": "general message about their progress",
    "warning": "message if too many questions are blank - encourage attempting first"
}}"""

        elif feedback_type == 'check':
            system_prompt = f"""You are a teaching assistant checking student answers.

Assignment: {assignment.get('title', 'Assignment')}
Subject: {assignment.get('subject', 'General')}
Total Marks: {assignment.get('total_marks', 100)}

TASK: Check the student's answers and indicate which are on track.

RULES:
1. For BLANK answers, mark as "Not attempted - please answer first"
2. For attempted answers, indicate: "On track", "Partially correct", or "Needs revision"
3. Do NOT provide correct answers - just indicate if they're on the right track
4. If most answers are blank, note this and encourage the student to attempt more questions.

Respond with JSON:
{{
    "check_result": "Summary of how many questions are on track vs need work",
    "questions_status": "Q1: On track | Q2: Not attempted | Q3: Needs revision | ...",
    "warning": "message if submission is mostly blank"
}}"""
        
        else:
            return {'error': 'Invalid feedback type', 'feedback': 'Please select a valid feedback type.'}
        
        content.append({
            "type": "text",
            "text": "STUDENT'S WORK:"
        })
        
        # Add student submission pages
        for i, page in enumerate(pages):
            if page['type'] == 'image':
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
            "text": "\nProvide your feedback as JSON:"
        })
        
        # Make API call
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
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
        return result
        
    except Exception as e:
        logger.error(f"Error getting preview feedback: {e}")
        return {
            'error': str(e),
            'feedback': f'Error generating feedback: {str(e)}'
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

def get_default_prompts():
    """Get default AI help prompts"""
    return {
        'stuck': {
            'name': 'Help me! I am stuck',
            'description': 'Helps students who don\'t know how to start a question',
            'system_prompt': """You are a patient and encouraging tutor helping a student who is stuck on a question.

Subject: {subject}
Assignment: {assignment_title}

The student is STUCK and doesn't know how to begin. Your job is to help them get started WITHOUT giving away the answer.

RULES:
1. DO NOT give the answer directly
2. Provide a step-by-step approach to think through the problem
3. Ask guiding questions that lead them toward the solution
4. Remind them of relevant concepts or formulas they might need
5. Be encouraging and supportive
6. Keep your response concise but helpful (3-5 bullet points max)

Respond with JSON:
{{
    "response": "Your main encouraging message with initial guidance",
    "hints": ["First step to consider", "Think about this concept", "Remember this formula/rule"]
}}""",
            'user_prompt': """The student is stuck on this question and doesn't know how to start:

QUESTION: {question}

Please provide gentle hints to help them get started without giving away the answer.""",
            'requires_answer': False
        },
        'wrong': {
            'name': 'Where did I go wrong?',
            'description': 'Identifies mistakes in student answers',
            'system_prompt': """You are a helpful tutor identifying where a student went wrong in their answer.

Subject: {subject}
Assignment: {assignment_title}

The student has attempted an answer but thinks it might be wrong. Help them understand their mistake.

RULES:
1. Identify the specific error or misconception in their answer
2. DO NOT provide the correct answer directly
3. Explain WHY their approach or answer is incorrect
4. Point them in the right direction to fix their mistake
5. Be constructive, not critical
6. Keep your response focused and clear

Respond with JSON:
{{
    "response": "Clear explanation of where they went wrong and why",
    "hints": ["What to reconsider", "Common mistake to avoid", "Concept to review"]
}}""",
            'user_prompt': """The student wants to know where they went wrong:

QUESTION: {question}

STUDENT'S ANSWER: {student_answer}

Please identify their mistake and guide them toward the correct approach without giving the answer directly.""",
            'requires_answer': True
        },
        'improve': {
            'name': 'How to improve my answer',
            'description': 'Suggests improvements to student answers',
            'system_prompt': """You are a tutor helping a student improve their answer.

Subject: {subject}
Assignment: {assignment_title}

The student has an answer but wants to make it better. Help them enhance their response.

RULES:
1. Acknowledge what's good about their current answer
2. Suggest specific improvements they can make
3. Mention any missing elements or concepts
4. Suggest ways to make the answer more complete or precise
5. DO NOT rewrite the answer for them
6. Keep suggestions actionable and specific

Respond with JSON:
{{
    "response": "What's good about their answer and how they can improve it",
    "hints": ["Add this element", "Clarify this part", "Consider including..."]
}}""",
            'user_prompt': """The student wants to improve their answer:

QUESTION: {question}

STUDENT'S ANSWER: {student_answer}

Please suggest specific ways they can enhance their answer.""",
            'requires_answer': True
        },
        'explain': {
            'name': 'Explain this concept to me',
            'description': 'Explains the underlying concept or theory',
            'system_prompt': """You are a knowledgeable tutor explaining concepts to students.

Subject: {subject}
Assignment: {assignment_title}

The student needs help understanding the concept behind a question. Explain it clearly.

RULES:
1. Explain the underlying concept or theory in simple terms
2. Use analogies or real-world examples when helpful
3. Break down complex ideas into digestible parts
4. DO NOT solve the actual question for them
5. Focus on building understanding, not just memorization
6. Keep explanations clear and age-appropriate

Respond with JSON:
{{
    "response": "Clear explanation of the concept with examples",
    "hints": ["Key point to remember", "Related concept", "How this applies to the question"]
}}""",
            'user_prompt': """The student needs help understanding the concept behind this question:

QUESTION: {question}

{answer_context}

Please explain the underlying concept clearly without solving the question directly.""",
            'requires_answer': False
        },
        'breakdown': {
            'name': 'Break down the question',
            'description': 'Helps understand what the question is asking',
            'system_prompt': """You are a tutor helping students understand what questions are asking.

Subject: {subject}
Assignment: {assignment_title}

The student is confused about what the question is asking. Break it down for them.

RULES:
1. Identify the key components of the question
2. Explain what each part is asking for
3. Highlight important keywords or phrases
4. Clarify any technical terms used
5. DO NOT provide the answer
6. Help them understand the structure and requirements

Respond with JSON:
{{
    "response": "Clear breakdown of what the question is asking",
    "hints": ["Key word/phrase to note", "This part asks for...", "The question wants you to..."]
}}""",
            'user_prompt': """The student needs help understanding what this question is asking:

QUESTION: {question}

Please break down the question into understandable parts without providing the answer.""",
            'requires_answer': False
        },
        'formula': {
            'name': 'What formula/method should I use?',
            'description': 'Guides students on which approach to take',
            'system_prompt': """You are a tutor helping students identify the right approach to solve problems.

Subject: {subject}
Assignment: {assignment_title}

The student needs guidance on which formula, method, or approach to use.

RULES:
1. Identify the relevant formula(s) or method(s) that apply
2. Explain WHEN and WHY this formula/method is used
3. Remind them of the formula structure (but don't plug in values)
4. DO NOT solve the problem for them
5. Help them recognize patterns that indicate which method to use
6. Keep it focused on the approach, not the solution

Respond with JSON:
{{
    "response": "Explanation of which formula/method to use and why",
    "hints": ["Formula: ...", "When to use this: ...", "Steps to apply: ..."]
}}""",
            'user_prompt': """The student needs to know which formula or method to use for this question:

QUESTION: {question}

{answer_context}

Please guide them on the appropriate formula or method without solving the question.""",
            'requires_answer': False
        },
        'example': {
            'name': 'Show me a similar example',
            'description': 'Provides a worked example of a similar problem',
            'system_prompt': """You are a tutor providing worked examples to help students learn.

Subject: {subject}
Assignment: {assignment_title}

The student wants to see a similar example worked out to understand the approach.

RULES:
1. Create a DIFFERENT but SIMILAR question (not the same one)
2. Show the complete working/solution for your example
3. Explain each step clearly
4. Make sure the example teaches the same concept/skill
5. DO NOT solve the student's actual question
6. The example should be slightly simpler if possible

Respond with JSON:
{{
    "response": "Here's a similar example with full working",
    "hints": ["Step 1: ...", "Step 2: ...", "Notice how we..."]
}}""",
            'user_prompt': """The student wants to see a similar worked example for this type of question:

QUESTION: {question}

Please create and solve a SIMILAR (but different) example question that demonstrates the same concept or method. Do NOT solve the student's actual question.""",
            'requires_answer': False
        }
    }

def get_ai_prompts(db_instance=None):
    """Get AI prompts from database or return defaults"""
    defaults = get_default_prompts()
    
    if db_instance is None:
        return defaults
    
    try:
        # Try to get prompts from database
        stored_prompts = db_instance.db.ai_prompts.find_one({'_id': 'help_prompts'})
        if stored_prompts and stored_prompts.get('prompts'):
            # Merge with defaults (stored prompts override defaults)
            for key, value in stored_prompts['prompts'].items():
                if key in defaults:
                    defaults[key].update(value)
            return defaults
    except Exception as e:
        logger.warning(f"Could not load prompts from database: {e}")
    
    return defaults

def save_ai_prompts(db_instance, prompts: dict) -> bool:
    """Save AI prompts to database"""
    try:
        db_instance.db.ai_prompts.update_one(
            {'_id': 'help_prompts'},
            {'$set': {
                'prompts': prompts,
                'updated_at': datetime.utcnow()
            }},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving prompts: {e}")
        return False

def get_question_help(question: str, student_answer: str, help_type: str, assignment: dict, teacher: dict = None, db_instance=None, question_image: str = None, answer_image: str = None) -> dict:
    """
    Get AI help for a specific question.
    
    Args:
        question: The question text the student needs help with
        student_answer: The student's answer attempt (can be empty for some types)
        help_type: 'stuck', 'wrong', 'improve', 'explain', 'breakdown', 'formula', 'example'
        assignment: Assignment document for context
        teacher: Teacher document for API key
        db_instance: Database instance for loading custom prompts
        question_image: Base64 encoded image of the question (optional)
        answer_image: Base64 encoded image of the student's answer (optional)
    
    Returns:
        Dictionary with help response
    """
    client = get_teacher_ai_service(teacher)
    if not client:
        return {
            'error': 'AI service not available',
            'response': 'AI help unavailable - no API key configured'
        }
    
    try:
        # Get prompts (from database or defaults)
        prompts = get_ai_prompts(db_instance)
        
        if help_type not in prompts:
            return {'error': 'Invalid help type', 'response': 'Please select a valid help option.'}
        
        prompt_config = prompts[help_type]
        
        # Check if answer is required (consider both text and image)
        has_answer = student_answer or answer_image
        if prompt_config.get('requires_answer') and not has_answer:
            return {
                'response': f'Please provide your answer so I can help with "{prompt_config["name"]}".',
                'hints': ['Enter your answer attempt in the answer field or upload a photo', 'Even partial answers help me give better feedback']
            }
        
        subject = assignment.get('subject', 'General')
        assignment_title = assignment.get('title', 'Assignment')
        
        # Format the prompts with variables
        answer_context = f"STUDENT'S ANSWER: {student_answer}" if student_answer else "No text answer provided."
        if answer_image:
            answer_context += " (Student also provided an image of their answer - see attached)"
        
        system_prompt = prompt_config['system_prompt'].format(
            subject=subject,
            assignment_title=assignment_title
        )
        
        user_text = prompt_config['user_prompt'].format(
            question=question if question else "(See question image attached)",
            student_answer=student_answer or 'Not provided in text',
            answer_context=answer_context
        )
        
        # Build message content with images if provided
        content_parts = []
        
        # Add question image if provided
        if question_image:
            # Extract base64 data from data URL
            if ',' in question_image:
                image_data = question_image.split(',')[1]
                media_type = question_image.split(';')[0].split(':')[1] if ':' in question_image else 'image/jpeg'
            else:
                image_data = question_image
                media_type = 'image/jpeg'
            
            content_parts.append({
                "type": "text",
                "text": "QUESTION IMAGE:"
            })
            content_parts.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data
                }
            })
        
        # Add answer image if provided
        if answer_image:
            if ',' in answer_image:
                image_data = answer_image.split(',')[1]
                media_type = answer_image.split(';')[0].split(':')[1] if ':' in answer_image else 'image/jpeg'
            else:
                image_data = answer_image
                media_type = 'image/jpeg'
            
            content_parts.append({
                "type": "text",
                "text": "STUDENT'S ANSWER IMAGE:"
            })
            content_parts.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data
                }
            })
        
        # Add the main text prompt
        content_parts.append({
            "type": "text",
            "text": user_text
        })
        
        # Make API call with vision support
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": content_parts}],
            system=system_prompt
        )
        
        response_text = message.content[0].text
        
        # Parse JSON response
        result = parse_ai_response(response_text)
        
        # If parsing failed, return raw response
        if 'error' in result and 'raw' in result:
            return {
                'response': result['raw'],
                'hints': []
            }
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting question help: {e}")
        return {
            'error': str(e),
            'response': f'Error generating help: {str(e)}'
        }

def extract_answers_from_key(file_content: bytes, file_type: str, question_count: int, teacher: dict = None) -> dict:
    """
    Extract answers from an uploaded answer key file (PDF or image).
    
    Args:
        file_content: The file content as bytes
        file_type: 'pdf' or 'image'
        question_count: Number of questions to extract answers for
        teacher: Teacher document for API key
    
    Returns:
        Dictionary with extracted answers for each question
    """
    client = get_teacher_ai_service(teacher)
    if not client:
        return {
            'error': 'AI service not available',
            'answers': {}
        }
    
    try:
        content = []
        
        # Add the answer key file
        file_b64 = base64.standard_b64encode(file_content).decode('utf-8')
        
        if file_type == 'pdf':
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": file_b64
                }
            })
        else:
            # Image file
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": file_b64
                }
            })
        
        content.append({
            "type": "text",
            "text": f"""This is an answer key for an assignment with {question_count} questions.

Please extract the correct answer for each question from this answer key.

IMPORTANT RULES:
1. Extract EXACTLY {question_count} answers (questions 1 through {question_count})
2. For text answers, provide the complete answer text
3. For mathematical answers, include formulas, working steps, and final answers
4. For diagrams/images that cannot be expressed as text, use "Refer to answer key for diagram"
5. If a question's answer is not found in the document, use "Answer not found in document"
6. Preserve any special formatting, formulas, or symbols where possible

Respond ONLY with valid JSON in this exact format:
{{
    "answers": {{
        "1": "the complete answer for question 1",
        "2": "the complete answer for question 2",
        ... continue for all {question_count} questions
    }},
    "notes": "any notes about answers that couldn't be fully extracted"
}}"""
        })
        
        # Make API call
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": content}]
        )
        
        response_text = message.content[0].text
        
        # Parse JSON response
        result = parse_ai_response(response_text)
        return result
        
    except Exception as e:
        logger.error(f"Error extracting answers from key: {e}")
        return {
            'error': str(e),
            'answers': {}
        }


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
