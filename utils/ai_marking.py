import os
import logging
import base64
import json
import re
from anthropic import Anthropic
from utils.auth import decrypt_api_key
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import optional dependencies
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logger.warning("OpenAI package not installed. Install with: pip install openai")

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("Google Generative AI package not installed. Install with: pip install google-generativeai")

try:
    from pdf2image import convert_from_bytes
    from PIL import Image
    import io
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    logger.warning("pdf2image or PIL not available. PDF to image conversion disabled. Install with: pip install pdf2image pillow")

def convert_pdf_to_images(pdf_bytes: bytes, max_pages: int = 10) -> list:
    """
    Convert PDF pages to JPEG images for providers that don't support PDF directly.
    
    Args:
        pdf_bytes: PDF file content as bytes
        max_pages: Maximum number of pages to convert (to avoid token limits)
    
    Returns:
        List of base64-encoded JPEG image strings
    """
    if not PDF2IMAGE_AVAILABLE:
        return []
    
    try:
        # Convert PDF pages to PIL Images
        images = convert_from_bytes(pdf_bytes, first_page=1, last_page=max_pages)
        
        image_b64_list = []
        for i, img in enumerate(images):
            # Convert PIL Image to JPEG bytes
            img_buffer = io.BytesIO()
            # Convert to RGB if necessary (for JPEG compatibility)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(img_buffer, format='JPEG', quality=85)
            img_buffer.seek(0)
            
            # Encode to base64
            image_b64 = base64.b64encode(img_buffer.read()).decode('utf-8')
            image_b64_list.append(image_b64)
        
        logger.info(f"Converted {len(image_b64_list)} PDF pages to images")
        return image_b64_list
    
    except Exception as e:
        logger.error(f"Error converting PDF to images: {e}")
        return []

def resize_image_for_ai(image_bytes: bytes, max_dimension: int = 1200, quality: int = 85) -> bytes:
    """
    Resize and compress an image to reduce payload size for AI APIs (avoids 413 request_too_large).
    Phone photos are often 3000x4000+ pixels; this reduces them to a manageable size while keeping
    text/figures readable.
    """
    if not PDF2IMAGE_AVAILABLE:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        w, h = img.size
        if w <= max_dimension and h <= max_dimension:
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=quality)
            return out.getvalue()
        ratio = min(max_dimension / w, max_dimension / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=quality)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"Could not resize image for AI: {e}")
        return image_bytes

# Model mappings for each provider
MODEL_MAPPINGS = {
    'anthropic': 'claude-sonnet-4-5-20250929',
    'openai': 'gpt-5.2-2025-12-11',  # GPT-5.2 with vision support
    'deepseek': 'deepseek-reasoner',  # DeepSeek-V3.2 thinking mode (best for complex reasoning tasks like marking)
    'google': 'gemini-3-flash-preview'  # Gemini 3 Flash Preview with vision support
}

VALID_MODEL_TYPES = frozenset(MODEL_MAPPINGS.keys())

def resolve_model_type(assignment, teacher, override_model=None):
    """
    Resolve which AI model to use. Prefer override, then assignment, then teacher default, then anthropic.
    Normalizes empty/invalid values so we never accidentally use a wrong provider.
    """
    if override_model and (override_model or '').strip() and override_model in VALID_MODEL_TYPES:
        model_type = override_model
        logger.info(f"Using override model_type={model_type}")
        return model_type
    raw = (assignment.get('ai_model') if assignment else None)
    if not raw or (isinstance(raw, str) and not raw.strip()) or raw not in VALID_MODEL_TYPES:
        raw = (teacher.get('default_ai_model') if teacher else None)
    if not raw or (isinstance(raw, str) and not raw.strip()) or raw not in VALID_MODEL_TYPES:
        raw = 'anthropic'
    logger.info(f"Resolved model_type={raw} (assignment.ai_model={assignment.get('ai_model') if assignment else None}, teacher.default_ai_model={teacher.get('default_ai_model') if teacher else None})")
    return raw

