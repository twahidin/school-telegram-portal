import io
import logging
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY, TA_RIGHT

logger = logging.getLogger(__name__)

# Colors
PRIMARY_COLOR = HexColor('#667eea')
SECONDARY_COLOR = HexColor('#764ba2')
SUCCESS_COLOR = HexColor('#28a745')
DANGER_COLOR = HexColor('#dc3545')
WARNING_COLOR = HexColor('#ffc107')
TEXT_COLOR = HexColor('#333333')
LIGHT_GRAY = HexColor('#f8f9fa')
BORDER_COLOR = HexColor('#dee2e6')

def get_styles():
    """Get custom paragraph styles"""
    styles = getSampleStyleSheet()
    
    styles.add(ParagraphStyle(
        name='Title_Custom',
        parent=styles['Title'],
        fontSize=22,
        textColor=PRIMARY_COLOR,
        spaceAfter=20,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    ))
    
    styles.add(ParagraphStyle(
        name='Heading_Custom',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=PRIMARY_COLOR,
        spaceBefore=15,
        spaceAfter=10,
        fontName='Helvetica-Bold'
    ))
    
    styles.add(ParagraphStyle(
        name='SubHeading',
        parent=styles['Heading2'],
        fontSize=12,
        textColor=SECONDARY_COLOR,
        spaceBefore=12,
        spaceAfter=6,
        fontName='Helvetica-Bold'
    ))
    
    styles.add(ParagraphStyle(
        name='Body_Custom',
        parent=styles['Normal'],
        fontSize=10,
        textColor=TEXT_COLOR,
        alignment=TA_JUSTIFY,
        spaceAfter=6,
        leading=14
    ))
    
    styles.add(ParagraphStyle(
        name='TableCell',
        parent=styles['Normal'],
        fontSize=9,
        textColor=TEXT_COLOR,
        leading=12
    ))
    
    styles.add(ParagraphStyle(
        name='TableHeader',
        parent=styles['Normal'],
        fontSize=9,
        textColor=white,
        fontName='Helvetica-Bold',
        alignment=TA_CENTER
    ))
    
    styles.add(ParagraphStyle(
        name='Footer',
        parent=styles['Normal'],
        fontSize=8,
        textColor=HexColor('#888888'),
        alignment=TA_CENTER
    ))
    
    return styles

