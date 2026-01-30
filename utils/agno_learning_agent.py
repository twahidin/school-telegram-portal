"""
Agno-based Learning Agent for Student Mastery Assessment

The agent can pull resources and generate interactive quizzes during the conversation
by calling tools: get_module_resources, generate_interactive_quiz, update_student_mastery, etc.
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# Optional Agno import - fallback if not installed
try:
    from agno.agent import Agent
    from agno.models.anthropic import Claude
    AGNO_AVAILABLE = True
except ImportError:
    AGNO_AVAILABLE = False
    Agent = None
    Claude = None


# ============================================================================
# TOOLS - The agent calls these during the conversation
# ============================================================================

def get_module_resources(module_id: str) -> List[Dict[str, Any]]:
    """
    Get all learning resources for a module (videos, PDFs, links).
    Call this when the student asks for videos, extra materials, or resources to practice with.
    """
    from models import ModuleResource
    resources = list(ModuleResource.find({'module_id': module_id}).sort('order', 1))
    return [
        {
            'resource_id': r.get('resource_id'),
            'type': r.get('type'),
            'title': r.get('title'),
            'description': r.get('description', ''),
            'url': r.get('url', ''),
            'duration_minutes': r.get('duration_minutes'),
        }
        for r in resources
    ]


def generate_interactive_quiz(
    module_id: str,
    difficulty: str = "medium",
    question_type: str = "mixed",
) -> Dict[str, Any]:
    """
    Generate an interactive quiz for the current module.
    Call this when you want to assess the student with multiple-choice or short-answer questions.
    Args: module_id, difficulty (easy/medium/hard), question_type (mcq/short_answer/problem/mixed).
    """
    from models import Module
    from utils.module_ai import generate_interactive_assessment
    module = Module.find_one({'module_id': module_id})
    if not module:
        return {'error': 'Module not found'}
    result = generate_interactive_assessment(
        module, difficulty=difficulty, question_type=question_type
    )
    if 'error' in result:
        return result
    return result


def update_student_mastery(
    student_id: str,
    module_id: str,
    mastery_change: float,
    concept: str,
) -> Dict[str, Any]:
    """
    Update the student's mastery score for this module.
    Call with positive change (1-10) when they answer correctly, negative (-1 to -5) for mistakes.
    """
    from models import StudentModuleMastery
    current = StudentModuleMastery.find_one({'student_id': student_id, 'module_id': module_id})
    current_score = current.get('mastery_score', 0) if current else 0
    new_score = max(0, min(100, round(current_score + mastery_change)))
    status = 'mastered' if new_score >= 100 else ('in_progress' if new_score > 0 else 'not_started')
    StudentModuleMastery.update_one(
        {'student_id': student_id, 'module_id': module_id},
        {
            '$set': {
                'mastery_score': new_score,
                'status': status,
                'updated_at': datetime.utcnow(),
                'last_activity': datetime.utcnow(),
            },
            '$inc': {'time_spent_minutes': 1, 'assessments_completed': 1},
        },
        upsert=True,
    )
    return {
        'previous_score': current_score,
        'new_score': new_score,
        'change': mastery_change,
        'status': status,
        'concept_assessed': concept,
    }


def record_student_strength(
    student_id: str,
    subject: str,
    topic: str,
    confidence: float,
) -> Dict[str, Any]:
    """Record a strength in the student's learning profile."""
    from models import StudentLearningProfile
    StudentLearningProfile.update_one(
        {'student_id': student_id, 'subject': subject},
        {
            '$push': {
                'strengths': {
                    'topic': topic,
                    'confidence': confidence,
                    'recorded_at': datetime.utcnow().isoformat(),
                }
            },
            '$set': {'last_updated': datetime.utcnow()},
        },
        upsert=True,
    )
    return {'recorded': True, 'topic': topic, 'confidence': confidence}


def record_student_weakness(
    student_id: str,
    subject: str,
    topic: str,
    notes: str,
) -> Dict[str, Any]:
    """Record a weakness or area for improvement."""
    from models import StudentLearningProfile
    StudentLearningProfile.update_one(
        {'student_id': student_id, 'subject': subject},
        {
            '$push': {
                'weaknesses': {
                    'topic': topic,
                    'notes': notes,
                    'recorded_at': datetime.utcnow().isoformat(),
                }
            },
            '$set': {'last_updated': datetime.utcnow()},
        },
        upsert=True,
    )
    return {'recorded': True, 'topic': topic}


def record_mistake_pattern(
    student_id: str,
    subject: str,
    pattern: str,
) -> Dict[str, Any]:
    """Record a common mistake pattern the student makes."""
    from models import StudentLearningProfile
    StudentLearningProfile.update_one(
        {'student_id': student_id, 'subject': subject},
        {
            '$push': {
                'common_mistakes': {
                    'pattern': pattern,
                    'frequency': 1,
                    'first_seen': datetime.utcnow().isoformat(),
                }
            },
            '$set': {'last_updated': datetime.utcnow()},
        },
        upsert=True,
    )
    return {'recorded': True, 'pattern': pattern}


# ============================================================================
# LEARNING AGENT
# ============================================================================