def get_available_ai_models(teacher):
    """Return dict of model_type -> True if that model has an API key configured (teacher or env)."""
    available = {}
    # Anthropic
    has_anthropic = (teacher and teacher.get('anthropic_api_key')) or bool(os.getenv('ANTHROPIC_API_KEY'))
    available['anthropic'] = bool(has_anthropic)
    # OpenAI
    has_openai = (teacher and teacher.get('openai_api_key')) or bool(os.getenv('OPENAI_API_KEY'))
    available['openai'] = bool(has_openai and OPENAI_AVAILABLE)
    # DeepSeek
    has_deepseek = (teacher and teacher.get('deepseek_api_key')) or bool(os.getenv('DEEPSEEK_API_KEY'))
    available['deepseek'] = bool(has_deepseek and OPENAI_AVAILABLE)
    # Google
    has_google = (teacher and teacher.get('google_api_key')) or bool(os.getenv('GOOGLE_API_KEY'))
    available['google'] = bool(has_google and GEMINI_AVAILABLE)
    return available

def get_teacher_ai_service(teacher, model_type='anthropic'):
    """
    Get AI service configured for a specific teacher and model type
    
    Args:
        teacher: Teacher document with API keys
        model_type: 'anthropic', 'openai', 'deepseek', or 'google'
    
    Returns:
        Tuple of (client, model_name, provider_type) or (None, None, None) if unavailable
    """
    if model_type == 'anthropic':
        api_key = None
        if teacher and teacher.get('anthropic_api_key'):
            api_key = decrypt_api_key(teacher['anthropic_api_key'])
        if not api_key:
            api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            logger.warning("No Anthropic API key available")
            return None, None, None
        try:
            client = Anthropic(api_key=api_key)
            return client, MODEL_MAPPINGS['anthropic'], 'anthropic'
        except Exception as e:
            logger.error(f"Error creating Anthropic client: {e}")
            return None, None, None
    
    elif model_type == 'openai':
        if not OPENAI_AVAILABLE:
            logger.error("OpenAI package not installed")
            return None, None, None
        api_key = None
        if teacher and teacher.get('openai_api_key'):
            api_key = decrypt_api_key(teacher['openai_api_key'])
        if not api_key:
            api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            logger.warning("No OpenAI API key available")
            return None, None, None
        try:
            client = OpenAI(api_key=api_key)
            return client, MODEL_MAPPINGS['openai'], 'openai'
        except Exception as e:
            logger.error(f"Error creating OpenAI client: {e}")
            return None, None, None
    
    elif model_type == 'deepseek':
        if not OPENAI_AVAILABLE:
            logger.error("OpenAI package required for DeepSeek (uses OpenAI-compatible API)")
            return None, None, None
        api_key = None
        if teacher and teacher.get('deepseek_api_key'):
            api_key = decrypt_api_key(teacher['deepseek_api_key'])
        if not api_key:
            api_key = os.getenv('DEEPSEEK_API_KEY')
        if not api_key:
            logger.warning("No DeepSeek API key available")
            return None, None, None
        try:
            # DeepSeek uses OpenAI-compatible API but different base URL
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            return client, MODEL_MAPPINGS['deepseek'], 'deepseek'
        except Exception as e:
            logger.error(f"Error creating DeepSeek client: {e}")
            return None, None, None
    
    elif model_type == 'google':
        if not GEMINI_AVAILABLE:
            logger.error("Google Generative AI package not installed")
            return None, None, None
        api_key = None
        if teacher and teacher.get('google_api_key'):
            api_key = decrypt_api_key(teacher['google_api_key'])
        if not api_key:
            api_key = os.getenv('GOOGLE_API_KEY')
        if not api_key:
            logger.warning("No Google Gemini API key available")
            return None, None, None
        try:
            genai.configure(api_key=api_key)
            return genai, MODEL_MAPPINGS['google'], 'google'
        except Exception as e:
            logger.error(f"Error configuring Google Gemini: {e}")
            return None, None, None
    
    else:
        logger.error(f"Unknown model type: {model_type}")
        return None, None, None