def generate_review_pdf(submission: dict, assignment: dict, student: dict, teacher: dict = None) -> bytes:
    """
    Generate a comprehensive PDF feedback report with feedback table
    
    Args:
        submission: The submission document with answers and feedback
        assignment: The assignment document
        student: The student document
        teacher: Optional teacher document
    
    Returns:
        PDF content as bytes
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5*cm,
        leftMargin=1.5*cm,
        topMargin=1.5*cm,
        bottomMargin=1.5*cm
    )
    
    styles = get_styles()
    story = []
    
    # Header with school info
    story.append(Paragraph("📚 Assignment Feedback Report", styles['Title_Custom']))
    story.append(Spacer(1, 5))
    
    # Info box
    info_data = [
        ['Student:', student.get('name', 'Unknown'), 'Date:', datetime.utcnow().strftime('%d %B %Y')],
        ['ID:', student.get('student_id', 'N/A'), 'Class:', student.get('class', 'N/A')],
        ['Assignment:', assignment.get('title', 'Untitled'), 'Subject:', assignment.get('subject', 'N/A')],
    ]
    
    info_table = Table(info_data, colWidths=[2*cm, 6*cm, 2*cm, 6*cm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TEXTCOLOR', (0, 0), (-1, -1), TEXT_COLOR),
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
        ('BOX', (0, 0), (-1, -1), 1, BORDER_COLOR),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 15))
    
    # Score summary box
    final_marks = submission.get('final_marks')
    total_marks = assignment.get('total_marks', 100)
    
    if final_marks is not None:
        percentage = (final_marks / total_marks * 100) if total_marks > 0 else 0
        grade = get_grade(percentage)
        
        score_data = [[
            Paragraph(f"<b>Total Score</b>", styles['TableCell']),
            Paragraph(f"<b>{final_marks} / {total_marks}</b>", styles['TableCell']),
            Paragraph(f"<b>{percentage:.1f}%</b>", styles['TableCell']),
            Paragraph(f"<b>Grade: {grade}</b>", styles['TableCell'])
        ]]
        
        score_table = Table(score_data, colWidths=[4*cm, 4*cm, 4*cm, 4*cm])
        score_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), PRIMARY_COLOR),
            ('TEXTCOLOR', (0, 0), (-1, -1), white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('PADDING', (0, 0), (-1, -1), 10),
            ('BOX', (0, 0), (-1, -1), 2, PRIMARY_COLOR),
        ]))
        story.append(score_table)
        story.append(Spacer(1, 15))
    
    # Feedback Table Header
    story.append(Paragraph("Detailed Feedback", styles['Heading_Custom']))
    
    # Build feedback table
    ai_feedback = submission.get('ai_feedback', {})
    teacher_feedback = submission.get('teacher_feedback', {})
    questions = ai_feedback.get('questions', [])
    
    if questions:
        # Table headers
        table_data = [[
            Paragraph('<b>Q#</b>', styles['TableHeader']),
            Paragraph('<b>Student Answer</b>', styles['TableHeader']),
            Paragraph('<b>Correct Answer</b>', styles['TableHeader']),
            Paragraph('<b>Feedback</b>', styles['TableHeader']),
            Paragraph('<b>Marks</b>', styles['TableHeader'])
        ]]
        
        # Add rows for each question
        for q in questions:
            q_num = q.get('question_num', '?')
            
            # Get teacher edits if available
            teacher_q = teacher_feedback.get('questions', {}).get(str(q_num), {})
            
            student_answer = q.get('student_answer', '')
            if student_answer == 'UNCLEAR' or q.get('needs_review'):
                student_answer = '(Unclear/Needs Review)'
            
            correct_answer = teacher_q.get('correct_answer', q.get('correct_answer', ''))
            feedback = teacher_q.get('feedback', q.get('feedback', ''))
            marks = teacher_q.get('marks', q.get('marks_awarded', ''))
            marks_total = q.get('marks_total', '?')
            
            # Status indicator
            if q.get('is_correct') == True:
                status = "✓"
            elif q.get('is_correct') == False:
                status = "✗"
            else:
                status = "?"
            
            row = [
                Paragraph(f"<b>{q_num}</b><br/>{status}", styles['TableCell']),
                Paragraph(truncate_text(student_answer, 100), styles['TableCell']),
                Paragraph(truncate_text(correct_answer, 100), styles['TableCell']),
                Paragraph(truncate_text(feedback, 150), styles['TableCell']),
                Paragraph(f"<b>{marks}</b>/{marks_total}" if marks != '' else f"?/{marks_total}", styles['TableCell'])
            ]
            table_data.append(row)
        
        # Create table
        feedback_table = Table(table_data, colWidths=[1.2*cm, 4*cm, 4*cm, 5*cm, 1.8*cm])
        feedback_table.setStyle(TableStyle([
            # Header row
            ('BACKGROUND', (0, 0), (-1, 0), PRIMARY_COLOR),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            # Alternating rows
            ('BACKGROUND', (0, 1), (-1, -1), white),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, LIGHT_GRAY]),
            # Borders
            ('BOX', (0, 0), (-1, -1), 1, BORDER_COLOR),
            ('GRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            # Alignment
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('ALIGN', (-1, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            # Padding
            ('PADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(feedback_table)
    else:
        story.append(Paragraph("No detailed question feedback available.", styles['Body_Custom']))
    
    story.append(Spacer(1, 15))
    
    # Overall feedback section
    overall = teacher_feedback.get('overall_feedback') or ai_feedback.get('overall_feedback', '')
    if overall:
        story.append(Paragraph("Overall Comments", styles['Heading_Custom']))
        
        # Create a styled box for overall feedback
        overall_data = [[Paragraph(overall, styles['Body_Custom'])]]
        overall_table = Table(overall_data, colWidths=[16*cm])
        overall_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#e8f5e9')),
            ('BOX', (0, 0), (-1, -1), 1, SUCCESS_COLOR),
            ('PADDING', (0, 0), (-1, -1), 10),
        ]))
        story.append(overall_table)
    
    # Areas for improvement
    improvement_notes = []
    for q in questions:
        if q.get('improvement'):
            improvement_notes.append(f"Q{q.get('question_num', '?')}: {q.get('improvement')}")
    
    if improvement_notes:
        story.append(Spacer(1, 15))
        story.append(Paragraph("Areas for Improvement", styles['Heading_Custom']))
        for note in improvement_notes:
            story.append(Paragraph(f"• {note}", styles['Body_Custom']))
    
    # Footer
    story.append(Spacer(1, 30))
    story.append(HRFlowable(width="100%", thickness=1, color=BORDER_COLOR))
    story.append(Spacer(1, 5))
    
    teacher_name = teacher.get('name', 'Teacher') if teacher else 'Teacher'
    footer_text = f"Reviewed by: {teacher_name} | Generated: {datetime.utcnow().strftime('%d %B %Y, %H:%M UTC')}"
    story.append(Paragraph(footer_text, styles['Footer']))
    
    # Build PDF
    try:
        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        raise

def generate_feedback_pdf(submission: dict, assignment: dict, student: dict) -> bytes:
    """Legacy function - redirects to generate_review_pdf"""
    return generate_review_pdf(submission, assignment, student)

def generate_batch_feedback_pdf(submissions: list, assignment: dict, students_map: dict, teacher: dict = None) -> bytes:
    """
    Generate a batch PDF with feedback for multiple students
    
    Args:
        submissions: List of submission documents
        assignment: The assignment document
        students_map: Dictionary mapping student_id to student document
        teacher: Optional teacher document
    
    Returns:
        PDF content as bytes
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5*cm,
        leftMargin=1.5*cm,
        topMargin=1.5*cm,
        bottomMargin=1.5*cm
    )
    
    styles = get_styles()
    story = []
    
    for i, submission in enumerate(submissions):
        if i > 0:
            story.append(PageBreak())
        
        student = students_map.get(submission.get('student_id'), {})
        
        # Add individual feedback page
        story.extend(generate_student_feedback_elements(submission, assignment, student, teacher, styles))
    
    try:
        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Error generating batch PDF: {e}")
        raise

