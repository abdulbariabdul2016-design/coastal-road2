import os

basedir = os.path.abspath(os.path.dirname(__file__))


def _build_database_uri():
    """يبني رابط قاعدة البيانات بشكل يعمل مع Neon (PostgreSQL) وSQLite محليًا."""
    raw_url = os.environ.get('DATABASE_URL')

    if not raw_url:
        # لا يوجد DATABASE_URL (تشغيل محلي) => استخدم SQLite كملف محلي
        return 'sqlite:///' + os.path.join(basedir, 'app.db')

    # Neon (وبعض الخدمات الأخرى) تعطي الرابط بصيغة postgres:// القديمة،
    # بينما SQLAlchemy الحديث يتطلب postgresql://
    if raw_url.startswith('postgres://'):
        raw_url = raw_url.replace('postgres://', 'postgresql://', 1)

    # Neon يتطلب اتصال SSL؛ نضيف sslmode=require تلقائيًا إذا لم يكن موجودًا
    if 'sslmode' not in raw_url:
        separator = '&' if '?' in raw_url else '?'
        raw_url = f'{raw_url}{separator}sslmode=require'

    return raw_url


class Config:
    # مفتاح سري لتشفير الجلسات - غيّره عند النشر الفعلي على الإنترنت
    SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key-please')

    # قاعدة البيانات: SQLite محليًا افتراضيًا، أو PostgreSQL (Neon) عبر متغير البيئة DATABASE_URL
    SQLALCHEMY_DATABASE_URI = _build_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # إعدادات اتصال إضافية مفيدة مع قواعد بيانات سحابية مثل Neon
    # (تتجاهَل تلقائيًا مع SQLite لأنها معاملات خاصة بمحرك pool)
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,   # يتأكد أن الاتصال ما زال حيًا قبل استخدامه (يفيد مع Neon الذي يوقف الاتصالات الخاملة)
        'pool_recycle': 300,
    } if os.environ.get('DATABASE_URL') else {}

    # مجلد رفع الملفات
    UPLOAD_FOLDER = os.path.join(basedir, 'static', 'uploads')

    # الحد الأقصى لحجم الملف المرفوع (25 ميجابايت)
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024

    # الامتدادات المسموح رفعها
    ALLOWED_EXTENSIONS = {
        'pdf', 'doc', 'docx', 'docm', 'xls', 'xlsx', 'xlsm', 'csv', 'txt',
        'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tif', 'tiff'
    }

    # طول الطريق الساحلي بالكيلومترات (يمكن تعديله)
    ROAD_LENGTH_KM = 100