def make_ai_api_call(client, model_name, provider, system_prompt, messages_content, max_tokens=4000, assignment=None):
    """
    Unified API call function that handles different provider formats
    
    Args:
        client: The AI service client
        model_name: Model name to use
        provider: 'anthropic', 'openai', 'deepseek', or 'google'
        system_prompt: System prompt string
        messages_content: List of content items (text, images, etc.)
        max_tokens: Maximum tokens in response
        assignment: Optional assignment dict to access extracted text for PDFs
    
    Returns:
        Response text string
    
    Note: The "max_completion_tokens" error is from OpenAI only. Claude (Anthropic) uses max_tokens.
    If you see that error, check that the assignment/teacher AI model is set to Claude (Anthropic), not OpenAI.
    """
    try:
        logger.info(f"Making AI API call with provider={provider}, model={model_name}")
        if provider == 'anthropic':
            # Claude (Anthropic) uses max_tokens - no max_completion_tokens
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": messages_content}],
                system=system_prompt
            )
            return message.content[0].text
        
        elif provider in ['openai', 'deepseek']:
            # OpenAI/DeepSeek format - need to convert content format
            openai_messages = []
            if system_prompt:
                openai_messages.append({"role": "system", "content": system_prompt})
            
            # Convert Anthropic-style content to OpenAI format
            user_content = []
            for item in messages_content:
                if isinstance(item, dict):
                    if item.get('type') == 'text':
                        user_content.append({"type": "text", "text": item.get('text', '')})
                    elif item.get('type') == 'image':
                        # OpenAI format for images
                        image_data = item.get('source', {}).get('data', '')
                        media_type = item.get('source', {}).get('media_type', 'image/jpeg')
                        user_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_data}"
                            }
                        })
                    elif item.get('type') == 'document':
                        # For PDFs, OpenAI/DeepSeek doesn't support PDF directly in vision API
                        # Convert PDF pages to images to preserve diagrams and visual content
                        pdf_data = item.get('source', {}).get('data', '')
                        pdf_bytes = base64.b64decode(pdf_data)
                        
                        # Convert PDF to images (preserves diagrams, formulas, etc.)
                        pdf_images = convert_pdf_to_images(pdf_bytes, max_pages=10)
                        
                        if pdf_images:
                            # Add each page as an image
                            for page_num, img_b64 in enumerate(pdf_images, 1):
                                user_content.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{img_b64}"
                                    }
                                })
                                # Add page label
                                user_content.append({
                                    "type": "text",
                                    "text": f"(PDF Page {page_num})"
                                })
                            logger.info(f"Converted PDF to {len(pdf_images)} images for OpenAI/DeepSeek")
                        else:
                            # Fallback: try extracted text if conversion failed
                            pdf_text = None
                            if assignment:
                                # Check previous messages to identify PDF type
                                context_text = ""
                                for prev_item in messages_content[:messages_content.index(item)]:
                                    if isinstance(prev_item, dict) and prev_item.get('type') == 'text':
                                        context_text += prev_item.get('text', '') + " "
                                
                                # Identify PDF type based on context
                                context_lower = context_text.lower()
                                if "answer key" in context_lower or "answer_key" in context_lower:
                                    pdf_text = assignment.get('answer_key_text', '')
                                elif "question paper" in context_lower or "question_paper" in context_lower:
                                    pdf_text = assignment.get('question_paper_text', '')
                                elif "rubric" in context_lower:
                                    pdf_text = assignment.get('rubrics_text', '')
                                elif "reference" in context_lower:
                                    pdf_text = assignment.get('reference_materials_text', '')
                            
                            if pdf_text and pdf_text.strip():
                                # Use extracted text as fallback
                                user_content.append({
                                    "type": "text",
                                    "text": f"[PDF Content - Extracted Text (diagrams may be missing)]:\n{pdf_text}"
                                })
                            else:
                                # Final fallback: warning message
                                user_content.append({
                                    "type": "text",
                                    "text": "[PDF document - Note: PDF to image conversion failed. For best results with PDFs containing diagrams, use Anthropic Claude or Google Gemini, or ensure pdf2image is properly installed with poppler-utils.]"
                                })
                elif isinstance(item, str):
                    user_content.append({"type": "text", "text": item})
            
            # Combine text parts and images
            text_parts = [c.get('text', '') for c in user_content if c.get('type') == 'text']
            image_parts = [c for c in user_content if c.get('type') == 'image_url']
            
            if image_parts:
                # OpenAI vision API format
                content_list = []
                for text in text_parts:
                    if text.strip():
                        content_list.append({"type": "text", "text": text})
                content_list.extend(image_parts)
                user_content = content_list
            else:
                # Text only
                user_content = [{"type": "text", "text": " ".join(text_parts)}]
            
            openai_messages.append({"role": "user", "content": user_content})
            
            # OpenAI API: use max_completion_tokens only (max_tokens is unsupported on current models)
            response = client.chat.completions.create(
                model=model_name,
                messages=openai_messages,
                max_completion_tokens=max_tokens
            )
            return response.choices[0].message.content
        
        elif provider == 'google':
            # Google Gemini format
            # client is the genai module (configured globally)
            # Combine system prompt with user content
            full_prompt = (system_prompt + "\n\n") if system_prompt else ""
            
            # Build content parts for Gemini
            content_parts = []
            
            for item in messages_content:
                if isinstance(item, dict):
                    if item.get('type') == 'text':
                        full_prompt += item.get('text', '') + "\n"
                    elif item.get('type') == 'image':
                        image_data = item.get('source', {}).get('data', '')
                        content_parts.append({
                            "mime_type": item.get('source', {}).get('media_type', 'image/jpeg'),
                            "data": base64.b64decode(image_data)
                        })
                    elif item.get('type') == 'document':
                        # Gemini supports PDF
                        pdf_data = item.get('source', {}).get('data', '')
                        content_parts.append({
                            "mime_type": "application/pdf",
                            "data": base64.b64decode(pdf_data)
                        })
                elif isinstance(item, str):
                    full_prompt += item + "\n"
            
            # Add text prompt first
            if full_prompt.strip():
                content_parts.insert(0, full_prompt.strip())
            
            # client is genai module for Google
            model = client.GenerativeModel(model_name)
            response = model.generate_content(content_parts)
            return response.text
        
        else:
            raise ValueError(f"Unknown provider: {provider}")
    
    except Exception as e:
        logger.error(f"Error making API call to {provider}: {e}")
        raise

