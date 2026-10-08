import os
import uuid
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from functools import wraps

from urllib.parse import quote

from flask import (
    Flask, render_template, redirect, url_for, flash, request,
    send_from_directory, abort, jsonify, Response
)
from sqlalchemy import inspect, text
from werkzeug.exceptions import RequestEntityTooLarge
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename

from config import Config
import storage
from models import (
    db, User, Document, WorkRecord, Report, ReportAttachment, Message, OfficialLetter,
    DOCUMENT_CATEGORIES, SIDES, WORK_STATUSES, WORK_TYPES,
    PERMISSIONS, PERMISSION_KEYS, REPORT_TYPES
)


# بيانات الدخول الثابتة للأدمن. يُفضَّل ضبطها من متغيرات البيئة في Render
# (ADMIN_USERNAME / ADMIN_PASSWORD) بدل تركها هنا، خاصة إذا كان مستودع GitHub عامًا.
DEFAULT_ADMIN_USERNAME = 'abdulbari'
DEFAULT_ADMIN_PASSWORD = 'abdo1996'
DEFAULT_ADMIN_FULL_NAME = 'المدير العام'


def fixed_admin_credentials():
    # القيم ثابتة من الكود فقط، وتُتجاهل متغيرات البيئة في Render.
    return (DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_FULL_NAME)



def ensure_schema():
    """يضيف الأعمدة الجديدة للجداول الموجودة (db.create_all لا يعدّل الجداول القديمة)."""
    insp = inspect(db.engine)
    wanted = {
        'user': [('permissions', 'TEXT')],
        'report': [('reporter_name', 'VARCHAR(150)'), ('work_type', 'VARCHAR(100)'),
                   ('station_from', 'VARCHAR(50)'), ('station_to', 'VARCHAR(50)'),
                   ('notes', 'TEXT')],
    }
    for table, columns in wanted.items():
        if not insp.has_table(table):
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name not in existing:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}'))


def file_extension(filename):
    """امتداد الملف من الاسم الأصلي (secure_filename يحذف الحروف العربية فلا يصلح لذلك)."""
    return os.path.splitext(filename or '')[1].lstrip('.').lower()


def display_name(filename):
    """اسم الملف للعرض: يحافظ على العربية ويزيل أي مسار."""
    return os.path.basename((filename or '').replace('\\', '/')).strip()[:200] or 'file'


def period_bounds(report_type, d):
    """بداية ونهاية فترة التقرير: يومي / أسبوعي (من السبت إلى الجمعة) / شهري."""
    if report_type == 'daily':
        return d, d
    if report_type == 'weekly':
        start = d - timedelta(days=(d.weekday() - 5) % 7)
        return start, start + timedelta(days=6)
    first = d.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return first, next_month - timedelta(days=1)


