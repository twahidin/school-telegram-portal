import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, MediaIoBaseDownload
import io

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.file']


def get_service_account_email() -> str:
    """
    Get the service account email address that needs to be granted
    access to Google Drive folders.
    
    Returns:
        The service account email or None if not configured
    """
    try:
        # Try file-based credentials
        creds_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
        if creds_file and os.path.exists(creds_file):
            import json
            with open(creds_file, 'r') as f:
                creds_info = json.load(f)
                return creds_info.get('client_email')
        
        # Try JSON credentials from environment
        creds_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
        if creds_json:
            import json
            creds_info = json.loads(creds_json)
            return creds_info.get('client_email')
        
        return None
        
    except Exception as e:
        logger.error(f"Error getting service account email: {e}")
        return None


def is_drive_configured() -> bool:
    """Check if Google Drive is configured with service account credentials"""
    creds_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
    creds_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    
    if creds_file and os.path.exists(creds_file):
        return True
    if creds_json:
        return True
    return False


def get_drive_service():
    """Get Google Drive service using service account credentials"""
    try:
        creds_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
        if creds_file and os.path.exists(creds_file):
            credentials = service_account.Credentials.from_service_account_file(
                creds_file, scopes=SCOPES
            )
            return build('drive', 'v3', credentials=credentials)
        
        # Try JSON credentials from environment
        import json
        creds_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
        if creds_json:
            creds_info = json.loads(creds_json)
            credentials = service_account.Credentials.from_service_account_info(
                creds_info, scopes=SCOPES
            )
            return build('drive', 'v3', credentials=credentials)
        
        logger.warning("No Google Drive credentials configured")
        return None
        
    except Exception as e:
        logger.error(f"Error creating Drive service: {e}")
        return None

def get_teacher_drive_manager(teacher):
    """Get a drive manager configured for a specific teacher's folder"""
    service = get_drive_service()
    if not service:
        return None
    
    folder_id = teacher.get('google_drive_folder_id') if teacher else None
    return DriveManager(service, folder_id)

