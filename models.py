from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# الأقسام التي يحدد المدير للموظف حق رؤيتها (المستخدمون للأدمن فقط دائمًا)
PERMISSIONS = [
    ('map', 'الخريطة'),
    ('reports', 'التقارير'),
    ('messages', 'الرسائل'),
    ('archive', 'الأرشفة'),
]
PERMISSION_KEYS = [key for key, _ in PERMISSIONS]

REPORT_TYPES = {
    'daily': 'يومي',
    'weekly': 'أسبوعي',
    'monthly': 'شهري',
}


# أنواع العمل في التقرير اليومي
WORK_TYPES = [
    'أعمال غرف',
    'الطبقة الرابطة الأولى (Binder course 1)',
    'الطبقة الرابطة الثانية (Binder course 2)',
    'الطبقة الأساسية الأولى (Basecourse 1)',
    'الطبقة الأساسية الثانية (Basecourse 2)',
    'RC2',
    'MCO',
    'Wearing Course',
]
IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'}


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # الدور: admin (أدمن) أو employee (موظف)
    role = db.Column(db.String(20), nullable=False, default='employee')
    is_active_user = db.Column(db.Boolean, default=True)
    # صلاحيات الموظف: مفاتيح مفصولة بفاصلة مثل "map,reports"
    permissions = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    # مطلوبة من Flask-Login حتى لا يُسمح بتسجيل الدخول للحسابات المعطّلة
    @property
    def is_active(self):
        return self.is_active_user

    @property
    def permission_list(self):
        return [p for p in (self.permissions or '').split(',') if p]

    def set_permissions(self, keys):
        self.permissions = ','.join(k for k in keys if k in PERMISSION_KEYS)

    def can(self, key):
        """الأدمن يملك كل شيء، والموظف يملك فقط ما اختاره له المدير."""
        return self.is_admin or key in self.permission_list


# فئات الأوراق (تُستخدم الآن في الأرشفة)
DOCUMENT_CATEGORIES = ['اختبار', 'رسالة', 'أخرى']
SIDES = ['يمين', 'يسار']
WORK_STATUSES = ['قيد التنفيذ', 'منتهي', 'متوقف']


class Document(db.Model):
    """سجل الأرشيف (الجدول القديم للأوراق، بياناته القديمة محفوظة)"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # اختبار / رسالة / أخرى
    description = db.Column(db.Text)

    stored_file_name = db.Column(db.String(300))
    original_file_name = db.Column(db.String(300))

    side = db.Column(db.String(10))       # يمين / يسار / فارغ = عام
    km_point = db.Column(db.Float)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    uploaded_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def has_file(self):
        return bool(self.stored_file_name)


class WorkRecord(db.Model):
    """حصر الأعمال على الطريق الساحلي (تظهر على الخريطة وتُلخَّص في التقارير)"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    side = db.Column(db.String(10), nullable=False)     # يمين / يسار
    km_start = db.Column(db.Float, nullable=False)
    km_end = db.Column(db.Float)

    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)

    status = db.Column(db.String(30), default='قيد التنفيذ')
    work_date = db.Column(db.Date)

    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Report(db.Model):
    """تقرير يومي / أسبوعي / شهري"""
    id = db.Column(db.Integer, primary_key=True)
    report_type = db.Column(db.String(20), nullable=False)  # daily / weekly / monthly
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)            # تقرير المهندس
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    # حقول التقرير اليومي
    reporter_name = db.Column(db.String(150))
    work_type = db.Column(db.String(100))
    station_from = db.Column(db.String(50))
    station_to = db.Column(db.String(50))
    notes = db.Column(db.Text)

    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attachments = db.relationship('ReportAttachment', backref='report',
                                  cascade='all, delete-orphan',
                                  order_by='ReportAttachment.id')

    @property
    def type_label(self):
        return REPORT_TYPES.get(self.report_type, self.report_type)


class ReportAttachment(db.Model):
    """مرفق (صورة أو ملف) تابع لتقرير"""
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('report.id'), nullable=False)
    stored_file_name = db.Column(db.String(300), nullable=False)
    original_file_name = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_image(self):
        ext = self.original_file_name.rsplit('.', 1)[-1].lower()
        return ext in IMAGE_EXTENSIONS


class Message(db.Model):
    """رسالة داخلية بين المستخدمين"""
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sender = db.relationship('User', foreign_keys=[sender_id])
    recipient = db.relationship('User', foreign_keys=[recipient_id])

    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    deleted_by_sender = db.Column(db.Boolean, default=False)
    deleted_by_recipient = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
