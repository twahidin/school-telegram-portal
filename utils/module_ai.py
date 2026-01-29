"""
AI-powered module generation and learning assessment utilities.
Uses Anthropic Claude API for:
1. Generating module structure from syllabus documents
2. Assessing student mastery through chat
3. Building student learning profiles
"""

import os
import logging
import base64
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def get_claude_client():
    """Get Anthropic client"""
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def generate_modules_from_syllabus(
    file_content: bytes,
    file_type: str,
    subject: str,
    year_level: str,
    teacher_id: str,
) -> Dict[str, Any]:
    """
    Generate hierarchical module structure from uploaded syllabus/scheme of work.

    Args:
        file_content: PDF or Word document bytes
        file_type: 'pdf' or 'docx'
        subject: Subject name
        year_level: e.g., "Secondary 3"
        teacher_id: Owner teacher ID

    Returns:
        Dictionary with module tree structure
    """
    client = get_claude_client()
    if not client:
        return {'error': 'AI service not available'}

    try:
        content = []

        if file_type == 'pdf':
            file_b64 = base64.standard_b64encode(file_content).decode('utf-8')
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": file_b64,
                },
            })
        else:
            content.append({
                "type": "text",
                "text": f"[Document content for {subject} - upload PDF for full analysis]",
            })

        system_prompt = f"""You are an expert curriculum designer. Analyze this syllabus/scheme of work and create a hierarchical module structure for {subject} ({year_level}).

STRUCTURE RULES:
1. The ROOT module represents the entire year/course
2. First level children are major topics/units (e.g., "Algebra", "Geometry")
3. Second level are sub-topics (e.g., "Linear Equations", "Quadratic Equations")
4. Third level (leaves) are specific learning objectives that can be assessed
5. Maximum depth: 4 levels (root + 3 levels)
6. Each leaf module should be learnable in 1-2 hours
7. Include estimated hours for each module
8. Generate learning objectives for each module

VISUALIZATION:
- Assign colors that group related topics (hex codes like #667eea)
- Use "icon" field with Bootstrap icon names like "bi-calculator", "bi-book"

Respond ONLY with valid JSON in this exact format (no markdown code fence):
{{
    "root": {{
        "title": "Mathematics Year 3",
        "description": "Complete mathematics curriculum for Secondary 3",
        "estimated_hours": 150,
        "color": "#667eea",
        "icon": "bi-diagram-3",
        "children": [
            {{
                "title": "Algebra",
                "description": "...",
                "estimated_hours": 40,
                "color": "#764ba2",
                "icon": "bi-calculator",
                "learning_objectives": ["Understand algebraic expressions", "..."],
                "children": [
                    {{
                        "title": "Linear Equations",
                        "description": "...",
                        "estimated_hours": 10,
                        "color": "#8b5cf6",
                        "icon": "bi-graph-up",
                        "learning_objectives": ["..."],
                        "children": [
                            {{
                                "title": "Solving One-Variable Equations",
                                "description": "...",
                                "estimated_hours": 2,
                                "color": "#a78bfa",
                                "icon": "bi-book",
                                "learning_objectives": ["..."],
                                "is_leaf": true
                            }}
                        ]
                    }}
                ]
            }}
        ]
    }},
    "total_modules": 25,
    "total_hours": 150
}}"""

        content.append({
            "type": "text",
            "text": f"""
Subject: {subject}
Year Level: {year_level}

Analyze this document and create a comprehensive module hierarchy.
Ensure all topics from the syllabus are covered.
Respond with JSON only:""",
        })

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        response_text = message.content[0].text
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            return json.loads(json_match.group())
        return {'error': 'Could not parse module structure'}

    except Exception as e:
        logger.error("Error generating modules: %s", e)
        return {'error': str(e)}


