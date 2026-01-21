from pymongo import MongoClient
from datetime import datetime
import os

class Database:
    def __init__(self):
        self.client = None
        self.db = None
    
    def init_app(self, app):
        # Support Railway's MONGO_URL or standard MONGODB_URI
        mongodb_uri = (
            app.config.get('MONGODB_URI') or 
            os.getenv('MONGO_URL') or 
            os.getenv('MONGODB_URI')
        )
        if not mongodb_uri:
            raise ValueError("No MongoDB connection string found. Set MONGODB_URI or MONGO_URL.")
        
        self.client = MongoClient(mongodb_uri)
        # Get database name from URI or use default
        db_name = app.config.get('MONGODB_DB', 'school_portal')
        self.db = self.client.get_database(db_name)
        self._create_indexes()
    
    def _create_indexes(self):
        self.db.students.create_index('student_id', unique=True)
        self.db.students.create_index('class')
        self.db.teachers.create_index('teacher_id', unique=True)
        self.db.teachers.create_index('telegram_id', unique=True, sparse=True)
        self.db.messages.create_index([('student_id', 1), ('teacher_id', 1), ('timestamp', -1)])
        self.db.messages.create_index([('timestamp', -1)])
        self.db.classes.create_index('class_id', unique=True)
        self.db.teaching_groups.create_index('group_id', unique=True)
        self.db.teaching_groups.create_index([('class_id', 1), ('teacher_id', 1)])
        self.db.assignments.create_index([('teacher_id', 1), ('subject', 1)])
        self.db.assignments.create_index('assignment_id', unique=True)
        self.db.submissions.create_index([('student_id', 1), ('assignment_id', 1)])
        self.db.submissions.create_index([('assignment_id', 1), ('status', 1)])
        self.db.submissions.create_index('submission_id', unique=True)

db = Database()

class Student:
    @staticmethod
    def find_one(query):
        return db.db.students.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.students.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.students.insert_one(document).inserted_id
    
    @staticmethod
    def update_one(query, update):
        return db.db.students.update_one(query, update)
    
    @staticmethod
    def update_many(query, update):
        return db.db.students.update_many(query, update)
    
    @staticmethod
    def count(query):
        return db.db.students.count_documents(query)

class Teacher:
    @staticmethod
    def find_one(query):
        return db.db.teachers.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.teachers.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.teachers.insert_one(document).inserted_id
    
    @staticmethod
    def update_one(query, update):
        return db.db.teachers.update_one(query, update)
    
    @staticmethod
    def count(query):
        return db.db.teachers.count_documents(query)

class Message:
    @staticmethod
    def find_one(query):
        return db.db.messages.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.messages.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.messages.insert_one(document).inserted_id
    
    @staticmethod
    def update_many(query, update):
        return db.db.messages.update_many(query, update)
    
    @staticmethod
    def count(query):
        return db.db.messages.count_documents(query)
    
    @staticmethod
    def distinct(field, query):
        return db.db.messages.distinct(field, query)

class Class:
    @staticmethod
    def find_one(query):
        return db.db.classes.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.classes.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.classes.insert_one(document).inserted_id
    
    @staticmethod
    def update_one(query, update, upsert=False):
        return db.db.classes.update_one(query, update, upsert=upsert)
    
    @staticmethod
    def count(query):
        return db.db.classes.count_documents(query)

class TeachingGroup:
    @staticmethod
    def find_one(query):
        return db.db.teaching_groups.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.teaching_groups.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.teaching_groups.insert_one(document).inserted_id
    
    @staticmethod
    def update_one(query, update):
        return db.db.teaching_groups.update_one(query, update)
    
    @staticmethod
    def delete_one(query):
        return db.db.teaching_groups.delete_one(query)
    
    @staticmethod
    def count(query):
        return db.db.teaching_groups.count_documents(query)

class Assignment:
    @staticmethod
    def find_one(query):
        return db.db.assignments.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.assignments.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.assignments.insert_one(document).inserted_id
    
    @staticmethod
    def update_one(query, update):
        return db.db.assignments.update_one(query, update)
    
    @staticmethod
    def count(query):
        return db.db.assignments.count_documents(query)

class Submission:
    @staticmethod
    def find_one(query):
        return db.db.submissions.find_one(query)
    
    @staticmethod
    def find(query):
        return db.db.submissions.find(query)
    
    @staticmethod
    def insert_one(document):
        return db.db.submissions.insert_one(document).inserted_id
    
    @staticmethod
    def update_one(query, update):
        return db.db.submissions.update_one(query, update)
    
    @staticmethod
    def count(query):
        return db.db.submissions.count_documents(query)