class LearningAgent:
    """
    Agno agent that teaches, assesses, and can pull resources or generate
    interactive quizzes during the conversation via tools.
    """

    def __init__(self):
        if not AGNO_AVAILABLE:
            raise ImportError("agno package not installed. Run: pip install agno")
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        self.agent = Agent(
            model=Claude(id="claude-sonnet-4-20250514", api_key=api_key),
            tools=[
                get_module_resources,
                generate_interactive_quiz,
                update_student_mastery,
                record_student_strength,
                record_student_weakness,
                record_mistake_pattern,
            ],
            description="Expert tutor that teaches, assesses, and tracks student learning. You can fetch module resources and generate quizzes during the conversation.",
            instructions=[
                "You are a patient, encouraging tutor helping a student learn.",
                "Adapt your teaching to the student's level and learning profile.",
                "When the student asks for videos, materials, or something to watch/read, use get_module_resources(module_id) to fetch real resources and share titles and links.",
                "When you want to check understanding with a short quiz, use generate_interactive_quiz(module_id, difficulty, question_type) and then present the questions to the student. Tell them you're showing an interactive quiz.",
                "After the student answers correctly, use update_student_mastery with a positive change (e.g. 5). When they make a mistake, use a small negative change and record_mistake_pattern or record_student_weakness if relevant.",
                "Use record_student_strength when they show clear mastery of a topic.",
                "Never give answers directly—guide them to discover. Be encouraging and celebrate progress.",
                "Always use the student_id, module_id, and subject provided in the session context when calling tools.",
            ],
            markdown=True,
        )

    def _session_context(
        self,
        student_id: str,
        module: Dict,
        subject: str,
        student_profile: Optional[Dict] = None,
        chat_history: Optional[List[Dict]] = None,
    ) -> str:
        profile_text = ""
        if student_profile:
            strengths = ", ".join([s.get('topic', '') for s in student_profile.get('strengths', [])])
            weaknesses = ", ".join([w.get('topic', '') for w in student_profile.get('weaknesses', [])])
            mistakes = ", ".join([m.get('pattern', '') for m in student_profile.get('common_mistakes', [])])
            profile_text = f"""
STUDENT PROFILE:
- Strengths: {strengths or 'Not yet identified'}
- Areas to improve: {weaknesses or 'Not yet identified'}
- Common mistake patterns: {mistakes or 'None recorded'}
- Learning style: {student_profile.get('learning_style', 'Unknown')}
"""
        history_text = ""
        if chat_history:
            recent = chat_history[-10:]
            history_text = "\nRECENT CONVERSATION:\n"
            for msg in recent:
                role = "Student" if msg.get('role') == 'student' else "Tutor"
                history_text += f"{role}: {msg.get('content', '')}\n"

        custom_prompt = (module.get('custom_prompt') or '').strip()
        custom_block = ""
        if custom_prompt:
            custom_block = f"""
TEACHER'S CUSTOM PROMPT FOR THIS MODULE (follow these instructions when teaching):
{custom_prompt}

"""
        return f"""CURRENT SESSION (use these values when calling tools):
- student_id: {student_id}
- module_id: {module.get('module_id')}
- subject: {subject}
- Module title: {module.get('title')}
- Learning objectives: {', '.join(module.get('learning_objectives', []))}
{custom_block}{profile_text}
{history_text}
"""

    def chat(
        self,
        message: str,
        student_id: str,
        module: Dict,
        subject: str,
        student_profile: Optional[Dict] = None,
        chat_history: Optional[List[Dict]] = None,
        image_data: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """
        Process a student message. The agent may call tools to pull resources
        or generate a quiz; tool_calls are returned so the frontend can render them.
        """
        try:
            context = self._session_context(
                student_id, module, subject, student_profile, chat_history
            )
            user_input = f"{context}\n\nSTUDENT MESSAGE: {message}"

            run_kwargs = {"input": user_input}
            if image_data:
                run_kwargs["images"] = [image_data]

            response = self.agent.run(**run_kwargs)

            content = getattr(response, "content", None)
            if content is None and hasattr(response, "messages") and response.messages:
                content = getattr(response.messages[-1], "content", str(response.messages[-1]))

            tool_calls = []
            if hasattr(response, "tool_calls") and response.tool_calls:
                for tc in response.tool_calls:
                    name = getattr(tc, "name", getattr(tc, "tool", str(tc)))
                    args = getattr(tc, "arguments", getattr(tc, "args", {}))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    result = getattr(tc, "result", getattr(tc, "output", None))
                    tool_calls.append({"name": name, "arguments": args, "result": result})
            if hasattr(response, "tool_executions") and response.tool_executions:
                for te in response.tool_executions:
                    name = getattr(te, "name", getattr(te, "tool_name", ""))
                    result = getattr(te, "result", getattr(te, "output", None))
                    tool_calls.append({"name": name, "arguments": {}, "result": result})

            return {
                "response": content or "I'm not sure how to respond right now.",
                "tool_calls": tool_calls,
                "success": True,
            }
        except Exception as e:
            logger.exception("Learning agent chat error")
            return {
                "response": "I'm having trouble right now. Let's try again!",
                "tool_calls": [],
                "success": False,
                "error": str(e),
            }


# ============================================================================
# SINGLETON
# ============================================================================

_learning_agent: Optional[LearningAgent] = None


def get_learning_agent() -> Optional[LearningAgent]:
    """Return the Agno learning agent if agno is installed and configured."""
    global _learning_agent
    if not AGNO_AVAILABLE:
        return None
    if _learning_agent is None:
        try:
            _learning_agent = LearningAgent()
        except Exception as e:
            logger.warning("Could not create Agno learning agent: %s", e)
            return None
    return _learning_agent