class DriveManager:
    def __init__(self, service, folder_id=None):
        self.service = service
        self.folder_id = folder_id
    
    def create_folder(self, name: str, parent_id: str = None) -> str:
        """Create a folder in Drive"""
        try:
            file_metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            if parent_id or self.folder_id:
                file_metadata['parents'] = [parent_id or self.folder_id]
            
            file = self.service.files().create(
                body=file_metadata, 
                fields='id'
            ).execute()
            
            return file.get('id')
        except Exception as e:
            logger.error(f"Error creating folder: {e}")
            return None
    
    def upload_file(self, file_path: str, name: str = None, folder_id: str = None) -> dict:
        """Upload a file to Drive"""
        try:
            if not name:
                name = os.path.basename(file_path)
            
            file_metadata = {'name': name}
            if folder_id or self.folder_id:
                file_metadata['parents'] = [folder_id or self.folder_id]
            
            # Determine mime type
            mime_type = 'application/octet-stream'
            if file_path.endswith('.pdf'):
                mime_type = 'application/pdf'
            elif file_path.endswith('.txt'):
                mime_type = 'text/plain'
            elif file_path.endswith('.json'):
                mime_type = 'application/json'
            
            media = MediaFileUpload(file_path, mimetype=mime_type)
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()
            
            return {
                'id': file.get('id'),
                'link': file.get('webViewLink')
            }
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            return None
    
    def upload_content(self, content: bytes, name: str, mime_type: str = 'application/pdf', folder_id: str = None) -> dict:
        """Upload content directly to Drive"""
        try:
            file_metadata = {'name': name}
            if folder_id or self.folder_id:
                file_metadata['parents'] = [folder_id or self.folder_id]
            
            media = MediaIoBaseUpload(
                io.BytesIO(content),
                mimetype=mime_type,
                resumable=True
            )
            
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()
            
            return {
                'id': file.get('id'),
                'link': file.get('webViewLink')
            }
        except Exception as e:
            logger.error(f"Error uploading content: {e}")
            return None
    
    def delete_file(self, file_id: str) -> bool:
        """Delete a file from Drive"""
        try:
            self.service.files().delete(fileId=file_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            return False
    
    def list_files(self, folder_id: str = None, mime_types: list = None) -> list:
        """List files in a folder"""
        try:
            query = f"'{folder_id or self.folder_id}' in parents and trashed=false"
            
            # Filter by mime types if provided
            if mime_types:
                mime_query = ' or '.join([f"mimeType='{mt}'" for mt in mime_types])
                query += f" and ({mime_query})"
            
            results = self.service.files().list(
                q=query,
                fields="files(id, name, mimeType, size, modifiedTime)",
                orderBy="name"
            ).execute()
            
            files = results.get('files', [])
            return files
        except Exception as e:
            logger.error(f"Error listing files: {e}")
            return []
    
    def get_file_content(self, file_id: str, export_as_pdf: bool = False) -> bytes:
        """Get file content, optionally exporting Google Docs/Sheets as PDF"""
        try:
            file_metadata = self.service.files().get(fileId=file_id).execute()
            mime_type = file_metadata.get('mimeType', '')
            
            # If it's a Google Doc/Sheet and we want PDF, export it
            if export_as_pdf:
                if mime_type == 'application/vnd.google-apps.document':
                    request = self.service.files().export_media(fileId=file_id, mimeType='application/pdf')
                elif mime_type == 'application/vnd.google-apps.spreadsheet':
                    request = self.service.files().export_media(fileId=file_id, mimeType='application/pdf')
                elif mime_type == 'application/vnd.google-apps.presentation':
                    request = self.service.files().export_media(fileId=file_id, mimeType='application/pdf')
                else:
                    # Regular file download
                    request = self.service.files().get_media(fileId=file_id)
            else:
                # Regular file download
                request = self.service.files().get_media(fileId=file_id)
            
            import io
            file_content = io.BytesIO()
            downloader = MediaIoBaseDownload(file_content, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            file_content.seek(0)
            return file_content.read()
        except Exception as e:
            logger.error(f"Error getting file content: {e}")
            return None

def upload_assignment_file(file_path: str, assignment: dict, teacher: dict) -> dict:
    """
    Upload an assignment-related file to the teacher's Drive folder
    """
    manager = get_teacher_drive_manager(teacher)
    if not manager:
        logger.warning("Drive manager not available")
        return None
    
    # Create assignment folder if it doesn't exist
    folder_name = f"Assignment_{assignment.get('assignment_id', 'Unknown')}"
    folder_id = manager.create_folder(folder_name)
    
    if folder_id:
        return manager.upload_file(file_path, folder_id=folder_id)
    
    return manager.upload_file(file_path)


def create_assignment_folder_structure(teacher: dict, assignment_title: str, assignment_id: str) -> dict:
    """
    Create the folder structure for an assignment in Google Drive.
    
    Structure:
    Teacher's Folder/
    └── [Assignment Title]/
        ├── Question Papers/
        └── Submissions/
    
    Returns:
        dict with folder IDs: {
            'assignment_folder_id': '...',
            'question_papers_folder_id': '...',
            'submissions_folder_id': '...'
        }
        or None if failed
    """
    manager = get_teacher_drive_manager(teacher)
    if not manager:
        logger.warning("Drive manager not available for folder creation")
        return None
    
    try:
        # Sanitize folder name
        safe_title = "".join(c for c in assignment_title if c.isalnum() or c in (' ', '-', '_')).strip()
        folder_name = f"{safe_title} ({assignment_id})"
        
        # Create main assignment folder
        assignment_folder_id = manager.create_folder(folder_name)
        if not assignment_folder_id:
            logger.error("Failed to create assignment folder")
            return None
        
        # Create Question Papers subfolder
        question_papers_folder_id = manager.create_folder("Question Papers", parent_id=assignment_folder_id)
        
        # Create Submissions subfolder
        submissions_folder_id = manager.create_folder("Submissions", parent_id=assignment_folder_id)
        
        logger.info(f"Created folder structure for assignment {assignment_id}")
        
        return {
            'assignment_folder_id': assignment_folder_id,
            'question_papers_folder_id': question_papers_folder_id,
            'submissions_folder_id': submissions_folder_id
        }
        
    except Exception as e:
        logger.error(f"Error creating assignment folder structure: {e}")
        return None


def upload_question_papers(teacher: dict, question_papers_folder_id: str, 
                           question_paper_content: bytes, question_paper_name: str,
                           answer_key_content: bytes, answer_key_name: str) -> dict:
    """
    Upload question paper and answer key to the Question Papers folder.
    
    Returns:
        dict with file info or None if failed
    """
    manager = get_teacher_drive_manager(teacher)
    if not manager:
        logger.warning("Drive manager not available")
        return None
    
    try:
        result = {}
        
        # Upload question paper
        if question_paper_content:
            qp_result = manager.upload_content(
                question_paper_content,
                question_paper_name or "Question_Paper.pdf",
                mime_type='application/pdf',
                folder_id=question_papers_folder_id
            )
            if qp_result:
                result['question_paper'] = qp_result
        
        # Upload answer key
        if answer_key_content:
            ak_result = manager.upload_content(
                answer_key_content,
                answer_key_name or "Answer_Key.pdf",
                mime_type='application/pdf',
                folder_id=question_papers_folder_id
            )
            if ak_result:
                result['answer_key'] = ak_result
        
        return result if result else None
        
    except Exception as e:
        logger.error(f"Error uploading question papers: {e}")
        return None


def upload_student_submission(teacher: dict, submissions_folder_id: str,
                              submission_content: bytes, filename: str,
                              student_name: str = None, student_id: str = None) -> dict:
    """
    Upload a student submission to the Submissions folder.
    
    Args:
        teacher: Teacher document
        submissions_folder_id: The ID of the Submissions folder
        submission_content: PDF content bytes
        filename: Original filename
        student_name: Student's name for folder organization
        student_id: Student ID
    
    Returns:
        dict with file info or None if failed
    """
    manager = get_teacher_drive_manager(teacher)
    if not manager:
        logger.warning("Drive manager not available")
        return None
    
    try:
        # Create a meaningful filename
        if student_name and student_id:
            safe_name = "".join(c for c in student_name if c.isalnum() or c in (' ', '-', '_')).strip()
            upload_name = f"{student_id}_{safe_name}_{filename}"
        elif student_id:
            upload_name = f"{student_id}_{filename}"
        else:
            upload_name = filename
        
        result = manager.upload_content(
            submission_content,
            upload_name,
            mime_type='application/pdf',
            folder_id=submissions_folder_id
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Error uploading student submission: {e}")
        return None
