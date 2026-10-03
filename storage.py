"""طبقة التخزين: S3 / Cloudflare R2 عند ضبط متغيرات S3_*، وإلا مجلد محلي (static/uploads)."""
import os
import shutil
from urllib.parse import quote

_state = {'app': None, 'client': None}


def _bucket():
    return os.environ.get('S3_BUCKET')


def is_cloud():
    return bool(_bucket() and os.environ.get('S3_ACCESS_KEY_ID')
                and os.environ.get('S3_SECRET_ACCESS_KEY'))


def init_app(app):
    _state['app'] = app


def local_folder():
    return _state['app'].config['UPLOAD_FOLDER']


def _client():
    if _state['client'] is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _state['client'] = boto3.client(
            's3',
            endpoint_url=os.environ.get('S3_ENDPOINT_URL') or None,
            aws_access_key_id=os.environ.get('S3_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('S3_SECRET_ACCESS_KEY'),
            region_name=os.environ.get('S3_REGION') or 'auto',
            config=BotoConfig(signature_version='s3v4'),
        )
    return _state['client']


def _local_path(name):
    base = os.path.abspath(local_folder())
    path = os.path.abspath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise ValueError('مسار ملف غير صالح')
    return path


def save_file(stream, name, content_type=None):
    """يحفظ الملف تحت الاسم/المفتاح name (قد يحتوي على مجلد مثل stations/x.kmz)."""
    if is_cloud():
        extra = {'ContentType': content_type} if content_type else {}
        _client().upload_fileobj(stream, _bucket(), name, ExtraArgs=extra)
        return
    path = _local_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as out:
        shutil.copyfileobj(stream, out)


def delete_file(name):
    try:
        if is_cloud():
            _client().delete_object(Bucket=_bucket(), Key=name)
        else:
            path = _local_path(name)
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        if _state['app']:
            _state['app'].logger.exception('تعذر حذف الملف %s', name)


def read_bytes(name):
    """يعيد محتوى الملف bytes أو None إن لم يكن موجودًا."""
    try:
        if is_cloud():
            return _client().get_object(Bucket=_bucket(), Key=name)['Body'].read()
        path = _local_path(name)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            return f.read()
    except Exception as exc:
        code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
        if code in ('NoSuchKey', '404', 'NotFound') or isinstance(exc, FileNotFoundError):
            return None
        if _state['app']:
            _state['app'].logger.exception('تعذر قراءة الملف %s', name)
        return None


def download_url(name, original_name=None, expires=300):
    """رابط تحميل مؤقت (سحابي فقط)."""
    params = {'Bucket': _bucket(), 'Key': name}
    if original_name:
        params['ResponseContentDisposition'] = \
            "attachment; filename*=UTF-8''" + quote(original_name)
    return _client().generate_presigned_url('get_object', Params=params, ExpiresIn=expires)