def generate_student_feedback_elements(submission: dict, assignment: dict, student: dict, teacher: dict, styles) -> list:
    """Generate story elements for a single student's feedback"""
    elements = []
    
    # Header
    elements.append(Paragraph(f"Feedback: {assignment.get('title', 'Assignment')}", styles['Title_Custom']))
    
    # Student info
    info_text = f"<b>Student:</b> {student.get('name', 'Unknown')} ({student.get('student_id', 'N/A')}) | <b>Class:</b> {student.get('class', 'N/A')}"
    elements.append(Paragraph(info_text, styles['Body_Custom']))
    elements.append(Spacer(1, 10))
    
    # Score
    final_marks = submission.get('final_marks')
    total_marks = assignment.get('total_marks', 100)
    
    if final_marks is not None:
        percentage = (final_marks / total_marks * 100) if total_marks > 0 else 0
        elements.append(Paragraph(f"<b>Score: {final_marks}/{total_marks} ({percentage:.1f}%)</b>", styles['Heading_Custom']))
    
    elements.append(Spacer(1, 10))
    
    # Feedback table (simplified for batch)
    ai_feedback = submission.get('ai_feedback', {})
    teacher_feedback = submission.get('teacher_feedback', {})
    questions = ai_feedback.get('questions', [])
    
    if questions:
        table_data = [['Q#', 'Status', 'Marks', 'Feedback']]
        
        for q in questions:
            teacher_q = teacher_feedback.get('questions', {}).get(str(q.get('question_num', '')), {})
            
            status = "✓" if q.get('is_correct') else "✗" if q.get('is_correct') == False else "?"
            marks = teacher_q.get('marks', q.get('marks_awarded', '?'))
            marks_total = q.get('marks_total', '?')
            feedback = teacher_q.get('feedback', q.get('feedback', ''))
            
            table_data.append([
                str(q.get('question_num', '?')),
                status,
                f"{marks}/{marks_total}",
                truncate_text(feedback, 80)
            ])
        
        table = Table(table_data, colWidths=[1*cm, 1.5*cm, 2*cm, 11.5*cm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), PRIMARY_COLOR),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_COLOR),
            ('GRID', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
            ('ALIGN', (0, 0), (2, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('PADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(table)
    
    # Overall feedback
    overall = teacher_feedback.get('overall_feedback') or ai_feedback.get('overall_feedback', '')
    if overall:
        elements.append(Spacer(1, 10))
        elements.append(Paragraph(f"<b>Comments:</b> {overall}", styles['Body_Custom']))
    
    return elements

def truncate_text(text: str, max_length: int) -> str:
    """Truncate text to max length with ellipsis"""
    if not text:
        return ''
    text = str(text)
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + '...'

def get_grade(percentage: float) -> str:
    """Convert percentage to letter grade"""
    if percentage >= 90:
        return 'A+'
    elif percentage >= 85:
        return 'A'
    elif percentage >= 80:
        return 'A-'
    elif percentage >= 75:
        return 'B+'
    elif percentage >= 70:
        return 'B'
    elif percentage >= 65:
        return 'B-'
    elif percentage >= 60:
        return 'C+'
    elif percentage >= 55:
        return 'C'
    elif percentage >= 50:
        return 'C-'
    elif percentage >= 45:
        return 'D'
    else:
        return 'F'

def generate_assignment_pdf(assignment: dict, teacher: dict = None) -> bytes:
    """
    Generate a PDF version of an assignment (for printing/distribution)
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5*cm,
        leftMargin=1.5*cm,
        topMargin=2*cm,
        bottomMargin=2*cm
    )
    
    styles = get_styles()
    story = []
    
    # Title
    story.append(Paragraph(assignment.get('title', 'Assignment'), styles['Title_Custom']))
    
    # Info
    story.append(Paragraph(f"Subject: {assignment.get('subject', 'N/A')}", styles['Body_Custom']))
    story.append(Paragraph(f"Total Marks: {assignment.get('total_marks', 0)}", styles['Body_Custom']))
    
    if assignment.get('due_date'):
        due_date = assignment['due_date']
        if isinstance(due_date, datetime):
            due_date = due_date.strftime('%d %B %Y')
        story.append(Paragraph(f"Due Date: {due_date}", styles['Body_Custom']))
    
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY_COLOR))
    story.append(Spacer(1, 20))
    
    # Instructions
    if assignment.get('instructions'):
        story.append(Paragraph("Instructions:", styles['SubHeading']))
        story.append(Paragraph(assignment['instructions'], styles['Body_Custom']))
        story.append(Spacer(1, 15))
    
    # Questions
    story.append(Paragraph("Questions", styles['Heading_Custom']))
    
    for i, q in enumerate(assignment.get('questions', []), 1):
        question_text = q.get('question', q.get('text', ''))
        marks = q.get('marks', 0)
        
        q_style = ParagraphStyle(
            'Question',
            parent=styles['Body_Custom'],
            fontName='Helvetica-Bold',
            spaceBefore=15
        )
        story.append(Paragraph(f"Q{i}. {question_text} [{marks} marks]", q_style))
        story.append(Spacer(1, 30))  # Space for answer
    
    try:
        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Error generating assignment PDF: {e}")
        return None
