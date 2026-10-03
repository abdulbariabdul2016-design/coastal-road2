# نظام إدارة أوراق وحصر أعمال الطريق الساحلي

تطبيق Flask لإدارة التقارير والرسائل والأرشفة وحصر الأعمال على الخريطة، مع صلاحيات (أدمن / موظف).

## الأقسام والصلاحيات
- الأقسام: **الخريطة** (وتُفتح منها صفحات حصر الأعمال)، **التقارير** (يومية وأسبوعية وشهرية)، **الرسائل**، **الأرشفة**، **المستخدمون** (للأدمن فقط).
- الأدمن يحدد لكل موظف الأقسام التي تظهر له من: المستخدمون ← تعديل / صلاحيات. الموظف الجديد لا يرى أي قسم حتى تُمنح له.
- التقرير اليومي يُكتب من نافذة: اسم الشخص، نوع العمل، المحطة من/إلى، تقرير المهندس، الملاحظات، المرفقات.
- الأعمدة والجداول الجديدة تُضاف تلقائيًا لقاعدة البيانات عند التشغيل.

## النشر: GitHub ثم Neon ثم Render

### 1) GitHub
ارفع **محتويات** هذا المجلد إلى جذر المستودع (يجب أن يظهر `app.py` و`requirements.txt` مباشرة في الصفحة الرئيسية للمستودع).

### 2) Neon (قاعدة البيانات)
1. أنشئ مشروعًا في https://neon.tech
2. من Dashboard اضغط **Connect** وانسخ **Connection string** (يبدأ بـ `postgresql://`).
3. احتفظ به لاستخدامه في Render كقيمة `DATABASE_URL`.

### 3) Render
1. **New → Web Service** واربطه بالمستودع.
2. Runtime: `Python 3`
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Environment Variables:
   - `PYTHON_VERSION` = `3.11.11`
   - `SECRET_KEY` = قيمة عشوائية طويلة
   - `DATABASE_URL` = رابط Neon
   - `ADMIN_USERNAME`, `ADMIN_PASSWORD` = بيانات الأدمن الأول
6. اضغط Deploy. تُنشأ الجداول وحساب الأدمن الثابت تلقائيًا.

**حساب الأدمن الثابت:** يُعرَّف في أعلى `app.py` (`DEFAULT_ADMIN_USERNAME` و`DEFAULT_ADMIN_PASSWORD`) وتُعاد كلمة مروره إلى هذه القيمة عند كل تشغيل، ولا يمكن حذفه أو تعطيله من صفحة المستخدمين. إن كان مستودع GitHub عامًا فغيّر القيم قبل النشر.

### 4) التخزين السحابي (Cloudflare R2 — مجاني حتى 10GB)
بدونه تُمسح ملفات الأوراق عند كل إعادة نشر على Render.
1. في Cloudflare: **R2 Object Storage ← Create bucket** (مثلًا `coastal-files`).
2. **R2 ← Manage API Tokens ← Create API token** بصلاحية **Object Read & Write** على هذا الـ bucket.
3. انسخ: Access Key ID وSecret Access Key، ومن صفحة R2 انسخ الـ Account ID.
4. أضف في Render:
   - `S3_BUCKET` = اسم الـ bucket
   - `S3_ENDPOINT_URL` = `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
   - `S3_ACCESS_KEY_ID` و`S3_SECRET_ACCESS_KEY`
   - `S3_REGION` = `auto`

يعمل الكود أيضًا مع Backblaze B2 وAWS S3 وأي خدمة متوافقة مع S3 بتغيير `S3_ENDPOINT_URL` و`S3_REGION` فقط.
عند عدم ضبط هذه المتغيرات تُحفظ الملفات محليًا في `static/uploads` (للتشغيل المحلي فقط).

## التشغيل محليًا
```bash
python -m venv venv
source venv/bin/activate        # ويندوز: venv\Scripts\activate
pip install -r requirements.txt
flask --app app create-admin
python app.py
```
ثم افتح http://127.0.0.1:5000