def works_summary(start, end):
    """ملخص أعمال الحصر التي تاريخها داخل الفترة."""
    works = WorkRecord.query.filter(
        WorkRecord.work_date >= start, WorkRecord.work_date <= end
    ).all()
    return {
        'total': len(works),
        'done': sum(1 for w in works if w.status == 'منتهي'),
        'in_progress': sum(1 for w in works if w.status == 'قيد التنفيذ'),
        'stopped': sum(1 for w in works if w.status == 'متوقف'),
    }


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    db.init_app(app)
    storage.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = 'login'
    login_manager.login_message = 'يرجى تسجيل الدخول للمتابعة'
    login_manager.login_message_category = 'warning'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # تهيئة قاعدة البيانات تلقائيًا عند الإقلاع (ضروري مع gunicorn)
    with app.app_context():
        db.create_all()
        ensure_schema()

        # حساب الأدمن الثابت: يُنشأ إن لم يكن موجودًا ويُعاد ضبط كلمة مروره عند كل تشغيل
        fixed_username, fixed_password, fixed_full_name = fixed_admin_credentials()
        fixed_admin = User.query.filter_by(username=fixed_username).first()
        if fixed_admin is None:
            fixed_admin = User(full_name=fixed_full_name, username=fixed_username,
                               role='admin')
            db.session.add(fixed_admin)
        fixed_admin.role = 'admin'
        fixed_admin.is_active_user = True
        fixed_admin.set_password(fixed_password)
        db.session.commit()

    # ------------------------------------------------------------------
    # أدوات مساعدة
    # ------------------------------------------------------------------
    def allowed_file(filename):
        return file_extension(filename) in app.config['ALLOWED_EXTENSIONS']

    def read_kml_map_data(file_path):
        """يقرأ نقاط المحطات وخطوط الطريق من ملف KML أو KMZ."""
        if file_path.lower().endswith('.kmz'):
            with zipfile.ZipFile(file_path) as archive:
                kml_names = [name for name in archive.namelist()
                             if name.lower().endswith('.kml')]
                if not kml_names:
                    raise ValueError('ملف KMZ لا يحتوي على ملف KML')
                kml_content = archive.read(kml_names[0])
        else:
            with open(file_path, 'rb') as kml_file:
                kml_content = kml_file.read()

        root = ET.fromstring(kml_content)
        points = []
        lines = []
        for placemark in root.iter():
            if placemark.tag.rsplit('}', 1)[-1] != 'Placemark':
                continue

            name = ''
            description = ''
            point_coordinates = None
            line_coordinates = []
            for element in placemark.iter():
                tag = element.tag.rsplit('}', 1)[-1]
                text = (element.text or '').strip()
                if tag == 'name' and text and not name:
                    name = text
                elif tag == 'description' and text:
                    description = text
                elif tag == 'Point':
                    coordinates_element = next(
                        (child for child in element.iter()
                         if child.tag.rsplit('}', 1)[-1] == 'coordinates'),
                        None
                    )
                    if coordinates_element is not None and coordinates_element.text:
                        point_coordinates = coordinates_element.text.strip().split()[0]
                elif tag == 'LineString':
                    coordinates_element = next(
                        (child for child in element.iter()
                         if child.tag.rsplit('}', 1)[-1] == 'coordinates'),
                        None
                    )
                    if coordinates_element is not None and coordinates_element.text:
                        line_coordinates.extend(coordinates_element.text.split())

            if point_coordinates:
                values = point_coordinates.split(',')
                if len(values) >= 2:
                    try:
                        points.append({
                            'name': name or 'محطة بدون اسم',
                            'description': description,
                            'lat': float(values[1]),
                            'lng': float(values[0])
                        })
                    except ValueError:
                        continue
            if line_coordinates:
                path = []
                for coordinate in line_coordinates:
                    values = coordinate.split(',')
                    if len(values) >= 2:
                        try:
                            path.append([float(values[1]), float(values[0])])
                        except ValueError:
                            continue
                if len(path) >= 2:
                    lines.append(path)
        return {'stations': points, 'lines': lines}

    @app.errorhandler(RequestEntityTooLarge)
    def handle_file_too_large(error):
        flash('حجم الملفات أكبر من الحد المسموح (25 ميجابايت)', 'danger')
        return redirect(request.referrer or url_for('dashboard'))

    def admin_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.is_admin:
                flash('هذه الصفحة متاحة للأدمن فقط', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapper

    def permission_required(key):
        """يسمح بالدخول للأدمن، أو للموظف الذي منحه المدير هذه الصلاحية."""
        def decorator(f):
            @wraps(f)
            def wrapper(*args, **kwargs):
                if not current_user.is_authenticated:
                    return login_manager.unauthorized()
                if not current_user.can(key):
                    flash('ليست لديك صلاحية للوصول إلى هذه الصفحة', 'danger')
                    return redirect(url_for('dashboard'))
                return f(*args, **kwargs)
            return wrapper
        return decorator

    @app.context_processor
    def inject_globals():
        unread = 0
        if current_user.is_authenticated and current_user.can('messages'):
            unread = Message.query.filter_by(
                recipient_id=current_user.id, is_read=False,
                deleted_by_recipient=False).count()
        return dict(PERMISSIONS=PERMISSIONS, REPORT_TYPES=REPORT_TYPES,
                    unread_count=unread)

    # ------------------------------------------------------------------
    # المصادقة
    # ------------------------------------------------------------------
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            user = User.query.filter_by(username=username).first()

            if user and user.check_password(password) and user.is_active_user:
                login_user(user)
                flash(f'مرحبًا بك {user.full_name}', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            else:
                flash('اسم المستخدم أو كلمة المرور غير صحيحة، أو الحساب معطّل', 'danger')

        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        flash('تم تسجيل الخروج بنجاح', 'info')
        return redirect(url_for('login'))

    # ------------------------------------------------------------------
    # الصفحة الرئيسية: توجّه المستخدم لأول قسم مسموح له
    # ------------------------------------------------------------------
    @app.route('/')
    @login_required
    def dashboard():
        for key, endpoint in (('map', 'map_view'), ('reports', 'reports_list'),
                              ('messages', 'messages_list'), ('archive', 'archive_list')):
            if current_user.can(key):
                return redirect(url_for(endpoint))
        return render_template('no_access.html')

    # روابط قديمة تحوّل للأقسام الجديدة (حتى لا ينكسر أي رابط قديم داخل الصفحات)
    @app.route('/documents')
    @login_required
    def documents_list():
        return redirect(url_for('archive_list'))

    @app.route('/documents/new')
    @login_required
    def document_new():
        return redirect(url_for('archive_new'))

    # ------------------------------------------------------------------
    # التقارير (يومية / أسبوعية / شهرية)
    # ------------------------------------------------------------------
    def can_access_report(report):
        return current_user.is_admin or report.created_by_id == current_user.id

    @app.route('/reports')
    @login_required
    @permission_required('reports')
    def reports_list():
        rtype = request.args.get('type', 'daily')
        if rtype not in REPORT_TYPES:
            rtype = 'daily'
        query = Report.query.filter_by(report_type=rtype)
        if not current_user.is_admin:
            query = query.filter_by(created_by_id=current_user.id)
        reports = query.order_by(Report.period_start.desc(),
                                 Report.created_at.desc()).all()
        start, end = period_bounds(rtype, date.today())
        return render_template('reports.html', reports=reports, rtype=rtype,
                               current_start=start, current_end=end,
                               current_summary=works_summary(start, end),
                               work_types=WORK_TYPES, today=date.today().isoformat())

    @app.route('/reports/new', methods=['GET', 'POST'])
    @login_required
    @permission_required('reports')
    def report_new():
        rtype = request.values.get('type', 'daily')
        if rtype not in REPORT_TYPES:
            rtype = 'daily'
        back = url_for('reports_list', type=rtype)

        if request.method == 'GET':
            if rtype == 'daily':  # التقرير اليومي يُكتب من النافذة داخل صفحة التقارير
                return redirect(back)
            return render_template('report_form.html', rtype=rtype,
                                   today=date.today().isoformat())

        try:
            chosen = datetime.strptime(request.form.get('report_date', ''), '%Y-%m-%d').date()
        except ValueError:
            chosen = date.today()
        files = [f for f in request.files.getlist('attachments') if f and f.filename]

        report = Report(report_type=rtype, created_by_id=current_user.id)
        content = request.form.get('content', '').strip()

        if rtype == 'daily':
            reporter_name = request.form.get('reporter_name', '').strip()
            work_type = request.form.get('work_type', '')
            if not reporter_name or work_type not in WORK_TYPES or not content:
                flash('أدخل اسم الشخص واختر نوع العمل واكتب تقرير المهندس', 'danger')
                return redirect(back)
            report.reporter_name = reporter_name
            report.work_type = work_type
            report.station_from = request.form.get('station_from', '').strip() or None
            report.station_to = request.form.get('station_to', '').strip() or None
            report.notes = request.form.get('notes', '').strip() or None
            report.title = f'{work_type} - {reporter_name}'
        else:
            title = request.form.get('title', '').strip()
            if not title or not content:
                flash('يرجى كتابة عنوان التقرير ومحتواه', 'danger')
                return redirect(url_for('report_new', type=rtype))
            report.title = title
        report.content = content

        for f in files:
            if not allowed_file(f.filename):
                flash(f'نوع الملف غير مسموح به: {display_name(f.filename)}. '
                      'المسموح: صور وPDF وWord وExcel.', 'danger')
                return redirect(back)

        report.period_start, report.period_end = period_bounds(rtype, chosen)

        saved_names = []
        try:
            db.session.add(report)
            for f in files:
                stored = f'{uuid.uuid4().hex}.{file_extension(f.filename)}'
                storage.save_file(f.stream, stored, f.mimetype)
                saved_names.append(stored)
                report.attachments.append(ReportAttachment(
                    stored_file_name=stored,
                    original_file_name=display_name(f.filename)))
            db.session.commit()
        except Exception:
            db.session.rollback()
            for name in saved_names:
                storage.delete_file(name)
            app.logger.exception('فشل حفظ التقرير أو مرفقاته')
            flash('تعذر حفظ التقرير أو رفع المرفقات. حاول مرة أخرى.', 'danger')
            return redirect(back)

        flash('تم حفظ التقرير', 'success')
        return redirect(back)

    @app.route('/reports/<int:report_id>')
    @login_required
    @permission_required('reports')
    def report_view(report_id):
        report = Report.query.get_or_404(report_id)
        if not can_access_report(report):
            abort(403)
        summary = works_summary(report.period_start, report.period_end)
        return render_template('report_view.html', report=report, summary=summary)

    @app.route('/reports/attachment/<int:att_id>')
    @login_required
    @permission_required('reports')
    def report_attachment(att_id):
        att = ReportAttachment.query.get_or_404(att_id)
        if not can_access_report(att.report):
            abort(403)
        if storage.is_cloud():
            return redirect(storage.download_url(att.stored_file_name, att.original_file_name))
        return send_from_directory(storage.local_folder(), att.stored_file_name,
                                   as_attachment=True, download_name=att.original_file_name)

    @app.route('/reports/<int:report_id>/delete', methods=['POST'])
    @login_required
    @permission_required('reports')
    def report_delete(report_id):
        report = Report.query.get_or_404(report_id)
        if not can_access_report(report):
            abort(403)
        rtype = report.report_type
        for att in report.attachments:
            storage.delete_file(att.stored_file_name)
        db.session.delete(report)
        db.session.commit()
        flash('تم حذف التقرير', 'info')
        return redirect(url_for('reports_list', type=rtype))

    # ------------------------------------------------------------------
    # الرسائل
    # ------------------------------------------------------------------
    @app.route('/messages')
    @login_required
    @permission_required('messages')
    def messages_list():
        box = request.args.get('box', 'inbox')
        if box == 'sent':
            msgs = Message.query.filter_by(sender_id=current_user.id,
                                           deleted_by_sender=False)
        else:
            box = 'inbox'
            msgs = Message.query.filter_by(recipient_id=current_user.id,
                                           deleted_by_recipient=False)
        msgs = msgs.order_by(Message.created_at.desc()).all()
        return render_template('messages.html', messages=msgs, box=box)

    @app.route('/messages/new', methods=['GET', 'POST'])
    @login_required
    @permission_required('messages')
    def message_new():
        recipients = User.query.filter(
            User.is_active_user.is_(True), User.id != current_user.id
        ).order_by(User.full_name).all()

        if request.method == 'POST':
            recipient = request.form.get('recipient', '')
            subject = request.form.get('subject', '').strip()
            body = request.form.get('body', '').strip()

            if recipient == 'all' and current_user.is_admin:
                targets = recipients
            else:
                targets = [u for u in recipients if str(u.id) == recipient]

            if not targets or not subject or not body:
                flash('اختر المستلم واكتب الموضوع ونص الرسالة', 'danger')
                return redirect(url_for('message_new'))

            for target in targets:
                db.session.add(Message(sender_id=current_user.id,
                                       recipient_id=target.id,
                                       subject=subject, body=body))
            db.session.commit()
            flash('تم إرسال الرسالة', 'success')
            return redirect(url_for('messages_list', box='sent'))

        return render_template('message_form.html', recipients=recipients,
                               prefill_to=request.args.get('to', ''),
                               prefill_subject=request.args.get('subject', ''))

    @app.route('/messages/<int:msg_id>')
    @login_required
    @permission_required('messages')
    def message_view(msg_id):
        msg = Message.query.get_or_404(msg_id)
        is_recipient = msg.recipient_id == current_user.id and not msg.deleted_by_recipient
        is_sender = msg.sender_id == current_user.id and not msg.deleted_by_sender
        if not (is_recipient or is_sender):
            abort(404)
        if is_recipient and not msg.is_read:
            msg.is_read = True
            db.session.commit()
        return render_template('message_view.html', msg=msg, is_recipient=is_recipient)

    @app.route('/messages/<int:msg_id>/delete', methods=['POST'])
    @login_required
    @permission_required('messages')
    def message_delete(msg_id):
        msg = Message.query.get_or_404(msg_id)
        if msg.recipient_id == current_user.id:
            msg.deleted_by_recipient = True
        elif msg.sender_id == current_user.id:
            msg.deleted_by_sender = True
        else:
            abort(404)
        if msg.deleted_by_recipient and msg.deleted_by_sender:
            db.session.delete(msg)
        db.session.commit()
        flash('تم حذف الرسالة', 'info')
        return redirect(url_for('messages_list'))

    # ------------------------------------------------------------------
    # الرسائل الرسمية (للأدمن فقط): موضوع، جهة مرسل إليها، رقم إشاري، تاريخ، نص، نسخ
    # ------------------------------------------------------------------
    def parse_letter_form():
        subject = request.form.get('subject', '').strip()
        addressee = request.form.get('addressee', '').strip()
        ref_number = request.form.get('ref_number', '').strip()
        body = request.form.get('body', '').strip()
        cc = '\n'.join(l.strip() for l in request.form.get('cc', '').splitlines() if l.strip())
        try:
            letter_date = datetime.strptime(request.form.get('letter_date', ''), '%Y-%m-%d').date()
        except ValueError:
            letter_date = None
        if not (subject and addressee and body and letter_date):
            return None
        return dict(subject=subject, addressee=addressee, ref_number=ref_number,
                    body=body, cc=cc, letter_date=letter_date)

    @app.route('/messages/letters')
    @login_required
    @permission_required('messages')
    @admin_required
    def messages_letters():
        letters = OfficialLetter.query.order_by(OfficialLetter.letter_date.desc(),
                                                OfficialLetter.id.desc()).all()
        return render_template('letters.html', letters=letters)

    @app.route('/messages/letters/new', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def message_letter_new():
        if request.method == 'POST':
            data = parse_letter_form()
            if not data:
                flash('أكمل الحقول المطلوبة: الموضوع، الجهة، التاريخ، نص الرسالة', 'danger')
                return redirect(url_for('message_letter_new'))
            letter = OfficialLetter(created_by_id=current_user.id, **data)
            db.session.add(letter)
            db.session.commit()
            flash('تم حفظ الرسالة الرسمية', 'success')
            return redirect(url_for('message_letter_view', letter_id=letter.id))
        year = date.today().year
        suggested = f'{year}/{OfficialLetter.query.count() + 1:03d}'
        return render_template('letter_form.html', letter=None,
                               today=date.today().isoformat(), suggested_ref=suggested)

    @app.route('/messages/letters/<int:letter_id>/edit', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def message_letter_edit(letter_id):
        letter = OfficialLetter.query.get_or_404(letter_id)
        if request.method == 'POST':
            data = parse_letter_form()
            if not data:
                flash('أكمل الحقول المطلوبة: الموضوع، الجهة، التاريخ، نص الرسالة', 'danger')
                return redirect(url_for('message_letter_edit', letter_id=letter.id))
            for key, value in data.items():
                setattr(letter, key, value)
            db.session.commit()
            flash('تم تعديل الرسالة', 'success')
            return redirect(url_for('message_letter_view', letter_id=letter.id))
        return render_template('letter_form.html', letter=letter,
                               today=date.today().isoformat(), suggested_ref='')

    @app.route('/messages/letters/<int:letter_id>')
    @login_required
    @admin_required
    def message_letter_view(letter_id):
        letter = OfficialLetter.query.get_or_404(letter_id)
        return render_template('letter_view.html', letter=letter)

    @app.route('/messages/letters/<int:letter_id>/word')
    @login_required
    @admin_required
    def message_letter_word(letter_id):
        letter = OfficialLetter.query.get_or_404(letter_id)
        html = render_template('letter_word.html', letter=letter)
        name = quote(f'رسالة-{letter.ref_number or letter.id}'.replace('/', '-') + '.doc')
        return Response(html, mimetype='application/msword', headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{name}"})

    @app.route('/messages/letters/<int:letter_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def message_letter_delete(letter_id):
        letter = OfficialLetter.query.get_or_404(letter_id)
        db.session.delete(letter)
        db.session.commit()
        flash('تم حذف الرسالة الرسمية', 'info')
        return redirect(url_for('messages_letters'))

    # ------------------------------------------------------------------
    # الأرشفة (الملفات والأوراق المحفوظة)
    # ------------------------------------------------------------------
    @app.route('/archive')
    @login_required
    @permission_required('archive')
    def archive_list():
        category = request.args.get('category', '')
        q = request.args.get('q', '').strip()

        query = Document.query
        if category:
            query = query.filter_by(category=category)
        if q:
            like = f'%{q}%'
            query = query.filter(db.or_(Document.title.ilike(like),
                                        Document.description.ilike(like)))
        docs = query.order_by(Document.created_at.desc()).all()
        return render_template('archive.html', documents=docs,
                               categories=DOCUMENT_CATEGORIES,
                               selected_category=category, q=q)

    @app.route('/archive/new', methods=['GET', 'POST'])
    @login_required
    @permission_required('archive')
    def archive_new():
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            category = request.form.get('category')
            description = request.form.get('description', '').strip()
            side = request.form.get('side') or None
            km_point = request.form.get('km_point') or None

            if not title or category not in DOCUMENT_CATEGORIES:
                flash('يرجى إدخال العنوان واختيار النوع بشكل صحيح', 'danger')
                return redirect(url_for('archive_new'))

            try:
                km_value = float(km_point) if km_point else None
            except ValueError:
                flash('رقم الكيلومتر غير صحيح', 'danger')
                return redirect(url_for('archive_new'))

            doc = Document(title=title, category=category, description=description,
                           side=side if side in SIDES else None, km_point=km_value,
                           uploaded_by_id=current_user.id)

            file = request.files.get('file')
            if file and file.filename:
                if not allowed_file(file.filename):
                    flash('نوع الملف غير مسموح به. استخدم PDF أو Word أو Excel أو صورة.', 'danger')
                    return redirect(url_for('archive_new'))
                original_name = display_name(file.filename)
                ext = file_extension(file.filename)
                stored_name = f"{uuid.uuid4().hex}.{ext}"
                try:
                    storage.save_file(file.stream, stored_name, file.mimetype)
                except Exception:
                    app.logger.exception('فشل رفع الملف إلى التخزين')
                    flash('تعذر رفع الملف إلى التخزين. حاول مرة أخرى.', 'danger')
                    return redirect(url_for('archive_new'))
                doc.stored_file_name = stored_name
                doc.original_file_name = original_name

            db.session.add(doc)
            db.session.commit()
            flash('تمت إضافة العنصر إلى الأرشيف', 'success')
            return redirect(url_for('archive_list'))

        return render_template('archive_form.html', categories=DOCUMENT_CATEGORIES, sides=SIDES)

    @app.route('/archive/<int:doc_id>/download')
    @login_required
    @permission_required('archive')
    def archive_download(doc_id):
        doc = Document.query.get_or_404(doc_id)
        if not doc.has_file:
            abort(404)
        if storage.is_cloud():
            return redirect(storage.download_url(doc.stored_file_name, doc.original_file_name))
        return send_from_directory(storage.local_folder(), doc.stored_file_name,
                                   as_attachment=True, download_name=doc.original_file_name)

    @app.route('/archive/<int:doc_id>/delete', methods=['POST'])
    @login_required
    @permission_required('archive')
    def archive_delete(doc_id):
        doc = Document.query.get_or_404(doc_id)
        if not (current_user.is_admin or doc.uploaded_by_id == current_user.id):
            flash('لا تملك صلاحية حذف هذا العنصر', 'danger')
            return redirect(url_for('archive_list'))
        if doc.has_file:
            storage.delete_file(doc.stored_file_name)
        db.session.delete(doc)
        db.session.commit()
        flash('تم حذف العنصر من الأرشيف', 'info')
        return redirect(url_for('archive_list'))

    # ------------------------------------------------------------------
    # الخريطة
    # ------------------------------------------------------------------
    @app.route('/map')
    @login_required
    @permission_required('map')
    def map_view():
        return render_template('map.html')

    # حصر الأعمال
    # ------------------------------------------------------------------
    @app.route('/works')
    @login_required
    @permission_required('map')
    def works_list():
        side = request.args.get('side', '')
        status = request.args.get('status', '')

        query = WorkRecord.query
        if side:
            query = query.filter_by(side=side)
        if status:
            query = query.filter_by(status=status)

        works = query.order_by(WorkRecord.km_start.asc()).all()
        return render_template('works.html', works=works, sides=SIDES,
                                statuses=WORK_STATUSES,
                                selected_side=side, selected_status=status)

    @app.route('/works/new', methods=['GET', 'POST'])
    @login_required
    @permission_required('map')
    def work_new():
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            side = request.form.get('side')
            km_start = request.form.get('km_start')
            km_end = request.form.get('km_end') or None
            description = request.form.get('description', '').strip()
            status = request.form.get('status', 'قيد التنفيذ')
            work_date = request.form.get('work_date') or None
            latitude = request.form.get('latitude') or None
            longitude = request.form.get('longitude') or None

            if not title or side not in SIDES or not km_start:
                flash('يرجى تعبئة العنوان والجهة ونقطة بداية الكيلومتر', 'danger')
                return redirect(url_for('work_new'))

            work = WorkRecord(
                title=title,
                description=description,
                side=side,
                km_start=float(km_start),
                km_end=float(km_end) if km_end else None,
                status=status,
                work_date=datetime.strptime(work_date, '%Y-%m-%d').date() if work_date else None,
                latitude=float(latitude) if latitude else None,
                longitude=float(longitude) if longitude else None,
                created_by_id=current_user.id
            )
            db.session.add(work)
            db.session.commit()
            flash('تم إضافة سجل العمل بنجاح', 'success')
            return redirect(url_for('works_list'))

        return render_template('work_form.html', sides=SIDES, statuses=WORK_STATUSES,
                                road_length=app.config['ROAD_LENGTH_KM'], work=None)

    @app.route('/works/<int:work_id>/edit', methods=['GET', 'POST'])
    @login_required
    @permission_required('map')
    def work_edit(work_id):
        work = WorkRecord.query.get_or_404(work_id)
        if not (current_user.is_admin or work.created_by_id == current_user.id):
            flash('لا تملك صلاحية تعديل هذا السجل', 'danger')
            return redirect(url_for('works_list'))

        if request.method == 'POST':
            work.title = request.form.get('title', '').strip()
            work.side = request.form.get('side')
            work.km_start = float(request.form.get('km_start'))
            km_end = request.form.get('km_end') or None
            work.km_end = float(km_end) if km_end else None
            work.description = request.form.get('description', '').strip()
            work.status = request.form.get('status', 'قيد التنفيذ')
            work_date = request.form.get('work_date') or None
            work.work_date = datetime.strptime(work_date, '%Y-%m-%d').date() if work_date else None
            latitude = request.form.get('latitude') or None
            longitude = request.form.get('longitude') or None
            work.latitude = float(latitude) if latitude else None
            work.longitude = float(longitude) if longitude else None

            db.session.commit()
            flash('تم تحديث سجل العمل', 'success')
            return redirect(url_for('works_list'))

        return render_template('work_form.html', sides=SIDES, statuses=WORK_STATUSES,
                                road_length=app.config['ROAD_LENGTH_KM'], work=work)

    @app.route('/works/<int:work_id>/delete', methods=['POST'])
    @login_required
    @permission_required('map')
    def work_delete(work_id):
        work = WorkRecord.query.get_or_404(work_id)
        if not (current_user.is_admin or work.created_by_id == current_user.id):
            flash('لا تملك صلاحية حذف هذا السجل', 'danger')
            return redirect(url_for('works_list'))

        db.session.delete(work)
        db.session.commit()
        flash('تم حذف سجل العمل', 'info')
        return redirect(url_for('works_list'))

    @app.route('/map/stations/upload', methods=['POST'])
    @login_required
    @permission_required('map')
    def stations_upload():
        station_file = request.files.get('stations_file')
        if not station_file or not station_file.filename:
            flash('اختر ملف KMZ أو KML أولًا', 'danger')
            return redirect(url_for('map_view'))

        extension = file_extension(station_file.filename)
        if extension not in {'kmz', 'kml'}:
            flash('يسمح فقط بملفات KMZ أو KML', 'danger')
            return redirect(url_for('map_view'))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, 'stations.' + extension)
            station_file.save(tmp_path)
            try:
                points = read_kml_map_data(tmp_path)['stations']
            except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
                flash('تعذر قراءة ملف المحطات. تأكد أنه ملف KMZ/KML صالح.', 'danger')
                return redirect(url_for('map_view'))

            if not points:
                flash('لم يتم العثور على نقاط بإحداثيات داخل الملف', 'danger')
                return redirect(url_for('map_view'))

            try:
                with open(tmp_path, 'rb') as saved:
                    storage.save_file(saved, 'stations/stations.' + extension,
                                      'application/octet-stream')
            except Exception:
                app.logger.exception('فشل رفع ملف المحطات')
                flash('تعذر حفظ ملف المحطات في التخزين.', 'danger')
                return redirect(url_for('map_view'))

        other_extension = 'kml' if extension == 'kmz' else 'kmz'
        storage.delete_file('stations/stations.' + other_extension)
        flash(f'تم تحميل {len(points)} محطة بنجاح', 'success')
        return redirect(url_for('map_view'))

    @app.route('/api/stations')
    @login_required
    @permission_required('map')
    def stations_geo():
        # الأولوية للملف المرفوع من المستخدم (في التخزين)، ثم الملف الافتراضي
        # المرفق مع المشروع في static/data.
        for extension in ('kmz', 'kml'):
            data = storage.read_bytes('stations/stations.' + extension)
            if data is None:
                continue
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = os.path.join(tmp_dir, 'stations.' + extension)
                with open(tmp_path, 'wb') as out:
                    out.write(data)
                try:
                    return jsonify(read_kml_map_data(tmp_path))
                except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
                    return jsonify([])

        default_dir = os.path.join(app.root_path, 'static', 'data')
        for extension in ('kmz', 'kml'):
            station_path = os.path.join(default_dir, 'stations.' + extension)
            if os.path.exists(station_path):
                try:
                    return jsonify(read_kml_map_data(station_path))
                except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
                    return jsonify([])
        return jsonify([])

    @app.route('/api/works-geo')
    @login_required
    @permission_required('map')
    def works_geo():
        """يعيد سجلات الأعمال التي تحتوي على إحداثيات GPS بصيغة JSON لعرضها على الخريطة"""
        works = WorkRecord.query.filter(
            WorkRecord.latitude.isnot(None), WorkRecord.longitude.isnot(None)
        ).all()
        data = []
        for w in works:
            data.append({
                'id': w.id,
                'title': w.title,
                'side': w.side,
                'km_start': w.km_start,
                'km_end': w.km_end,
                'status': w.status,
                'lat': w.latitude,
                'lng': w.longitude,
                'work_date': w.work_date.strftime('%Y-%m-%d') if w.work_date else '',
                'description': w.description or ''
            })
        return jsonify(data)

    # ------------------------------------------------------------------
    # إدارة المستخدمين وصلاحياتهم (أدمن فقط)
    # ------------------------------------------------------------------
    def selected_permissions():
        return [p for p in request.form.getlist('permissions') if p in PERMISSION_KEYS]

    @app.route('/users')
    @login_required
    @admin_required
    def users_list():
        users = User.query.order_by(User.created_at.asc()).all()
        return render_template('users.html', users=users)

    @app.route('/users/new', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def user_new():
        if request.method == 'POST':
            full_name = request.form.get('full_name', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            role = request.form.get('role', 'employee')
            if role not in ('admin', 'employee'):
                role = 'employee'

            if not full_name or not username or not password:
                flash('يرجى تعبئة جميع الحقول', 'danger')
                return redirect(url_for('user_new'))

            if User.query.filter_by(username=username).first():
                flash('اسم المستخدم موجود مسبقًا', 'danger')
                return redirect(url_for('user_new'))

            user = User(full_name=full_name, username=username, role=role)
            user.set_password(password)
            user.set_permissions(selected_permissions())
            db.session.add(user)
            db.session.commit()
            flash('تم إنشاء المستخدم بنجاح', 'success')
            return redirect(url_for('users_list'))

        return render_template('user_form.html', user=None)

    @app.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def user_edit(user_id):
        user = User.query.get_or_404(user_id)
        is_fixed_admin = user.username == fixed_admin_credentials()[0]

        if request.method == 'POST':
            full_name = request.form.get('full_name', '').strip()
            if not full_name:
                flash('الاسم الكامل مطلوب', 'danger')
                return redirect(url_for('user_edit', user_id=user.id))
            user.full_name = full_name

            # لا يُغيَّر دور الأدمن الثابت ولا دور حسابك الحالي
            if not is_fixed_admin and user.id != current_user.id:
                role = request.form.get('role', 'employee')
                user.role = role if role in ('admin', 'employee') else 'employee'

            # كلمة مرور الأدمن الثابت تُضبط من الكود فقط
            new_password = request.form.get('password', '')
            if new_password and not is_fixed_admin:
                user.set_password(new_password)

            user.set_permissions(selected_permissions())
            db.session.commit()
            flash('تم تحديث بيانات المستخدم وصلاحياته', 'success')
            return redirect(url_for('users_list'))

        return render_template('user_form.html', user=user, is_fixed_admin=is_fixed_admin)

    @app.route('/users/<int:user_id>/toggle-active', methods=['POST'])
    @login_required
    @admin_required
    def user_toggle_active(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash('لا يمكنك تعطيل حسابك الخاص', 'danger')
            return redirect(url_for('users_list'))
        if user.username == fixed_admin_credentials()[0]:
            flash('لا يمكن تعطيل حساب الأدمن الثابت', 'danger')
            return redirect(url_for('users_list'))
        user.is_active_user = not user.is_active_user
        db.session.commit()
        flash('تم تحديث حالة المستخدم', 'info')
        return redirect(url_for('users_list'))

    @app.route('/users/<int:user_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def user_delete(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash('لا يمكنك حذف حسابك الخاص', 'danger')
            return redirect(url_for('users_list'))
        if user.username == fixed_admin_credentials()[0]:
            flash('لا يمكن حذف حساب الأدمن الثابت', 'danger')
            return redirect(url_for('users_list'))
        # الرسائل المرتبطة بالمستخدم تُحذف معه حتى لا تبقى مراجع معلّقة
        Message.query.filter(db.or_(Message.sender_id == user.id,
                                    Message.recipient_id == user.id)).delete(
            synchronize_session=False)
        db.session.delete(user)
        db.session.commit()
        flash('تم حذف المستخدم', 'info')
        return redirect(url_for('users_list'))

    # ------------------------------------------------------------------
    # أوامر CLI مساعدة: تهيئة قاعدة البيانات وإنشاء أول حساب أدمن
    # ------------------------------------------------------------------
    @app.cli.command('init-db')
    def init_db():
        """إنشاء جداول قاعدة البيانات"""
        db.create_all()
        print('تم إنشاء قاعدة البيانات بنجاح.')

    @app.cli.command('create-admin')
    def create_admin():
        """إنشاء أول حساب أدمن بشكل تفاعلي"""
        full_name = input('الاسم الكامل: ')
        username = input('اسم المستخدم: ')
        password = input('كلمة المرور: ')

        if User.query.filter_by(username=username).first():
            print('اسم المستخدم موجود مسبقًا!')
            return

        user = User(full_name=full_name, username=username, role='admin')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f'تم إنشاء حساب الأدمن "{username}" بنجاح.')

    return app


app = create_app()

if __name__ == '__main__':
    # يُستخدم فقط عند التشغيل المحلي المباشر (python app.py).
    # على Render يقوم gunicorn (حسب Procfile) باستدعاء app مباشرة، ولا يمر بهذا الشرط إطلاقًا.
    local_port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=local_port)
