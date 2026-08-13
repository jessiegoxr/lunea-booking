from flask import Flask, render_template, request, jsonify, session, send_from_directory, Response
from flask_socketio import SocketIO, emit, join_room
import psycopg2, psycopg2.extras
import os, uuid, base64
from datetime import datetime
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'lunea-secret-change-me')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
EXT_MIME = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif'}
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'lunea2026')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
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
    conn.commit(); cur.close(); conn.close()

init_db()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def analyze_nail_design(image_bytes, media_type):
    if not ANTHROPIC_API_KEY:
        return {'analysis': '⚠️ AI key not configured. Admin will review manually.', 'price_estimate': 'Pending review'}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        image_data = base64.standard_b64encode(image_bytes).decode('utf-8')
        response = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=600,
            messages=[{'role':'user','content':[
                {'type':'image','source':{'type':'base64','media_type':media_type,'data':image_data}},
                {'type':'text','text':'''You are a nail technician assistant for LUNÉA Nail Studio, Penang.
Analyze this nail design and respond EXACTLY in this format:

**款式复杂度 Complexity:** [Simple 简单 / Moderate 中等 / Complex 复杂]
**技术 Techniques:** [list: gel, extension, cat eye, nail art, ombre, etc.]
**估价 Estimated Price:** RM [amount or range]
**说明 Notes:** [1-2 sentences describing the design in English & Chinese]

Price reference:
- Gel manicure single/ombre/cat eye: RM50
- Gel manicure simple design: RM65
- Gel manicure complex design: RM85
- Nail extension single/ombre/cat eye: RM95
- Nail extension simple design: RM115
- Nail extension complex design: RM130
- Accessories add-on: RM2-10'''}]}])
        text = response.content[0].text
        price_line = next((l for l in text.split('\n') if 'Estimated Price' in l or '估价' in l), '')
        price = price_line.split(':',1)[-1].strip() if price_line else 'See analysis'
        return {'analysis': text, 'price_estimate': price}
    except Exception as e:
        return {'analysis': f'Analysis error: {str(e)}', 'price_estimate': 'Contact us for quote'}

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
    if not all(d.get(f) for f in ['name','phone','service','date','time']):
        return jsonify({'success':False,'error':'Please fill all required fields.'}), 400
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('SELECT is_available FROM availability WHERE date=%s AND time=%s',(d['date'],d['time']))
    slot = cur.fetchone()
    if not slot or not slot['is_available']:
        cur.close(); conn.close()
        return jsonify({'success':False,'error':'This time slot has just been closed. Please choose another time.'}), 409
    cur2 = conn.cursor()
    cur2.execute('INSERT INTO appointments (name,phone,service,date,time,notes,customer_email) VALUES (%s,%s,%s,%s,%s,%s,%s)',
        (d['name'],d['phone'],d['service'],d['date'],d['time'],d.get('notes',''),session.get('customer_email','')))
    conn.commit(); cur.close(); cur2.close(); conn.close()
    socketio.emit('new_appointment',{'name':d['name'],'service':d['service'],'date':d['date']},room='admin')
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
    result = analyze_nail_design(image_bytes, content_type)
    conn = get_db(); cur = conn.cursor()
    cur.execute('''INSERT INTO design_uploads
        (customer_name,phone,filename,original_name,ai_analysis,price_estimate,customer_email,image_data,content_type)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (name,phone,filename,original_name,result['analysis'],result['price_estimate'],customer_email,
         psycopg2.Binary(image_bytes),content_type))
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    socketio.emit('new_design',{'name':name,'price':result['price_estimate']},room='admin')
    return jsonify({'success':True,'analysis':result['analysis'],'price_estimate':result['price_estimate'],'image_url':f'/design-image/{new_id}'})

@app.route('/design-image/<int:did>')
def design_image(did):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT image_data, content_type FROM design_uploads WHERE id=%s',(did,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row or not row[0]:
        return '', 404
    return Response(bytes(row[0]), mimetype=row[1] or 'image/jpeg')

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
    cur2.execute('SELECT phone,service,date,time FROM appointments WHERE id=%s',(aid,))
    row = cur2.fetchone()
    if status == 'done' and row:
        cur.execute('''INSERT INTO availability (date,time,is_available,note) VALUES (%s,%s,FALSE,'')
            ON CONFLICT (date,time) DO UPDATE SET is_available=FALSE''',(row['date'],row['time']))
    conn.commit(); cur.close(); cur2.close(); conn.close()
    if row and row['phone']:
        socketio.emit('appointment_status_changed',{
            'status':status,'reason':reason,'service':row['service'],'date':row['date'],'time':row['time']
        }, room='appt_notify_'+row['phone'])
    if status == 'done' and row:
        socketio.emit('availability_changed',{'date':row['date'],'time':row['time'],'is_available':False})
    return jsonify({'success':True})

@app.route('/api/admin/designs')
def get_designs():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); cur = dict_cursor(conn)
    cur.execute('''SELECT id,customer_name,phone,filename,original_name,ai_analysis,price_estimate,
        admin_note,admin_price,created_at,customer_email FROM design_uploads ORDER BY created_at DESC''')
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])

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
    return Response(bytes(row[0]), mimetype=row[1])

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

@socketio.on('join_appt_notify')
def join_appt_notify(data):
    phone = data.get('phone','')
    if phone:
        join_room('appt_notify_'+phone)

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