# Limit pages sent to AI to avoid 413 request_too_large (API max request size)
MAX_PAGES_FOR_AI = 20

def analyze_submission_images(pages: list, assignment: dict, answer_key_content: bytes = None, teacher: dict = None, override_ai_model: str = None) -> dict:
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
    # Limit pages to avoid 413 request_too_large
    if len(pages) > MAX_PAGES_FOR_AI:
        pages = pages[:MAX_PAGES_FOR_AI]
        logger.warning(f"Limiting to first {MAX_PAGES_FOR_AI} pages to avoid request size limit")
    
    # Resolve model type (override → assignment → teacher default → anthropic)
    model_type = resolve_model_type(assignment, teacher, override_ai_model)
    
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {
            'error': f'AI service not available for {model_type}',
            'questions': [],
            'overall_feedback': f'AI feedback unavailable - no {model_type} API key configured'
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
        
        # Add student submission pages (resize images to reduce payload and avoid 413)
        for i, page in enumerate(pages):
            if page['type'] == 'image':
                # Image submission - resize/compress to avoid request_too_large (413)
                image_data = resize_image_for_ai(page['data'])
                image_b64 = base64.standard_b64encode(image_data).decode('utf-8')
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
        
        # Make API call using unified function (generous max_tokens so long feedback isn't truncated)
        response_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt=system_prompt,
            messages_content=content,
            max_tokens=16384,
            assignment=assignment
        )
        
        # Parse JSON response
        result = parse_ai_response(response_text)
        result['generated_at'] = datetime.utcnow().isoformat()
        result['raw_response'] = response_text
        
        return result
        
    except Exception as e:
        err_str = str(e)
        logger.error(f"Error analyzing submission: {e}")
        is_413 = '413' in err_str or 'request_too_large' in err_str.lower()
        return {
            'error': err_str,
            'error_code': 'request_too_large' if is_413 else None,
            'questions': [],
            'overall_feedback': (
                'Submission too large for AI (413). Please ask the student to resubmit with fewer or smaller images (e.g. one photo per page, lower resolution), or use Remark to try again.'
                if is_413 else f'Error generating feedback: {err_str}'
            )
        }