def assess_student_understanding(
    student_message: str,
    module: Dict,
    chat_history: List[Dict],
    student_profile: Optional[Dict] = None,
    writing_image: Optional[bytes] = None,
) -> Dict[str, Any]:
    """
    AI learning agent that assesses student understanding and provides teaching.

    Args:
        student_message: Student's chat message or question
        module: Current module being studied
        chat_history: Previous messages in this session
        student_profile: Student's learning profile (strengths/weaknesses)
        writing_image: Optional image of student's handwritten work

    Returns:
        Dictionary with response, assessment, and profile updates
    """
    client = get_claude_client()
    if not client:
        return {'error': 'AI service not available'}

    try:
        profile_context = ""
        if student_profile:
            strengths = ", ".join([s.get('topic', '') for s in student_profile.get('strengths', [])])
            weaknesses = ", ".join([w.get('topic', '') for w in student_profile.get('weaknesses', [])])
            profile_context = f"""
STUDENT PROFILE:
- Strengths: {strengths or 'Not yet identified'}
- Areas needing work: {weaknesses or 'Not yet identified'}
- Learning style: {student_profile.get('learning_style', 'Unknown')}
- Common mistakes: {', '.join([m.get('pattern', '') for m in student_profile.get('common_mistakes', [])])}
"""

        system_prompt = f"""You are an expert, patient tutor helping a student learn.

CURRENT MODULE: {module.get('title', 'Unknown')}
LEARNING OBJECTIVES: {', '.join(module.get('learning_objectives', []))}
{profile_context}

YOUR ROLE:
1. TEACH: Explain concepts clearly, use examples, adapt to student's level
2. ASSESS: Ask questions to check understanding, identify misconceptions
3. ENCOURAGE: Be supportive, celebrate progress, build confidence
4. ADAPT: Use the student's learning profile to personalize teaching

ASSESSMENT GUIDELINES:
- After teaching a concept, ask a question to assess understanding
- If student answers correctly: Award mastery points (mastery_change 1-10), move to next concept
- If student struggles: Provide hints, break down the problem, try different explanations
- Note any patterns in mistakes for profile updates

RESPONSE FORMAT - Respond with valid JSON only (no markdown):
{{
    "response": "Your teaching response to the student (use markdown for formatting, include examples)",
    "response_type": "teaching",
    "assessment": {{
        "question_asked": "The assessment question if any",
        "student_answer_correct": true,
        "mastery_change": 5,
        "concept_assessed": "Specific concept tested"
    }},
    "profile_updates": {{
        "new_strength": null,
        "new_weakness": null,
        "new_mistake_pattern": null
    }},
    "next_action": "continue_teaching",
    "interactive_element": null
}}

Use "response_type" one of: teaching, assessment, feedback, encouragement.
Use "mastery_change" between -10 and 10. Use "next_action" one of: continue_teaching, assess_understanding, review_previous, module_complete."""

        messages_content = []

        if chat_history:
            history_text = "\n".join([
                f"{'Student' if m.get('role') == 'student' else 'Tutor'}: {m.get('content', '')}"
                for m in chat_history[-10:]
            ])
            messages_content.append({
                "type": "text",
                "text": f"RECENT CONVERSATION:\n{history_text}\n\n",
            })

        if writing_image:
            image_b64 = base64.standard_b64encode(writing_image).decode('utf-8')
            messages_content.append({"type": "text", "text": "STUDENT'S HANDWRITTEN WORK:"})
            messages_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64,
                },
            })

        messages_content.append({
            "type": "text",
            "text": f"STUDENT'S MESSAGE: {student_message}\n\nRespond with JSON only:",
        })

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": messages_content}],
        )

        response_text = message.content[0].text
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            result = json.loads(json_match.group())
            result['raw_response'] = response_text
            return result

        return {
            'response': response_text,
            'response_type': 'teaching',
            'assessment': None,
            'profile_updates': None,
        }

    except Exception as e:
        logger.error("Error in learning assessment: %s", e)
        return {
            'error': str(e),
            'response': "I'm having trouble right now. Let's try again!",
        }


def generate_interactive_assessment(
    module: Dict,
    difficulty: str = "medium",
    question_type: str = "mixed",
) -> Dict[str, Any]:
    """
    Generate an interactive assessment for a module.

    Args:
        module: Module to assess
        difficulty: easy/medium/hard
        question_type: mcq/short_answer/problem/mixed

    Returns:
        Assessment questions and answers
    """
    client = get_claude_client()
    if not client:
        return {'error': 'AI service not available'}

    try:
        system_prompt = f"""Generate an interactive assessment for this learning module.

MODULE: {module.get('title')}
OBJECTIVES: {', '.join(module.get('learning_objectives', []))}
DIFFICULTY: {difficulty}
QUESTION TYPE: {question_type}

Create 5 questions that test understanding of the learning objectives.

Respond with valid JSON only:
{{
    "questions": [
        {{
            "id": 1,
            "type": "mcq",
            "question": "...",
            "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
            "correct_answer": "B",
            "explanation": "Why B is correct...",
            "hints": ["Hint 1", "Hint 2"],
            "points": 10
        }}
    ],
    "total_points": 50,
    "passing_score": 35,
    "time_limit_minutes": 15
}}"""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": "Generate the assessment now."}],
        )

        response_text = message.content[0].text
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            return json.loads(json_match.group())
        return {'error': 'Could not generate assessment'}

    except Exception as e:
        logger.error("Error generating assessment: %s", e)
        return {'error': str(e)}


def analyze_writing_submission(
    image_data: bytes,
    module: Dict,
    expected_content: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyze student's handwritten work (equations, diagrams, workings).

    Args:
        image_data: Image bytes of handwritten work
        module: Current module context
        expected_content: What the student was asked to show

    Returns:
        Analysis of the work
    """
    client = get_claude_client()
    if not client:
        return {'error': 'AI service not available'}

    try:
        image_b64 = base64.standard_b64encode(image_data).decode('utf-8')

        system_prompt = f"""Analyze this student's handwritten work for the module: {module.get('title')}

{('Expected content: ' + expected_content) if expected_content else ''}

Evaluate:
1. Mathematical/logical correctness
2. Clarity of presentation
3. Method and approach used
4. Any errors or misconceptions

Respond with valid JSON only:
{{
    "transcription": "Text version of what's written",
    "analysis": "Detailed analysis of the work",
    "is_correct": true,
    "errors": [],
    "suggestions": [],
    "mastery_indication": 75
}}

Use is_correct: true, false, or "partial". mastery_indication is 0-100."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": "Analyze this handwritten work and respond with JSON only:"},
                    ],
                }
            ],
        )

        response_text = message.content[0].text
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            return json.loads(json_match.group())
        return {'analysis': response_text}

    except Exception as e:
        logger.error("Error analyzing writing: %s", e)
        return {'error': str(e)}
