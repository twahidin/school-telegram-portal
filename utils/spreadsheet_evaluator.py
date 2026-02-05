"""
Spreadsheet assignment evaluator for the school portal.
Evaluates student Excel submissions against an answer key and produces:
- Text/PDF report of where they went wrong
- Commented Excel (student file with cell comments)

Uses the in-repo evaluator from utils.excel_evaluator (SALES_ANALYSIS mark scheme).
"""
import io
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from utils.excel_evaluator import ExcelEvaluator, MarkScheme

logger = logging.getLogger(__name__)


def evaluate_spreadsheet_submission(
    answer_key_bytes: bytes,
    student_bytes: bytes,
    student_name: str = "Student",
    student_filename: str = "submission.xlsx",
) -> Optional[Dict[str, Any]]:
    """
    Evaluate a student Excel submission against the answer key.
    Returns a dict with marks_awarded, total_marks, percentage, questions (list of question results),
    summary (text), and full result for PDF/Excel generation; or None if evaluation fails.
    """
    with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f_ans:
        f_ans.write(answer_key_bytes)
        ans_path = f_ans.name
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f_stu:
            f_stu.write(student_bytes)
            stu_path = f_stu.name
        try:
            evaluator = ExcelEvaluator(ans_path, MarkScheme())
            result = evaluator.evaluate(stu_path)
            result.student_name = student_name
            result.student_file = student_filename
            return _result_to_dict(result)
        finally:
            try:
                os.unlink(stu_path)
            except Exception:
                pass
    finally:
        try:
            os.unlink(ans_path)
        except Exception:
            pass


def _result_to_dict(result) -> Dict[str, Any]:
    """Convert EvaluationResult to a JSON-serializable dict."""
    return {
        'student_name': result.student_name,
        'student_file': result.student_file,
        'total_marks': result.total_marks,
        'marks_awarded': result.marks_awarded,
        'percentage': result.percentage,
        'summary': result.summary,
        'questions': [
            {
                'question_num': q.question_num,
                'description': q.description,
                'total_marks': q.total_marks,
                'marks_awarded': q.marks_awarded,
                'feedback': q.feedback,
                'cells': [
                    {
                        'cell_ref': c.cell_ref,
                        'feedback': c.feedback,
                        'formula_correct': c.formula_correct,
                        'value_correct': c.value_correct,
                    }
                    for c in (q.cells or [])
                ]
            }
            for q in result.questions
        ],
    }


def generate_text_report(result_dict: Dict[str, Any]) -> str:
    """Generate plain text feedback report from result dict."""
    lines = [
        "=" * 60,
        "EXCEL EVALUATION REPORT",
        "=" * 60,
        f"Student: {result_dict.get('student_name', 'Student')}",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"TOTAL SCORE: {result_dict.get('marks_awarded', 0)}/{result_dict.get('total_marks', 0)} ({result_dict.get('percentage', 0):.1f}%)",
        "",
        "-" * 60,
        "QUESTION BREAKDOWN",
        "-" * 60,
    ]
    for q in result_dict.get('questions', []):
        status = "✓" if q.get('marks_awarded') == q.get('total_marks') else "△" if q.get('marks_awarded', 0) > 0 else "✗"
        lines.append(f"Q{q.get('question_num')}: {q.get('marks_awarded')}/{q.get('total_marks')} {status} - {q.get('description', '')}")
    lines.extend(["", "-" * 60, "DETAILED FEEDBACK", "-" * 60])
    for q in result_dict.get('questions', []):
        lines.append("")
        lines.append(f"Question {q.get('question_num')}: {q.get('description')}")
        lines.append(f"Marks: {q.get('marks_awarded')}/{q.get('total_marks')}")
        lines.append(f"Feedback: {q.get('feedback')}")
        for c in (q.get('cells') or [])[:5]:
            if not c.get('formula_correct') or not c.get('value_correct'):
                lines.append(f"  • {c.get('cell_ref')}: {c.get('feedback')}")
    lines.extend(["", "=" * 60, "END OF REPORT", "=" * 60])
    return "\n".join(lines)


def generate_pdf_report(result_dict: Dict[str, Any]) -> bytes:
    """Generate a PDF feedback report from the result dict."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)
    styles = getSampleStyleSheet()
    style = ParagraphStyle(
        name='Body',
        parent=styles['Normal'],
        fontSize=10,
        leading=14,
    )
    text = generate_text_report(result_dict)
    parts = []
    for line in text.splitlines():
        line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        if line.strip():
            parts.append(Paragraph(line, style))
        else:
            parts.append(Spacer(1, 12))
    doc.build(parts)
    return buf.getvalue()


def generate_commented_excel(student_bytes: bytes, result_dict: Dict[str, Any]) -> bytes:
    """Add feedback comments to the student's Excel file and return the workbook as bytes."""
    import openpyxl
    from openpyxl.comments import Comment

    wb = openpyxl.load_workbook(io.BytesIO(student_bytes))
    ws = wb.active
    author = "Feedback"

    for q in result_dict.get('questions', []):
        for c in (q.get('cells') or []):
            if not c.get('formula_correct') or not c.get('value_correct'):
                cell_ref = c.get('cell_ref')
                feedback = (c.get('feedback') or '').strip()
                if not cell_ref or not feedback:
                    continue
                try:
                    cell = ws[cell_ref]
                    comment_text = f"Q{q.get('question_num')}: {feedback}"
                    cell.comment = Comment(comment_text, author)
                except Exception as e:
                    logger.warning(f"Could not add comment to {cell_ref}: {e}")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