def parse_ai_response(response_text: str) -> dict:
    """Parse AI response into structured format. Strips markdown code fences and handles truncated JSON."""
    if not response_text or not response_text.strip():
        return {'error': 'Empty response', 'raw': response_text}
    # Strip markdown code block if present (e.g. ```json ... ``` or ``` ... ```)
    text = response_text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text)
    try:
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            json_str = json_match.group()
            return json.loads(json_str)
        return {'error': 'Could not parse response', 'raw': response_text}
    except json.JSONDecodeError:
        # Likely truncated when assignment is large (model hit max_tokens)
        truncated_hint = (
            ' Response may have been cut off; this assignment might have too many questions or long feedback. '
            'Try "Remark" again or use fewer pages.'
            if len(text) > 300 and ('"questions"' in text or '"question_num"' in text) else ''
        )
        return {
            'error': f'Invalid JSON{truncated_hint}'.strip(),
            'raw': response_text
        }

def analyze_single_page(page_data: bytes, page_type: str, assignment: dict, teacher: dict = None) -> dict:
    """
    Analyze a single page for quick feedback (during upload)
    
    Returns quick feedback for initial review
    """
    model_type = assignment.get('ai_model') or (teacher.get('default_ai_model') if teacher else None) or 'anthropic'
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {'error': f'AI not available for {model_type}'}
    
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
        
        response_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt="",
            messages_content=content,
            max_tokens=1000,
            assignment=assignment
        )
        
        return {
            'preview_feedback': response_text,
            'generated_at': datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in quick analysis: {e}")
        return {'error': str(e)}

def mark_submission(submission: dict, assignment: dict, teacher: dict = None) -> dict:
    """
    Legacy function - Use AI to mark a text-based student submission
    """
    model_type = assignment.get('ai_model') or (teacher.get('default_ai_model') if teacher else None) or 'anthropic'
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {
            'error': f'AI service not available for {model_type}',
            'questions': {},
            'overall': f'Unable to generate AI feedback - no {model_type} API key configured'
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

        content = [{"type": "text", "text": prompt}]
        feedback_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt="",
            messages_content=content,
            max_tokens=2000,
            assignment=assignment
        )
        
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
    model_type = assignment.get('ai_model') or (teacher.get('default_ai_model') if teacher else None) or 'anthropic'
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {
            'error': f'AI service not available for {model_type}',
            'feedback': f'AI feedback unavailable - no {model_type} API key configured'
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
        response_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt=system_prompt,
            messages_content=content,
            max_tokens=1500,
            assignment=assignment
        )
        
        # Parse JSON response
        result = parse_ai_response(response_text)
        return result
        
    except Exception as e:
        logger.error(f"Error getting preview feedback: {e}")
        return {
            'error': str(e),
            'feedback': f'Error generating feedback: {str(e)}'
        }

def get_quick_feedback(answer: str, question: str, model_answer: str = None, teacher: dict = None, assignment: dict = None) -> str:
    """Get quick feedback on a single text answer"""
    model_type = (assignment.get('ai_model') if assignment else None) or (teacher.get('default_ai_model') if teacher else None) or 'anthropic'
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return f"AI feedback not available for {model_type}"
    
    try:
        prompt = f"""Provide brief, constructive feedback (2-3 sentences) on this student answer.

Question: {question}
{"Model Answer: " + model_answer if model_answer else ""}
Student Answer: {answer}

Give specific, helpful feedback focusing on what's good and what could be improved."""

        content = [{"type": "text", "text": prompt}]
        return make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt="",
            messages_content=content,
            max_tokens=300,
            assignment=assignment
        )
        
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
    model_type = assignment.get('ai_model') or (teacher.get('default_ai_model') if teacher else None) or 'anthropic'
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {
            'error': f'AI service not available for {model_type}',
            'response': f'AI help unavailable - no {model_type} API key configured'
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
        response_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt=system_prompt,
            messages_content=content_parts,
            max_tokens=1000,
            assignment=assignment
        )
        
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

def extract_answers_from_key(file_content: bytes, file_type: str, question_count: int, teacher: dict = None, assignment: dict = None) -> dict:
    """
    Extract answers from an uploaded answer key file (PDF or image).
    
    Args:
        file_content: The file content as bytes
        file_type: 'pdf' or 'image'
        question_count: Number of questions to extract answers for
        teacher: Teacher document for API key
        assignment: Assignment document (optional, for model selection)
    
    Returns:
        Dictionary with extracted answers for each question
    """
    model_type = (assignment.get('ai_model') if assignment else None) or (teacher.get('default_ai_model') if teacher else None) or 'anthropic'
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {
            'error': f'AI service not available for {model_type}',
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
        response_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt="",
            messages_content=content,
            max_tokens=4000,
            assignment=assignment
        )
        
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


