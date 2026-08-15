from flask import Flask, render_template, request, jsonify, session, send_from_directory, Response
from flask_socketio import SocketIO, emit, join_room
import psycopg2, psycopg2.extras
import os, uuid
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'lunea-secret-change-me')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
EXT_MIME = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif'}
TIMES = [f'{h:02d}:00' for h in range(24)]
BOOKING_BUFFER_SLOTS = 3  # booked slot + 2 hours after (e.g. 12pm booking closes 12pm,1pm,2pm)

def buffer_slots(time_str):
    if time_str not in TIMES:
        return [time_str]
    idx = TIMES.index(time_str)
    return TIMES[idx:idx+BOOKING_BUFFER_SLOTS]
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'lunea2026')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_db():
    return psycopg2.connect(DATABASE_URL)

def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS appointments (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
        service TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
        notes TEXT DEFAULT '', status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS design_uploads (
        id SERIAL PRIMARY KEY, customer_name TEXT NOT NULL, phone TEXT NOT NULL,
        filename TEXT NOT NULL, original_name TEXT NOT NULL,
        ai_analysis TEXT DEFAULT '', price_estimate TEXT DEFAULT '',
        admin_note TEXT DEFAULT '', admin_price TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY, customer_name TEXT NOT NULL,
        phone TEXT DEFAULT '', last_message TEXT DEFAULT '',
        last_time TEXT DEFAULT '', created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id SERIAL PRIMARY KEY, session_id TEXT NOT NULL,
        sender TEXT NOT NULL, message TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS customers (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS availability (
        id SERIAL PRIMARY KEY,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        is_available BOOLEAN DEFAULT TRUE,
        note TEXT DEFAULT '',
        UNIQUE(date, time))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS customers (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS availability (
        id SERIAL PRIMARY KEY, date TEXT NOT NULL, time TEXT NOT NULL,
        is_available BOOLEAN DEFAULT TRUE, note TEXT DEFAULT '',
        UNIQUE(date,time))''')
    cur.execute("ALTER TABLE design_uploads ADD COLUMN IF NOT EXISTS customer_email TEXT")
    cur.execute("ALTER TABLE design_uploads ADD COLUMN IF NOT EXISTS image_data BYTEA")
    cur.execute("ALTER TABLE design_uploads ADD COLUMN IF NOT EXISTS content_type TEXT")
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_images (
        id SERIAL PRIMARY KEY, data BYTEA NOT NULL, content_type TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS customer_email TEXT")
    cur.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancel_reason TEXT DEFAULT ''")
    # Backfill records uploaded/booked before account linking existed, matching on name+phone
    # (only when that combination maps to exactly one account, to avoid misattributing data).
    cur.execute('''UPDATE design_uploads du SET customer_email = c.email
        FROM customers c
        WHERE (du.customer_email IS NULL OR du.customer_email = '')
        AND du.customer_name = c.name AND du.phone = c.phone
        AND (SELECT COUNT(*) FROM customers c2 WHERE c2.name = du.customer_name AND c2.phone = du.phone) = 1''')
    cur.execute('''UPDATE appointments a SET customer_email = c.email
        FROM customers c
        WHERE (a.customer_email IS NULL OR a.customer_email = '')
        AND a.name = c.name AND a.phone = c.phone
        AND (SELECT COUNT(*) FROM customers c2 WHERE c2.name = a.name AND c2.phone = a.phone) = 1''')
    cur.execute('''CREATE TABLE IF NOT EXISTS announcements (
        id SERIAL PRIMARY KEY,
        type TEXT NOT NULL DEFAULT 'announcement',
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS weekly_schedule (
        id SERIAL PRIMARY KEY,
        day_of_week INTEGER NOT NULL,
        time TEXT NOT NULL,
        UNIQUE(day_of_week, time))''')
    cur.execute('SELECT COUNT(*) FROM weekly_schedule')
    if cur.fetchone()[0] == 0:
        evening = ['17:00','18:00','19:00','20:00','21:00','22:00','23:00']
        weekend = [f'{h:02d}:00' for h in range(9,24)]
        default_pattern = {
            0: ['00:00','01:00'] + evening,   # Monday
            1: ['00:00','01:00','23:00'],     # Tuesday
            2: ['00:00','01:00'] + evening,   # Wednesday
            3: ['23:00'],                     # Thursday
            4: ['00:00','01:00'] + evening,   # Friday
            5: ['00:00','01:00'] + weekend,   # Saturday
            6: ['00:00','01:00'] + weekend,   # Sunday
        }
        for dow, times in default_pattern.items():
            for t in times:
                cur.execute('INSERT INTO weekly_schedule (day_of_week,time) VALUES (%s,%s) ON CONFLICT (day_of_week,time) DO NOTHING',(dow,t))
    conn.commit(); cur.close(); conn.close()

init_db()

def generate_from_schedule(weeks_ahead):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT day_of_week, time FROM weekly_schedule')
    pattern = {}
    for dow, t in cur.fetchall():
        pattern.setdefault(dow, []).append(t)
    created = 0
    today = datetime.now().date()
    for i in range(weeks_ahead * 7):
        d = today + timedelta(days=i)
        for t in pattern.get(d.weekday(), []):
            cur.execute('''INSERT INTO availability (date,time,is_available,note) VALUES (%s,%s,TRUE,'')
                ON CONFLICT (date,time) DO NOTHING''',(d.isoformat(), t))
            created += cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return created

generate_from_schedule(12)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def customer_home():
    customer = None
    if session.get('customer_email'):
        conn = get_db(); cur = dict_cursor(conn)
        cur.execute('SELECT id,email,name,phone FROM customers WHERE email=%s',(session['customer_email'],))
        row = cur.fetchone(); cur.close(); conn.close()
        customer = dict(row) if row else None
    return render_template('customer.html', customer=customer)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/book', methods=['POST'])
def book_appointment():
    d = request.json or {}
    if not all(d.get(f) for f in ['name','phone','date','time']):
        return jsonify({'success':False,'error':'Please fill all required fields.'}), 400
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT is_available FROM availability WHERE date=%s AND time=%s',(d['date'],d['time']))
    slot = cur.fetchone()
    if not slot or not slot['is_available']:
        cur.close(); conn.close()
        return jsonify({'success':False,'error':'This time slot has just been closed. Please choose another time.'}), 409
    cur2 = conn.cursor()
    cur2.execute('INSERT INTO appointments (name,phone,service,date,time,notes,customer_email) VALUES (%s,%s,%s,%s,%s,%s,%s)',
        (d['name'],d['phone'],d.get('service',''),d['date'],d['time'],d.get('notes',''),session.get('customer_email','')))
    cur2.execute('UPDATE availability SET is_available=FALSE WHERE date=%s AND time=%s',(d['date'],d['time']))
    conn.commit(); cur.close(); cur2.close(); conn.close()
    socketio.emit('new_appointment',{'name':d['name'],'date':d['date']},room='admin')
    socketio.emit('availability_changed',{'date':d['date'],'time':d['time'],'is_available':False})
    return jsonify({'success':True})

@app.route('/api/upload-design', methods=['POST'])
def upload_design():
    if 'file' not in request.files:
        return jsonify({'success':False,'error':'No file uploaded.'}), 400
    file = request.files['file']
    name = request.form.get('name','Customer')
    phone = request.form.get('phone','')
    customer_email = session.get('customer_email','')
    if not file or not allowed_file(file.filename):
        return jsonify({'success':False,'error':'Please upload JPG/PNG/WEBP image.'}), 400
    original_name = secure_filename(file.filename)
    filename = f"{uuid.uuid4().hex}_{original_name}"
    content_type = EXT_MIME.get(original_name.rsplit('.',1)[1].lower(), 'image/jpeg')
    image_bytes = file.read()
    conn = get_db(); cur = conn.cursor()
    cur.execute('''INSERT INTO design_uploads
        (customer_name,phone,filename,original_name,customer_email,image_data,content_type)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (name,phone,filename,original_name,customer_email,
         psycopg2.Binary(image_bytes),content_type))
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    socketio.emit('new_design',{'name':name},room='admin')
    return jsonify({'success':True,'image_url':f'/design-image/{new_id}'})

@app.route('/design-image/<int:did>')
def design_image(did):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT image_data, content_type, original_name FROM design_uploads WHERE id=%s',(did,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row or not row[0]:
        return '', 404
    resp = Response(bytes(row[0]), mimetype=row[1] or 'image/jpeg')
    if request.args.get('download'):
        resp.headers['Content-Disposition'] = f'attachment; filename="{row[2] or f"design-{did}.jpg"}"'
    return resp

@app.route('/admin')
def admin_page():
    if not session.get('admin'):
        return render_template('admin.html', logged_in=False)
    return render_template('admin.html', logged_in=True)

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    if request.json.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success':True})
    return jsonify({'success':False,'error':'Wrong password'}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin',None)
    return jsonify({'success':True})

@app.route('/api/admin/appointments')
def get_appointments():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT * FROM appointments ORDER BY date,time')
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/appointments/<int:aid>/status', methods=['POST'])
def update_appt_status(aid):
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    status = d.get('status')
    reason = d.get('reason','') if status == 'cancelled' else ''
    conn = get_db(); cur = conn.cursor()
    cur.execute('UPDATE appointments SET status=%s,cancel_reason=%s WHERE id=%s',(status,reason,aid))
    cur2 = dict_cursor(conn)
    cur2.execute('SELECT phone,service,date,time,customer_email FROM appointments WHERE id=%s',(aid,))
    row = cur2.fetchone()
    changed = []
    if status in ('confirmed','done') and row:
        for t in buffer_slots(row['time']):
            cur.execute('''INSERT INTO availability (date,time,is_available,note) VALUES (%s,%s,FALSE,'')
                ON CONFLICT (date,time) DO UPDATE SET is_available=FALSE''',(row['date'],t))
            changed.append(t)
    elif status == 'cancelled' and row:
        for t in buffer_slots(row['time']):
            cur.execute("SELECT COUNT(*) FROM appointments WHERE date=%s AND time=%s AND status<>'cancelled' AND id<>%s",(row['date'],t,aid))
            if cur.fetchone()[0] == 0:
                cur.execute('UPDATE availability SET is_available=TRUE WHERE date=%s AND time=%s',(row['date'],t))
                changed.append(t)
    conn.commit(); cur.close(); cur2.close(); conn.close()
    if row and row['customer_email']:
        socketio.emit('appointment_status_changed',{
            'status':status,'reason':reason,'service':row['service'],'date':row['date'],'time':row['time']
        }, room='customer_'+row['customer_email'])
    for t in changed:
        socketio.emit('availability_changed',{'date':row['date'],'time':t,'is_available':status=='cancelled'})
    return jsonify({'success':True})

@app.route('/api/admin/designs')
def get_designs():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('''SELECT id,customer_name,phone,filename,original_name,ai_analysis,price_estimate,
        admin_note,admin_price,created_at,customer_email FROM design_uploads ORDER BY created_at DESC''')
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/customers')
def get_customers():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT id,email,name,phone,created_at FROM customers ORDER BY created_at DESC')
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/announcements')
def get_announcements():
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute("SELECT * FROM announcements WHERE active=TRUE ORDER BY created_at DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/announcements')
def admin_get_announcements():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute("SELECT * FROM announcements ORDER BY created_at DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/announcements', methods=['POST'])
def create_announcement():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    if not d.get('title'):
        return jsonify({'success':False,'error':'Title is required.'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO announcements (type,title,content) VALUES (%s,%s,%s)',
        (d.get('type','announcement'),d['title'],d.get('content','')))
    conn.commit(); cur.close(); conn.close()
    socketio.emit('announcements_changed',{})
    return jsonify({'success':True})

@app.route('/api/admin/announcements/<int:aid>', methods=['POST'])
def update_announcement(aid):
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    conn = get_db(); cur = conn.cursor()
    cur.execute('UPDATE announcements SET type=%s,title=%s,content=%s,active=%s WHERE id=%s',
        (d.get('type','announcement'),d.get('title',''),d.get('content',''),d.get('active',True),aid))
    conn.commit(); cur.close(); conn.close()
    socketio.emit('announcements_changed',{})
    return jsonify({'success':True})

@app.route('/api/admin/announcements/<int:aid>', methods=['DELETE'])
def delete_announcement(aid):
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute('DELETE FROM announcements WHERE id=%s',(aid,))
    conn.commit(); cur.close(); conn.close()
    socketio.emit('announcements_changed',{})
    return jsonify({'success':True})

@app.route('/api/admin/designs/<int:did>/review', methods=['POST'])
def review_design(did):
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    conn = get_db(); cur = conn.cursor()
    cur.execute('UPDATE design_uploads SET admin_note=%s,admin_price=%s WHERE id=%s',
        (d.get('note',''),d.get('price',''),did))
    cur2 = dict_cursor(conn)
    cur2.execute('SELECT phone,customer_name FROM design_uploads WHERE id=%s',(did,))
    row = cur2.fetchone()
    conn.commit(); cur.close(); cur2.close(); conn.close()
    if row and row['phone']:
        socketio.emit('design_reviewed',{
            'admin_price': d.get('price',''),
            'admin_note': d.get('note',''),
            'customer_name': row['customer_name']
        }, room='design_notify_'+row['phone'])
    return jsonify({'success':True})

@app.route('/api/admin/chat-sessions')
def get_chat_sessions():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT * FROM chat_sessions ORDER BY last_time DESC,created_at DESC')
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/chat/messages/<session_id>')
def get_messages(session_id):
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT * FROM chat_messages WHERE session_id=%s ORDER BY timestamp',(session_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/chat/upload-image', methods=['POST'])
def upload_chat_image():
    if 'file' not in request.files:
        return jsonify({'success':False,'error':'No file uploaded.'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'success':False,'error':'Please upload JPG/PNG/WEBP image.'}), 400
    content_type = EXT_MIME.get(file.filename.rsplit('.',1)[1].lower(), 'image/jpeg')
    image_bytes = file.read()
    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO chat_images (data,content_type) VALUES (%s,%s) RETURNING id',
        (psycopg2.Binary(image_bytes),content_type))
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success':True,'image_url':f'/chat-image/{new_id}'})

@app.route('/chat-image/<int:iid>')
def chat_image(iid):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT data, content_type FROM chat_images WHERE id=%s',(iid,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        return '', 404
    resp = Response(bytes(row[0]), mimetype=row[1])
    if request.args.get('download'):
        ext = (row[1] or 'image/jpeg').split('/')[-1]
        resp.headers['Content-Disposition'] = f'attachment; filename="chat-photo-{iid}.{ext}"'
    return resp

@socketio.on('join')
def on_join(data):
    sid = data.get('session_id')
    name = data.get('name','Customer')
    phone = data.get('phone','')
    is_admin = data.get('is_admin',False)
    if is_admin:
        join_room('admin')
    else:
        join_room(sid)
        conn = get_db(); cur = dict_cursor(conn)
        cur.execute('SELECT id FROM chat_sessions WHERE id=%s',(sid,))
        exists = cur.fetchone()
        if not exists:
            cur2 = conn.cursor()
            cur2.execute('INSERT INTO chat_sessions (id,customer_name,phone) VALUES (%s,%s,%s)',(sid,name,phone))
            cur2.close()
            conn.commit()
        cur.close(); conn.close()
        emit('new_session',{'session_id':sid,'name':name,'phone':phone},room='admin')

@socketio.on('send_message')
def on_message(data):
    sid = data.get('session_id','')
    msg = data.get('message','').strip()
    sender = data.get('sender','customer')
    if not msg or not sid: return
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO chat_messages (session_id,sender,message,timestamp) VALUES (%s,%s,%s,%s)',(sid,sender,msg,now))
    cur.execute('UPDATE chat_sessions SET last_message=%s,last_time=%s WHERE id=%s',(msg[:60],now,sid))
    conn.commit(); cur.close(); conn.close()
    emit('receive_message',{'session_id':sid,'sender':sender,'message':msg,'timestamp':now},room=sid)
    if sender == 'customer':
        emit('customer_message',{'session_id':sid,'message':msg,'timestamp':now},room='admin')

@socketio.on('admin_join_session')
def admin_join_session(data):
    join_room(data.get('session_id'))

@socketio.on('join_design_notify')
def join_design_notify(data):
    phone = data.get('phone','')
    if phone:
        join_room('design_notify_'+phone)

@socketio.on('join_customer_room')
def join_customer_room():
    email = session.get('customer_email','')
    if email:
        join_room('customer_'+email)

@app.route('/api/signup', methods=['POST'])
def customer_signup():
    d = request.json or {}
    email = d.get('email','').strip().lower()
    name = d.get('name','').strip()
    phone = d.get('phone','').strip()
    password = d.get('password','')
    if not all([email, name, password]):
        return jsonify({'success':False,'error':'Please fill all required fields.'}), 400
    if len(password) < 6:
        return jsonify({'success':False,'error':'Password must be at least 6 characters.'}), 400
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute('INSERT INTO customers (email,name,phone,password_hash) VALUES (%s,%s,%s,%s)',
            (email,name,phone,generate_password_hash(password)))
        conn.commit(); cur.close(); conn.close()
        session['customer_email'] = email
        session['customer_name'] = name
        return jsonify({'success':True})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        if 'unique' in str(e).lower():
            return jsonify({'success':False,'error':'Email already registered. Please login instead.'}), 400
        return jsonify({'success':False,'error':'Signup failed. Please try again.'}), 500

@app.route('/api/customer/login', methods=['POST'])
def customer_login():
    d = request.json or {}
    email = d.get('email','').strip().lower()
    password = d.get('password','')
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT * FROM customers WHERE email=%s',(email,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row or not check_password_hash(row['password_hash'], password):
        return jsonify({'success':False,'error':'Wrong email or password.'}), 401
    session['customer_email'] = row['email']
    session['customer_name'] = row['name']
    return jsonify({'success':True})

@app.route('/api/customer/logout', methods=['POST'])
def customer_logout():
    session.pop('customer_email',None)
    session.pop('customer_name',None)
    return jsonify({'success':True})

@app.route('/api/admin/availability', methods=['GET'])
def get_availability():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    month = request.args.get('month','')
    conn = get_db(); cur = dict_cursor(conn)
    if month:
        cur.execute("SELECT * FROM availability WHERE date LIKE %s ORDER BY date,time", (month+'%',))
    else:
        cur.execute("SELECT * FROM availability ORDER BY date,time")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/availability', methods=['POST'])
def save_availability():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    conn = get_db(); cur = conn.cursor()
    cur.execute('''INSERT INTO availability (date,time,is_available,note)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (date,time) DO UPDATE SET is_available=%s, note=%s''',
        (d['date'],d['time'],d.get('is_available',True),d.get('note',''),
         d.get('is_available',True),d.get('note','')))
    conn.commit(); cur.close(); conn.close()
    socketio.emit('availability_changed',{'date':d['date'],'time':d['time'],'is_available':d.get('is_available',True)})
    return jsonify({'success':True})

@app.route('/api/admin/availability/<int:aid>', methods=['DELETE'])
def delete_availability(aid):
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT date,time FROM availability WHERE id=%s',(aid,))
    row = cur.fetchone()
    cur2 = conn.cursor()
    cur2.execute('DELETE FROM availability WHERE id=%s',(aid,))
    conn.commit(); cur.close(); cur2.close(); conn.close()
    if row:
        socketio.emit('availability_changed',{'date':row['date'],'time':row['time'],'is_available':False})
    return jsonify({'success':True})

@app.route('/api/availability/open')
def get_open_slots():
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute("SELECT date,time FROM availability WHERE is_available=TRUE AND date >= CURRENT_DATE::TEXT ORDER BY date,time")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/availability')
def get_public_availability():
    month = request.args.get('month','')
    start = request.args.get('start','')
    end = request.args.get('end','')
    conn = get_db(); cur = dict_cursor(conn)
    if month:
        cur.execute("SELECT date,time,note FROM availability WHERE is_available=TRUE AND date LIKE %s ORDER BY date,time",(month+'%',))
    elif start and end:
        cur.execute("SELECT date,time,note FROM availability WHERE is_available=TRUE AND date BETWEEN %s AND %s ORDER BY date,time",(start,end))
    else:
        cur.execute("SELECT date,time,note FROM availability WHERE is_available=TRUE AND date >= CURRENT_DATE::TEXT ORDER BY date,time")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/weekly-schedule')
def get_weekly_schedule():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT day_of_week, time FROM weekly_schedule ORDER BY day_of_week, time')
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/weekly-schedule', methods=['POST'])
def toggle_weekly_schedule():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    dow, t = d.get('day_of_week'), d.get('time')
    if dow is None or not t:
        return jsonify({'success':False,'error':'Missing day_of_week or time.'}), 400
    conn = get_db(); cur = conn.cursor()
    if d.get('open'):
        cur.execute('INSERT INTO weekly_schedule (day_of_week,time) VALUES (%s,%s) ON CONFLICT (day_of_week,time) DO NOTHING',(dow,t))
    else:
        cur.execute('DELETE FROM weekly_schedule WHERE day_of_week=%s AND time=%s',(dow,t))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'success':True})

@app.route('/api/admin/weekly-schedule/generate', methods=['POST'])
def trigger_generate_schedule():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    d = request.json or {}
    weeks = max(1, min(int(d.get('weeks',12)), 52))
    created = generate_from_schedule(weeks)
    socketio.emit('availability_changed',{'date':'','time':'','is_available':True})
    return jsonify({'success':True,'created':created})

@app.route('/api/customer/designs')
def get_customer_designs():
    if not session.get('customer_email'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('''SELECT id,customer_name,phone,filename,original_name,ai_analysis,price_estimate,
        admin_note,admin_price,created_at,customer_email FROM design_uploads
        WHERE customer_email=%s ORDER BY created_at DESC''',(session['customer_email'],))
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/customer/appointments')
def get_customer_appointments():
    if not session.get('customer_email'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT * FROM appointments WHERE customer_email=%s ORDER BY created_at DESC',(session['customer_email'],))
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])


if __name__ == '__main__':
    port = int(os.environ.get('PORT',5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