def analyze_essay_with_rubrics(pages: list, assignment: dict, rubrics_content: bytes = None, teacher: dict = None, override_ai_model: str = None) -> dict:
    """
    Analyze student essay submission using rubrics for criteria-based marking.
    
    This is specifically designed for essays/compositions where:
    - Marking is based on rubric criteria (e.g., Content, Language, Organisation)
    - Detailed sentence-level corrections are needed
    - The AI evaluates writing quality, not correct/incorrect answers
    
    Args:
        pages: List of page dictionaries with 'type' and 'data' keys (student's essay)
        assignment: Assignment document with details (including rubrics_text)
        rubrics_content: Optional bytes of rubrics PDF for vision analysis
        teacher: Teacher document for API key
        override_ai_model: Optional model key to use for this run (e.g. 'anthropic', 'openai')
    
    Returns:
        Dictionary with:
        - criteria: List of {name, reasoning, afi, marks_awarded, max_marks}
        - errors: List of {location, error, correction, feedback}
        - overall_feedback: General assessment
        - total_marks: Sum of marks awarded
        - submission_quality: 'acceptable', 'poor', 'wrong_submission' for rejection logic
    """
    model_type = resolve_model_type(assignment, teacher, override_ai_model)
    client, model_name, provider = get_teacher_ai_service(teacher, model_type)
    if not client:
        return {
            'error': f'AI service not available for {model_type}',
            'criteria': [],
            'errors': [],
            'overall_feedback': f'AI feedback unavailable - no {model_type} API key configured',
            'submission_quality': 'unknown'
        }
    
    # Limit pages to avoid 413 request_too_large
    if len(pages) > MAX_PAGES_FOR_AI:
        pages = pages[:MAX_PAGES_FOR_AI]
        logger.warning(f"Limiting essay to first {MAX_PAGES_FOR_AI} pages to avoid request size limit")
    
    try:
        content = []
        
        # Get rubrics text from assignment
        rubrics_text = assignment.get('rubrics_text', '')
        
        # Build system prompt for essay analysis
        system_prompt = f"""You are an experienced English/Language teacher marking student essays.

Assignment: {assignment.get('title', 'Essay')}
Subject: {assignment.get('subject', 'English')}
Total Marks: {assignment.get('total_marks', 30)}

{"GRADING RUBRICS:" if rubrics_text else "Use standard essay rubrics:"}
{rubrics_text if rubrics_text else '''
Default criteria:
- Content (10 marks): Relevance, ideas, engagement with topic
- Language (10 marks): Grammar, vocabulary, sentence structure
- Organisation (10 marks): Paragraphing, coherence, flow
'''}

IMPORTANT INSTRUCTIONS:
1. First, check if this is actually an essay/composition submission. If the student submitted:
   - A blank/mostly blank page
   - Wrong assignment (e.g., math homework instead of essay)
   - Illegible/unreadable content
   - Off-topic content unrelated to the assignment
   Mark submission_quality as "wrong_submission" or "poor" accordingly.

2. For valid essays, evaluate EACH rubric criterion separately with:
   - A mark out of the maximum
   - Clear reasoning for the mark
   - Specific areas for improvement (AFI)

3. CRITICAL: Extract SPECIFIC sentences from the essay that need improvement. For each error:
   - Quote the exact sentence or phrase from the essay
   - Identify what type of error it is (grammar, spelling, vocabulary, clarity, etc.)
   - Provide the corrected version
   - Give brief feedback on why it's wrong

4. Look for these common issues:
   - Subject-verb agreement errors
   - Tense inconsistencies
   - Run-on sentences or fragments
   - Spelling mistakes
   - Unclear or awkward phrasing
   - Punctuation errors
   - Vocabulary misuse

Respond ONLY with valid JSON in this exact format:
{{
    "submission_quality": "acceptable" or "poor" or "wrong_submission",
    "rejection_reason": "only if wrong_submission - explain why (e.g., 'This appears to be a math worksheet, not an essay')",
    "criteria": [
        {{
            "name": "Content",
            "max_marks": 10,
            "marks_awarded": 7,
            "reasoning": "Clear explanation of why this mark was given...",
            "afi": "Specific suggestions for improvement in this area..."
        }},
        {{
            "name": "Language",
            "max_marks": 10,
            "marks_awarded": 6,
            "reasoning": "Assessment of grammar, vocabulary, sentence structure...",
            "afi": "Areas to work on for language improvement..."
        }},
        {{
            "name": "Organisation",
            "max_marks": 10,
            "marks_awarded": 7,
            "reasoning": "Evaluation of structure, paragraphing, flow...",
            "afi": "Suggestions for better organization..."
        }}
    ],
    "errors": [
        {{
            "location": "Paragraph 1, Line 2",
            "error": "The exact sentence with the error quoted from essay",
            "correction": "The corrected version of the sentence",
            "feedback": "Brief explanation (e.g., 'Subject-verb agreement: use 'was' not 'were' for singular subject')"
        }},
        {{
            "location": "Paragraph 2, Line 1",
            "error": "Another problematic sentence...",
            "correction": "Corrected version...",
            "feedback": "Explanation of the error..."
        }}
    ],
    "overall_feedback": "2-3 paragraphs of constructive overall feedback about the essay",
    "total_marks": 20,
    "strengths": ["What the student did well", "Another strength"],
    "priority_improvements": ["Most important thing to work on", "Second priority"]
}}"""

        # Add rubrics PDF with vision if available
        if rubrics_content:
            content.append({
                "type": "text",
                "text": "GRADING RUBRICS (reference document):"
            })
            rubrics_b64 = base64.standard_b64encode(rubrics_content).decode('utf-8')
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": rubrics_b64
                }
            })
        
        content.append({
            "type": "text",
            "text": "\nSTUDENT'S ESSAY SUBMISSION:"
        })
        
        # Add student submission pages (resize images to reduce payload and avoid 413)
        for i, page in enumerate(pages):
            if page['type'] == 'image':
                image_data = resize_image_for_ai(page['data'])
                image_b64 = base64.standard_b64encode(image_data).decode('utf-8')
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
        
        # Add teacher's custom instructions if available
        custom_instructions = ""
        feedback_instructions = assignment.get('feedback_instructions', '')
        grading_instructions = assignment.get('grading_instructions', '')
        if feedback_instructions:
            custom_instructions += f"\n\nTEACHER'S FEEDBACK STYLE INSTRUCTIONS: {feedback_instructions}"
        if grading_instructions:
            custom_instructions += f"\n\nTEACHER'S GRADING INSTRUCTIONS: {grading_instructions}"
        
        content.append({
            "type": "text",
            "text": f"""
{custom_instructions}

Analyze this essay submission and provide detailed rubric-based feedback with specific sentence corrections.
Respond with JSON:"""
        })
        
        # Make API call
        response_text = make_ai_api_call(
            client=client,
            model_name=model_name,
            provider=provider,
            system_prompt=system_prompt,
            messages_content=content,
            max_tokens=6000,
            assignment=assignment
        )
        
        # Parse JSON response
        result = parse_ai_response(response_text)
        result['generated_at'] = datetime.utcnow().isoformat()
        result['raw_response'] = response_text
        
        # Ensure required fields exist with defaults
        if 'criteria' not in result:
            result['criteria'] = []
        if 'errors' not in result:
            result['errors'] = []
        if 'submission_quality' not in result:
            result['submission_quality'] = 'acceptable'
        if 'total_marks' not in result:
            # Calculate from criteria if not provided
            result['total_marks'] = sum(c.get('marks_awarded', 0) for c in result.get('criteria', []))
        
        return result
        
    except Exception as e:
        err_str = str(e)
        logger.error(f"Error analyzing essay with rubrics: {e}")
        is_413 = '413' in err_str or 'request_too_large' in err_str.lower()
        return {
            'error': err_str,
            'error_code': 'request_too_large' if is_413 else None,
            'criteria': [],
            'errors': [],
            'overall_feedback': (
                'Submission too large for AI (413). Please ask the student to resubmit with fewer or smaller images (e.g. one photo per page, lower resolution), or use Remark to try again.'
                if is_413 else f'Error generating feedback: {err_str}'
            ),
            'submission_quality': 'unknown'
        }
