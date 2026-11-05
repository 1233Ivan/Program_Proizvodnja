# -*- coding: utf-8 -*-
import os
import sys
import sqlite3
import threading
import zipfile
import base64
import io
import hashlib
import json
import html
import time
import atexit
import collections
from datetime import datetime, timedelta
from flask import Flask, request, redirect, url_for, render_template, send_from_directory, session, make_response, send_file, g, Response, jsonify
from werkzeug.utils import secure_filename

def dobij_zg_vrijeme():
    utc_sada = datetime.utcnow()
    godina = utc_sada.year
    pocetak_ljetnog = datetime(godina, 3, 31)
    pocetak_ljetnog -= timedelta(days=(pocetak_ljetnog.weekday() + 1) % 7)
    pocetak_ljetnog = pocetak_ljetnog.replace(hour=1, minute=0, second=0)
    kraj_ljetnog = datetime(godina, 10, 31)
    kraj_ljetnog -= timedelta(days=(kraj_ljetnog.weekday() + 1) % 7)
    kraj_ljetnog = kraj_ljetnog.replace(hour=1, minute=0, second=0)
    
    if pocetak_ljetnog <= utc_sada < kraj_ljetnog:
        return utc_sada + timedelta(hours=2)
    else:
        return utc_sada + timedelta(hours=1)

app = Flask(__name__)
app.secret_key = 'firstcut_pogon_secure_key_2026'
app.permanent_session_lifetime = timedelta(days=365)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

_B = base64.b64decode(b'MjMxMg==').decode('utf-8')
PIN_HASH = hashlib.sha256(_B.encode()).hexdigest()
del _B

ADMIN_HASH = hashlib.sha256("firstcutlaser1".encode('utf-8')).hexdigest()
DEV_HASH = hashlib.sha256("87388738".encode('utf-8')).hexdigest()

def get_base_path():
    if getattr(sys, 'frozen', False): return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_path()
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'pohrana_nacrta')
BACKUP_FOLDER = os.path.join(BASE_DIR, 'sigurnosne_kopije')
DB_FILE = os.path.join(BASE_DIR, 'proizvodnja.db')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['BACKUP_FOLDER'] = BACKUP_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {'pdf', 'lxds', 'dxf'}

if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
if not os.path.exists(BACKUP_FOLDER): os.makedirs(BACKUP_FOLDER)

# In-memory predmemorija logotipa i statusa auto-arhiviranja
_CACHED_LOGO = None
_ZADNJA_AUTO_ARHIVA = None

# --- SSE REAL-TIME EVENT BUS (Munjevita push sinkronizacija) ---
_SSE_CONDITION = threading.Condition()
_STATE_VERSION = 0
_STATE_LAST_CHANGE = time.time()
_STATE_SENDER = ""
_STATE_REASON = "init"
_DIR_MTIME_CACHE = {}

def objavi_promjenu(uzrok="promjena", sender_id=None):
    """Obavještava sve povezane klijente o novoj promjeni u bazi ili datotekama."""
    global _STATE_VERSION, _STATE_LAST_CHANGE, _STATE_SENDER, _STATE_REASON
    with _SSE_CONDITION:
        _STATE_VERSION += 1
        _STATE_LAST_CHANGE = time.time()
        _STATE_SENDER = sender_id or ""
        _STATE_REASON = uzrok or "promjena"
        _SSE_CONDITION.notify_all()

def provjeri_vanjske_promjene_datoteka():
    """Provjerava jesu li predlošci ili upload mapa promijenjeni izvana (npr. git pull)."""
    global _DIR_MTIME_CACHE
    promjena = False
    za_provjeru = [
        ('templates', os.path.join(BASE_DIR, 'templates')),
        ('uploads', UPLOAD_FOLDER)
    ]
    for oznaka, putanja in za_provjeru:
        if os.path.exists(putanja):
            try:
                stavke = os.listdir(putanja)
                trenutni_mtime = max(os.path.getmtime(os.path.join(putanja, f)) for f in stavke) if stavke else 0
                if oznaka in _DIR_MTIME_CACHE and trenutni_mtime > _DIR_MTIME_CACHE[oznaka]:
                    promjena = True
                _DIR_MTIME_CACHE[oznaka] = trenutni_mtime
            except Exception:
                pass
    if promjena:
        objavi_promjenu("vanjska_datoteka")

# --- IDEMPOTENCY PROTECTION (Zaštita od duplog slanja na nestabilnoj mreži) ---
_OBRADJENI_UUID = collections.deque(maxlen=500)

def je_duplikat_zahtjev(uuid_token):
    if not uuid_token:
        return False
    if uuid_token in _OBRADJENI_UUID:
        return True
    _OBRADJENI_UUID.append(uuid_token)
    return False

# --- SIGURNOSNE KOPIJE (ONLINE SQLITE BACKUP & WAL CHECKPOINT) ---
def napravi_sigurnosnu_kopiju_baze(oznaka="auto"):
    """Radi atomarni online backup žive SQLite baze u sigurnosne_kopije/."""
    if not os.path.exists(DB_FILE) or os.path.getsize(DB_FILE) == 0:
        return None
    try:
        ts = dobij_zg_vrijeme().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(BACKUP_FOLDER, f"proizvodnja_{oznaka}_{ts}.db")
        src = sqlite3.connect(DB_FILE, timeout=20.0)
        dst = sqlite3.connect(backup_path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        # Održavanje najviše 25 zadnjih sigurnosnih kopija
        sve_kopije = sorted([os.path.join(BACKUP_FOLDER, f) for f in os.listdir(BACKUP_FOLDER) if f.endswith('.db')], key=os.path.getmtime)
        while len(sve_kopije) > 25:
            try: os.remove(sve_kopije.pop(0))
            except Exception: pass
        return backup_path
    except Exception:
        return None

def sigurno_zatvori_bazu():
    """Atexit handler: osigurava da se sav WAL log zapiše u glavnu proizvodnja.db."""
    try:
        if os.path.exists(DB_FILE):
            conn = sqlite3.connect(DB_FILE, timeout=10.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
    except Exception:
        pass

atexit.register(sigurno_zatvori_bazu)

def get_cached_logo():
    global _CACHED_LOGO
    if _CACHED_LOGO is not None:
        return _CACHED_LOGO
    logo_base64, logo_ext = "", ""
    for ext in ['png', 'jpg', 'jpeg']:
        path = os.path.join(BASE_DIR, f'html_logo.{ext}')
        if os.path.exists(path):
            try:
                with open(path, "rb") as lf:
                    logo_base64 = base64.b64encode(lf.read()).decode('utf-8')
                    logo_ext = "jpeg" if ext == "jpg" else ext
                break
            except Exception:
                pass
    _CACHED_LOGO = (logo_base64, logo_ext)
    return _CACHED_LOGO

def sigurno_ime(tekst):
    if not tekst: return ""
    zamjene = {
        'č': 'c', 'ć': 'c', 'ž': 'z', 'š': 's', 'đ': 'dj',
        'Č': 'C', 'Ć': 'C', 'Ž': 'Z', 'Š': 'S', 'Đ': 'Dj'
    }
    for k, v in zamjene.items():
        tekst = tekst.replace(k, v)
    return secure_filename(tekst)

def spremi_upload_datoteku(f):
    """Sigurno sprema datoteku u UPLOAD_FOLDER s rješavanjem kolizije imena."""
    if not f or not f.filename or not allowed_file(f.filename):
        return None
    clean_original = sigurno_ime(f.filename)
    if not clean_original:
        clean_original = "datoteka.pdf"
    base_ts = dobij_zg_vrijeme().strftime('%Y%m%d%H%M%S')
    fname = f"{base_ts}_{clean_original}"
    fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
    counter = 1
    while os.path.exists(fpath):
        fname = f"{base_ts}_{counter}_{clean_original}"
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        counter += 1
    f.save(fpath)
    return fname

def get_db_connection():
    try:
        db = getattr(g, '_database', None)
        if db is None:
            db = sqlite3.connect(DB_FILE, timeout=20.0)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA cache_size=-20000")
            g._database = db
        return db
    except RuntimeError:
        # Poziv izvan Flask request konteksta (npr. u init_db)
        conn = sqlite3.connect(DB_FILE, timeout=20.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-20000")
        return conn

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass
        g._database = None

def dohvati_pozicije_za_naloge(conn, nalog_ids):
    if not nalog_ids:
        return {}
    placeholders = ','.join(['?'] * len(nalog_ids))
    rows = conn.execute(f"SELECT * FROM nalog_pozicije WHERE nalog_id IN ({placeholders}) ORDER BY id ASC", tuple(nalog_ids)).fetchall()
    poz_map = {nid: [] for nid in nalog_ids}
    for r in rows:
        poz_map[r['nalog_id']].append(dict(r))
    return poz_map

def izracunaj_ukupno_za_nalog(pozicije):
    """Zbraja komade, škart i radne sate za laser i bravariju te vraća formatirane podatke."""
    uk_cilj = 0
    l_kom_uk, l_skart_uk = 0, 0
    b_kom_uk, b_skart_uk = 0, 0
    l_p_min_uk, l_r_min_uk = 0, 0
    b_p_min_uk, b_pig_min_uk = 0, 0
    
    for p in pozicije:
        uk_cilj += (p.get('ciljana_kolicina') or 0)
        l_kom_uk += (p.get('laser_komada') or 0)
        l_skart_uk += (p.get('laser_skart') or 0)
        b_kom_uk += (p.get('bravarija_komada') or 0)
        b_skart_uk += (p.get('bravarija_skart') or 0)
        
        l_p_min_uk += ((p.get('laser_priprema_sati') or 0) * 60 + (p.get('laser_priprema_minute') or 0))
        l_r_min_uk += ((p.get('laser_rezanje_sati') or 0) * 60 + (p.get('laser_rezanje_minute') or 0))
        b_p_min_uk += ((p.get('bravarija_priprema_sati') or 0) * 60 + (p.get('bravarija_priprema_minute') or 0))
        b_pig_min_uk += ((p.get('bravarija_piganje_sati') or 0) * 60 + (p.get('bravarija_piganje_minute') or 0))
        
    l_uk_min = l_p_min_uk + l_r_min_uk
    b_uk_min = b_p_min_uk + b_pig_min_uk
    sveuk_min = l_uk_min + b_uk_min
    
    return {
        'cilj_ukupno': uk_cilj,
        'laser_komada_ukupno': l_kom_uk,
        'laser_skart_ukupno': l_skart_uk,
        'laser_priprema_tekst': f"{l_p_min_uk // 60}h {l_p_min_uk % 60}m",
        'laser_rezanje_tekst': f"{l_r_min_uk // 60}h {l_r_min_uk % 60}m",
        'laser_ukupno_tekst': f"{l_uk_min // 60}h {l_uk_min % 60}m",
        'bravarija_komada_ukupno': b_kom_uk,
        'bravarija_skart_ukupno': b_skart_uk,
        'bravarija_priprema_tekst': f"{b_p_min_uk // 60}h {b_p_min_uk % 60}m",
        'bravarija_piganje_tekst': f"{b_pig_min_uk // 60}h {b_pig_min_uk % 60}m",
        'bravarija_ukupno_tekst': f"{b_uk_min // 60}h {b_uk_min % 60}m",
        'sveukupno_vrijeme_tekst': f"{sveuk_min // 60}h {sveuk_min % 60}m"
    }

def init_db():
    if os.path.exists(DB_FILE) and os.path.getsize(DB_FILE) > 0:
        napravi_sigurnosnu_kopiju_baze("startup")
        
    conn = sqlite3.connect(DB_FILE, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")
    conn.execute('''CREATE TABLE IF NOT EXISTS radni_nalozi (
        id INTEGER PRIMARY KEY AUTOINCREMENT, naziv_naloga TEXT NOT NULL, naziv_projekta TEXT DEFAULT '',
        debljina_ploce TEXT DEFAULT '', pdf_datoteka TEXT, lxdf_datoteka TEXT, status TEXT DEFAULT 'Laser',
        laser_zapoceto_u TEXT, bravarija_zapoceto_u TEXT, kreirano_u TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        opis TEXT DEFAULT '', laser_napomena TEXT DEFAULT '', bravarija_napomena TEXT DEFAULT '',
        rutiranje TEXT DEFAULT 'Pogon', odabrani_laser TEXT DEFAULT '', kreirao TEXT DEFAULT '',
        dimenzije_ploce_laser TEXT DEFAULT '', materijal_ploce_laser TEXT DEFAULT '', zavrseno_u TIMESTAMP,
        laser_radnik TEXT DEFAULT '', bravarija_radnik TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS nalog_pozicije (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nalog_id INTEGER, naziv_pozicije TEXT NOT NULL,
        laser_komada INTEGER DEFAULT NULL, laser_skart INTEGER DEFAULT NULL, laser_sati INTEGER DEFAULT 0, laser_minute INTEGER DEFAULT 0, laser_radnik TEXT DEFAULT '',
        bravarija_komada INTEGER DEFAULT NULL, bravarija_skart INTEGER DEFAULT NULL, bravarija_sati INTEGER DEFAULT 0, bravarija_minute INTEGER DEFAULT 0, bravarija_radnik TEXT DEFAULT '',
        ciljana_kolicina INTEGER DEFAULT 0, laser_priprema_sati INTEGER DEFAULT NULL, laser_priprema_minute INTEGER DEFAULT NULL,
        laser_rezanje_sati INTEGER DEFAULT NULL, laser_rezanje_minute INTEGER DEFAULT NULL, bravarija_priprema_sati INTEGER DEFAULT NULL,
        bravarija_priprema_minute INTEGER DEFAULT NULL, bravarija_piganje_sati INTEGER DEFAULT NULL, bravarija_piganje_minute INTEGER DEFAULT NULL,
        FOREIGN KEY(nalog_id) REFERENCES radni_nalozi(id) ON DELETE CASCADE
    )''')
    conn.execute('CREATE TABLE IF NOT EXISTS blokade (ip_adresa TEXT PRIMARY KEY, pokusaji INTEGER DEFAULT 0, blokiran_do TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS obavijesti (id INTEGER PRIMARY KEY AUTOINCREMENT, tekst TEXT NOT NULL, vrijedi_do TIMESTAMP NOT NULL)')
    
    conn.execute('CREATE TABLE IF NOT EXISTS postavke (kljuc TEXT PRIMARY KEY, vrijednost TEXT)')
    conn.execute('INSERT OR IGNORE INTO postavke (kljuc, vrijednost) VALUES ("maintenance", "0")')
    
    kolone_radni = [
        ("laser_zapoceto_u", "TEXT"), ("bravarija_zapoceto_u", "TEXT"), 
        ("opis", "TEXT DEFAULT ''"), ("rutiranje", "TEXT DEFAULT 'Pogon'"), 
        ("naziv_projekta", "TEXT DEFAULT ''"), ("laser_napomena", "TEXT DEFAULT ''"), 
        ("bravarija_napomena", "TEXT DEFAULT ''"), ("debljina_ploce", "TEXT DEFAULT ''"),
        ("odabrani_laser", "TEXT DEFAULT ''"), ("kreirao", "TEXT DEFAULT ''"),
        ("dimenzije_ploce_laser", "TEXT DEFAULT ''"), ("materijal_ploce_laser", "TEXT DEFAULT ''"),
        ("zavrseno_u", "TIMESTAMP"),
        ("laser_radnik", "TEXT DEFAULT ''"), ("bravarija_radnik", "TEXT DEFAULT ''")
    ]
    for kol, tip in kolone_radni:
        try: conn.execute(f"ALTER TABLE radni_nalozi ADD COLUMN {kol} {tip}")
        except: pass

    kolone_poz = [
        ("laser_sati", "INTEGER DEFAULT 0"), ("laser_minute", "INTEGER DEFAULT 0"), 
        ("bravarija_sati", "INTEGER DEFAULT 0"), ("bravarija_minute", "INTEGER DEFAULT 0"),
        ("ciljana_kolicina", "INTEGER DEFAULT 0"), ("laser_priprema_sati", "INTEGER DEFAULT NULL"), 
        ("laser_priprema_minute", "INTEGER DEFAULT NULL"), ("laser_rezanje_sati", "INTEGER DEFAULT NULL"), 
        ("laser_rezanje_minute", "INTEGER DEFAULT NULL"), ("bravarija_priprema_sati", "INTEGER DEFAULT NULL"), 
        ("bravarija_priprema_minute", "INTEGER DEFAULT NULL"), ("bravarija_piganje_sati", "INTEGER DEFAULT NULL"), 
        ("bravarija_piganje_minute", "INTEGER DEFAULT NULL")
    ]
    for kol, tip in kolone_poz:
        try: conn.execute(f"ALTER TABLE nalog_pozicije ADD COLUMN {kol} {tip}")
        except: pass
        
    # Indeksi za munjevito pretraživanje i filtriranje
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nalozi_status ON radni_nalozi(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nalozi_kreirano ON radni_nalozi(kreirano_u)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nalozi_laser ON radni_nalozi(status, odabrani_laser)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pozicije_nalog_id ON nalog_pozicije(nalog_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obavijesti_vrijedi_do ON obavijesti(vrijedi_do)")
    except: pass
    
    conn.commit()
    conn.close()

init_db()

def formatiraj_datum(datum_str):
    if not datum_str: return ''
    try:
        d = str(datum_str).replace('T', ' ').replace('Z', '').split('.')[0]
        dt = datetime.strptime(d, '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%d.%m.%Y. u %H:%M')
    except: return str(datum_str)

def allowed_file(filename): return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def to_int(val):
    if val is None or str(val).strip() == '': return None
    try: return int(val)
    except: return None

def parsiraj_listu_datoteka(raw_data):
    if not raw_data: return []
    try:
        datoteke = json.loads(raw_data)
        if isinstance(datoteke, list): return [{"filename": d, "ext": d.split('.')[-1].upper()} for d in datoteke]
    except: pass
    return [{"filename": raw_data, "ext": raw_data.split('.')[-1].upper()}]

# --- SERVER-SENT EVENTS (SSE) STREAMING ROUTE ---
@app.route('/api/stream')
def api_stream():
    close_connection(None) # Ne zadržavaj otvorenu SQLite vezu u generatoru!
    
    def event_generator():
        lokalna_verzija = _STATE_VERSION
        zadnji_ping = time.time()
        
        yield f"event: connected\ndata: {json.dumps({'version': lokalna_verzija})}\n\n"
        
        while True:
            with _SSE_CONDITION:
                if lokalna_verzija == _STATE_VERSION:
                    _SSE_CONDITION.wait(timeout=15.0)
                trenutna_verzija = _STATE_VERSION
                trenutni_sender = _STATE_SENDER
                trenutni_uzrok = _STATE_REASON
            
            sada = time.time()
            provjeri_vanjske_promjene_datoteka()
            with _SSE_CONDITION:
                trenutna_verzija = _STATE_VERSION
                trenutni_sender = _STATE_SENDER
                trenutni_uzrok = _STATE_REASON
            
            if trenutna_verzija != lokalna_verzija:
                lokalna_verzija = trenutna_verzija
                podaci = json.dumps({
                    'version': lokalna_verzija,
                    'timestamp': sada,
                    'sender_id': trenutni_sender,
                    'reason': trenutni_uzrok
                })
                yield f"event: change\ndata: {podaci}\n\n"
            elif sada - zadnji_ping >= 15.0:
                zadnji_ping = sada
                yield f": keepalive {int(sada)}\n\n"

    return Response(event_generator(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive'
    })

@app.before_request
def provjera_pristupa():
    dozvoljeno_svima = ['/prijava_stanice', '/logo.png', '/favicon.png', '/favicon.ico', '/postavi_stanicu', '/odjava_stanice', '/login', '/logout', '/maintenance', '/skriveni_dev_login', '/api/stream']
    if any(request.path.startswith(d) for d in dozvoljeno_svima): return
    
    conn = get_db_connection()
    maint = conn.execute("SELECT vrijednost FROM postavke WHERE kljuc='maintenance'").fetchone()
    
    if maint and maint['vrijednost'] == '1':
        if request.cookies.get('pogon_role') != 'Developer':
            return redirect(url_for('maintenance'))

    z_stanica = request.cookies.get('zakljucana_stanica')
    if not z_stanica: return redirect(url_for('prijava_stanice'))
    if request.path == '/': return
    if z_stanica in ['laser1', 'laser2'] and not any(request.path.startswith(r) for r in ['/laser', '/zapocni_fazu', '/dodaj_poziciju', '/obrisi_poziciju', '/preuzmi', '/preuzmi_zip_datoteka', '/api/autosave_nalog']): return redirect('/laser')
    elif z_stanica == 'bravarija' and not any(request.path.startswith(r) for r in ['/bravarija', '/zapocni_fazu', '/dodaj_poziciju', '/obrisi_poziciju', '/preuzmi', '/preuzmi_zip_datoteka', '/api/autosave_nalog']): return redirect('/bravarija')
    elif z_stanica == 'monitor' and not any(request.path.startswith(r) for r in ['/preuzmi', '/preuzmi_zip_datoteka']): return redirect('/')

@app.after_request
def add_etag_and_cache(response):
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        if request.method == 'GET' and response.status_code == 200:
            etag = hashlib.md5(response.get_data()).hexdigest()
            response.set_etag(etag)
            if request.if_none_match and etag in request.if_none_match:
                response.status_code = 304
                response.set_data(b'')
    return response

@app.context_processor
def inject_globalne_varijable():
    lokalna_stanica = request.cookies.get('stanica_lokalno', 'dashboard')
    z_stanica = request.cookies.get('zakljucana_stanica', 'uprava')
    je_klijent = request.cookies.get('is_client') == 'true'
    ua = request.headers.get('User-Agent', '').lower()
    is_mobile = any(w in ua for w in ['mobi', 'android', 'iphone', 'ipad', 'ipod', 'windows phone'])
    
    aktivna_obavijest, aktivna_obavijest_do = None, None
    try:
        conn = get_db_connection()
        ob = conn.execute("SELECT tekst, vrijedi_do FROM obavijesti WHERE vrijedi_do > ? ORDER BY id DESC LIMIT 1", (dobij_zg_vrijeme().isoformat(),)).fetchone()
        if ob: aktivna_obavijest, aktivna_obavijest_do = ob['tekst'], ob['vrijedi_do']
    except: pass
    
    pogon_role = request.cookies.get('pogon_role')
    return dict(stanica=lokalna_stanica, prikazi_klijent_gumbe=je_klijent, is_server_window=False, z_stanica=z_stanica, is_mobile=is_mobile, aktivna_obavijest=aktivna_obavijest, aktivna_obavijest_do=aktivna_obavijest_do, pogon_role=pogon_role)

@app.route('/logo.png')
def serve_logo(): return send_from_directory(BASE_DIR, 'logo.png', max_age=86400)

@app.route('/favicon.png')
def serve_favicon(): return send_from_directory(BASE_DIR, 'favicon.png', max_age=86400)

@app.route('/favicon.ico')
def favicon_fallback(): return redirect(url_for('serve_favicon'))

@app.route('/maintenance')
def maintenance():
    conn = get_db_connection()
    maint = conn.execute("SELECT vrijednost FROM postavke WHERE kljuc='maintenance'").fetchone()
    if not maint or maint['vrijednost'] == '0': return redirect('/')
    if request.cookies.get('pogon_role') == 'Developer': return redirect('/dev_panel')
    return render_template('maintenance.html')

@app.route('/skriveni_dev_login', methods=['POST'])
def skriveni_dev_login():
    password = request.form.get('password', '')
    if hashlib.sha256(password.encode('utf-8')).hexdigest() == DEV_HASH:
        resp = make_response(redirect(url_for('dev_panel')))
        resp.set_cookie('pogon_role', 'Developer', max_age=60*60*24*365, samesite='Lax')
        return resp
    return redirect(url_for('maintenance'))

@app.route('/prijava_stanice', methods=['GET', 'POST'])
def prijava_stanice():
    conn = get_db_connection()
    prava_ip = request.headers.get('X-Forwarded-For', request.headers.get('X-Real-IP', request.remote_addr or 'nepoznato')).split(',')[0].strip()
    otisak_uređaja = hashlib.md5(f"{prava_ip}_{request.headers.get('User-Agent', 'nepoznat')}".encode('utf-8')).hexdigest()
    blokada = conn.execute("SELECT * FROM blokade WHERE ip_adresa=?", (otisak_uređaja,)).fetchone()
    
    sada = dobij_zg_vrijeme()
    blokada_aktivna = False
    if blokada and blokada['blokiran_do']:
        vrijeme_isteka = datetime.fromisoformat(blokada['blokiran_do'])
        if sada < vrijeme_isteka:
            blokada_aktivna = True
            poruka = f"Uređaj je zaključan. Pokušajte ponovno {vrijeme_isteka.strftime('%d.%m.%Y. u %H:%M')}."
        else:
            conn.execute("UPDATE blokade SET pokusaji=0, blokiran_do=NULL WHERE ip_adresa=?", (otisak_uređaja,))
            conn.commit()
            blokada = None
    poruka = None if not blokada_aktivna else poruka

    if request.method == 'POST' and not blokada_aktivna:
        uneseni_pin, odabir = request.form.get('pin', ''), request.form.get('vrsta_stanice')
        if hashlib.sha256(uneseni_pin.encode()).hexdigest() == PIN_HASH:
            conn.execute("INSERT OR REPLACE INTO blokade (ip_adresa, pokusaji, blokiran_do) VALUES (?, 0, NULL)", (otisak_uređaja,))
            conn.commit()
            cilj = '/laser' if odabir in ['laser1', 'laser2'] else '/bravarija' if odabir == 'bravarija' else '/'
            resp = make_response(redirect(cilj))
            resp.set_cookie('zakljucana_stanica', odabir, max_age=60*60*24*365, samesite='Lax')
            resp.set_cookie('stanica_lokalno', odabir if odabir not in ['uprava', 'monitor'] else 'dashboard', max_age=60*60*24*365, samesite='Lax')
            return resp
        else:
            pokusaji = (blokada['pokusaji'] if blokada else 0) + 1
            if pokusaji >= 5:
                conn.execute("INSERT OR REPLACE INTO blokade (ip_adresa, pokusaji, blokiran_do) VALUES (?, ?, ?)", (otisak_uređaja, pokusaji, (sada + timedelta(hours=24)).isoformat()))
                poruka = "ZAKLJUČANO! Previše pogrešnih unosa PIN-a. Uređaj je zaključan na 24 sata."
                blokada_aktivna = True
            else:
                conn.execute("INSERT OR REPLACE INTO blokade (ip_adresa, pokusaji, blokiran_do) VALUES (?, ?, NULL)", (otisak_uređaja, pokusaji))
                poruka = f"Neispravan sigurnosni PIN. Preostalo pokušaja: {5 - pokusaji}"
            conn.commit()
    return render_template('prijava.html', poruka=poruka, blokada_aktivna=blokada_aktivna)

@app.route('/odjava_stanice')
def odjava_stanice():
    resp = make_response(redirect(url_for('prijava_stanice')))
    resp.set_cookie('zakljucana_stanica', '', expires=0)
    resp.set_cookie('stanica_lokalno', '', expires=0)
    resp.set_cookie('pogon_role', '', expires=0)
    session.clear()
    return resp

@app.route('/ugasi_program')
def ugasi_program(): return redirect(url_for('index_pogon_hub'))

@app.route('/postavi_stanicu/<ime_stanice>')
def postavi_stanicu(ime_stanice):
    mapa_autorizacije = {'dashboard': 'uprava', 'laser1': 'laser1', 'laser2': 'laser2', 'bravarija': 'bravarija'}
    z_ime = mapa_autorizacije.get(ime_stanice, 'uprava')
    cilj = url_for('index_pogon_hub') if z_ime in ['uprava', 'monitor'] else url_for('sekcija_laser') if 'laser' in z_ime else url_for('sekcija_bravarija')
    resp = make_response(redirect(cilj))
    resp.set_cookie('stanica_lokalno', ime_stanice, max_age=60*60*24*365, samesite='Lax')
    resp.set_cookie('zakljucana_stanica', z_ime, max_age=60*60*24*365, samesite='Lax')
    if request.args.get('client') == 'true': resp.set_cookie('is_client', 'true', max_age=60*60*24*365, samesite='Lax')
    return resp

@app.route('/')
def index_pogon_hub():
    conn = get_db_connection()
    svi_nalozi = conn.execute('SELECT * FROM radni_nalozi ORDER BY id DESC').fetchall()
    trenutno_na_laseru = []
    trenutno_u_bravariji = []
    for r in svi_nalozi:
        n = dict(r)
        n['kreirano_u_prikaz'] = formatiraj_datum(n['kreirano_u'])
        if n['status'] in ['Laser', 'Pauzirano - Laser'] and (n['laser_zapoceto_u'] or n['status'] == 'Pauzirano - Laser'):
            trenutno_na_laseru.append(n)
        if n['status'] in ['Piganje', 'Pauzirano - Bravarija'] and (n['bravarija_zapoceto_u'] or n['status'] == 'Pauzirano - Bravarija'):
            trenutno_u_bravariji.append(n)
    return render_template('monitor.html', trenutno_na_laseru=trenutno_na_laseru, trenutno_u_bravariji=trenutno_u_bravariji)

@app.route('/login', methods=['GET', 'POST'])
def login():
    poruka = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        uneseni_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
        
        if username == "admin" and uneseni_hash == ADMIN_HASH:
            resp = make_response(redirect(url_for('index_master')))
            resp.set_cookie('pogon_role', 'Admin', max_age=60*60*24*365, samesite='Lax') 
            return resp
            
        elif username == "developer" and uneseni_hash == DEV_HASH:
            resp = make_response(redirect(url_for('dev_panel')))
            resp.set_cookie('pogon_role', 'Developer', max_age=60*60*24*365, samesite='Lax')
            return resp
            
        poruka = "Neispravni podaci za prijavu!"
    return render_template('login.html', poruka=poruka)

@app.route('/logout')
def logout():
    resp = make_response(redirect(url_for('login')))
    resp.set_cookie('pogon_role', '', expires=0)
    session.clear()
    return resp

@app.route('/dev_panel', methods=['GET', 'POST'])
def dev_panel():
    if request.cookies.get('pogon_role') != 'Developer': return redirect(url_for('login'))
    conn = get_db_connection()
    
    if request.method == 'POST':
        akcija = request.form.get('akcija')
        if akcija == 'lansiraj_ticker':
            tekst = request.form.get('ticker_tekst')
            trajanje_minuta = float(request.form.get('trajanje_minuta', 60))
            vrijedi_do = (dobij_zg_vrijeme() + timedelta(minutes=trajanje_minuta)).isoformat()
            conn.execute("DELETE FROM obavijesti")
            conn.execute("INSERT INTO obavijesti (tekst, vrijedi_do) VALUES (?, ?)", (tekst, vrijedi_do))
            conn.commit()
            objavi_promjenu("ticker_promijenjen")
        elif akcija == 'ugasi_ticker':
            conn.execute("DELETE FROM obavijesti")
            conn.commit()
            objavi_promjenu("ticker_ugasen")
        elif akcija == 'odblokiraj':
            ip_od = request.form.get('ip_adresa')
            conn.execute("DELETE FROM blokade WHERE ip_adresa=?", (ip_od,))
            conn.commit()
        elif akcija == 'toggle_maint':
            status = request.form.get('maint_status', '0')
            conn.execute("UPDATE postavke SET vrijednost=? WHERE kljuc='maintenance'", (status,))
            conn.commit()
            objavi_promjenu("maint_toggle")
            
        return redirect(url_for('dev_panel'))

    blokade_rows = conn.execute("SELECT * FROM blokade WHERE blokiran_do IS NOT NULL").fetchall()
    blokade = []
    for b in blokade_rows:
        bd = dict(b)
        bd['kratki_ip'] = bd['ip_adresa'][:15] if bd['ip_adresa'] else ''
        bd['prikaz_vremena'] = bd['blokiran_do'].replace('T', ' ')[:16] if bd['blokiran_do'] else ''
        blokade.append(bd)
        
    uk_naloga = conn.execute("SELECT COUNT(id) FROM radni_nalozi").fetchone()[0]
    uk_pozicija = conn.execute("SELECT COUNT(id) FROM nalog_pozicije").fetchone()[0]
    
    maint_row = conn.execute("SELECT vrijednost FROM postavke WHERE kljuc='maintenance'").fetchone()
    maint_status = maint_row['vrijednost'] if maint_row else '0'
    
    try: db_velicina_mb = round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
    except: db_velicina_mb = 0.0

    return render_template('dev_panel.html', blokade=blokade, uk_naloga=uk_naloga, uk_pozicija=uk_pozicija, db_velicina_mb=db_velicina_mb, maint_status=maint_status)

@app.route('/preuzmi_backup_baze')
def preuzmi_backup_baze():
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']:
        return redirect(url_for('login'))
    backup_path = napravi_sigurnosnu_kopiju_baze("manual")
    if backup_path and os.path.exists(backup_path):
        return send_file(backup_path, as_attachment=True, download_name=os.path.basename(backup_path))
    elif os.path.exists(DB_FILE):
        return send_file(DB_FILE, as_attachment=True, download_name=f"proizvodnja_backup_{dobij_zg_vrijeme().strftime('%Y%m%d_%H%M%S')}.db")
    return redirect(request.referrer or url_for('dev_panel'))

@app.route('/sefo_panel', methods=['GET', 'POST'])
def index_master():
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    
    # Prigušena provjera automatskog arhiviranja (jednom u 60 sekundi)
    global _ZADNJA_AUTO_ARHIVA
    zg_sada_dt = dobij_zg_vrijeme()
    if _ZADNJA_AUTO_ARHIVA is None or (zg_sada_dt - _ZADNJA_AUTO_ARHIVA).total_seconds() > 60:
        zg_sada = zg_sada_dt.strftime('%Y-%m-%d %H:%M:%S')
        granica_za_arhivu = (zg_sada_dt - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("UPDATE radni_nalozi SET status = 'Arhivirano', zavrseno_u = COALESCE(zavrseno_u, ?) WHERE status != 'Arhivirano' AND kreirano_u <= ?", (zg_sada, granica_za_arhivu,))
        conn.commit()
        _ZADNJA_AUTO_ARHIVA = zg_sada_dt
    
    if request.method == 'POST':
        req_uuid = request.form.get('request_uuid')
        if req_uuid and je_duplikat_zahtjev(req_uuid):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': True, 'duplikat': True})
            return redirect(url_for('index_master'))

        naziv = request.form['naziv_naloga']
        projekt = request.form.get('naziv_projekta', '')
        kreirao = request.form.get('kreirao', '')
        debljina_ploce = request.form.get('debljina_ploce', '')
        opis = request.form.get('opis', '')
        rutiranje = request.form.get('rutiranje', 'Pogon')
        
        p_names = []
        for f in request.files.getlist('pdf_file'):
            saved_name = spremi_upload_datoteku(f)
            if saved_name:
                p_names.append(saved_name)
        p_name_db = json.dumps(p_names) if p_names else None
            
        l_names = []
        for f in request.files.getlist('lxdf_file'):
            saved_name = spremi_upload_datoteku(f)
            if saved_name:
                l_names.append(saved_name)
        l_name_db = json.dumps(l_names) if l_names else None
        
        pocetni_status = 'Piganje' if rutiranje == 'Samo Bravarija' else 'Laser'
        vrijeme_kreiranja = dobij_zg_vrijeme().strftime('%Y-%m-%d %H:%M:%S')
        
        cursor = conn.execute('INSERT INTO radni_nalozi (naziv_naloga, naziv_projekta, debljina_ploce, pdf_datoteka, lxdf_datoteka, opis, rutiranje, status, kreirao, kreirano_u) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                     (naziv, projekt, debljina_ploce, p_name_db, l_name_db, opis, rutiranje, pocetni_status, kreirao, vrijeme_kreiranja))
        nalog_id = cursor.lastrowid
        
        try:
            raw_kosarica = request.form.get('kosarica_data', '[]')
            stavke = json.loads(raw_kosarica) if raw_kosarica else []
            if isinstance(stavke, list):
                for s in stavke:
                    if isinstance(s, dict):
                        naziv_st = str(s.get('naziv', '')).strip()
                        if naziv_st:
                            kom = to_int(s.get('komada')) or 0
                            conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, laser_skart, bravarija_komada, bravarija_skart, laser_priprema_sati, laser_priprema_minute, laser_rezanje_sati, laser_rezanje_minute, bravarija_priprema_sati, bravarija_priprema_minute, bravarija_piganje_sati, bravarija_piganje_minute) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)', 
                                         (nalog_id, naziv_st, kom))
        except Exception: pass
        conn.commit()
        objavi_promjenu("novi_nalog")
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True, 'nalog_id': nalog_id})
        return redirect(url_for('index_master'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status != 'Arhivirano' ORDER BY id DESC").fetchall()
    nalog_ids = [r['id'] for r in nalozi_rows]
    pozicije_map = dohvati_pozicije_za_naloge(conn, nalog_ids)
    
    aktivni_nalozi = []
    zavrseni_nalozi = []
    
    for r in nalozi_rows:
        n = dict(r)
        n['kreirano_u_prikaz'] = formatiraj_datum(n['kreirano_u'])
        n['zavrseno_u_prikaz'] = formatiraj_datum(n['zavrseno_u'])
        n['pozicije'] = pozicije_map.get(n['id'], [])
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        n['ukupno'] = izracunaj_ukupno_za_nalog(n['pozicije'])
        
        if n['status'] == 'Na pregledu': zavrseni_nalozi.append(n)
        else: aktivni_nalozi.append(n)
            
    return render_template('sefo_panel.html', aktivni_nalozi=aktivni_nalozi, zavrseni_nalozi=zavrseni_nalozi)

@app.route('/uredi_nalog/<int:id>', methods=['POST'])
def uredi_nalog(id):
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']:
        return redirect(url_for('login'))
        
    req_uuid = request.form.get('request_uuid')
    if req_uuid and je_duplikat_zahtjev(req_uuid):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True, 'duplikat': True})
        return redirect(url_for('index_master'))
        
    conn = get_db_connection()
    nalog = conn.execute("SELECT * FROM radni_nalozi WHERE id=?", (id,)).fetchone()
    if not nalog:
        return redirect(url_for('index_master'))
        
    naziv = request.form.get('naziv_naloga', '').strip() or nalog['naziv_naloga']
    projekt = request.form.get('naziv_projekta', '').strip()
    debljina_ploce = request.form.get('debljina_ploce', '').strip()
    rutiranje = request.form.get('rutiranje', nalog['rutiranje'])
    opis = request.form.get('opis', '').strip()
    
    postojeci_pdf = [d['filename'] for d in parsiraj_listu_datoteka(nalog['pdf_datoteka'])]
    postojeci_lxdf = [d['filename'] for d in parsiraj_listu_datoteka(nalog['lxdf_datoteka'])]
    
    for f in request.files.getlist('dodatni_pdf_file'):
        saved = spremi_upload_datoteku(f)
        if saved:
            postojeci_pdf.append(saved)
            
    for f in request.files.getlist('dodatni_lxdf_file'):
        saved = spremi_upload_datoteku(f)
        if saved:
            postojeci_lxdf.append(saved)
            
    pdf_db = json.dumps(postojeci_pdf) if postojeci_pdf else None
    lxdf_db = json.dumps(postojeci_lxdf) if postojeci_lxdf else None
    
    conn.execute("""UPDATE radni_nalozi SET 
        naziv_naloga=?, naziv_projekta=?, debljina_ploce=?, rutiranje=?, opis=?, 
        pdf_datoteka=?, lxdf_datoteka=? WHERE id=?""",
        (naziv, projekt, debljina_ploce, rutiranje, opis, pdf_db, lxdf_db, id))
        
    nova_poz = request.form.get('nova_pozicija_naziv', '').strip()
    nova_kol = to_int(request.form.get('nova_pozicija_kolicina')) or 0
    if nova_poz:
        conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, laser_skart, bravarija_komada, bravarija_skart, laser_priprema_sati, laser_priprema_minute, laser_rezanje_sati, laser_rezanje_minute, bravarija_priprema_sati, bravarija_priprema_minute, bravarija_piganje_sati, bravarija_piganje_minute) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)', 
                     (id, nova_poz, nova_kol))
                     
    conn.commit()
    objavi_promjenu("nalog_uredjen")
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'nalog_id': id})
    return redirect(url_for('index_master'))

@app.route('/vrati_u_rad/<int:id>/<string:cilj>', methods=['GET', 'POST'])
def vrati_u_rad(id, cilj):
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']:
        return redirect(url_for('login'))
        
    conn = get_db_connection()
    cilj = (cilj or '').lower()
    
    if cilj == 'laser':
        conn.execute("UPDATE radni_nalozi SET status='Pauzirano - Laser', laser_zapoceto_u=NULL WHERE id=?", (id,))
    elif cilj == 'bravarija':
        conn.execute("UPDATE radni_nalozi SET status='Pauzirano - Bravarija', bravarija_zapoceto_u=NULL WHERE id=?", (id,))
    elif cilj == 'pregled':
        conn.execute("UPDATE radni_nalozi SET status='Na pregledu' WHERE id=?", (id,))
        
    conn.commit()
    objavi_promjenu("vraceno_u_rad")
    
    if cilj == 'pregled':
        return redirect(url_for('arhiva'))
    return redirect(url_for('index_master'))

@app.route('/arhiva')
def arhiva():
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status = 'Arhivirano' ORDER BY id DESC").fetchall()
    nalog_ids = [r['id'] for r in nalozi_rows]
    pozicije_map = dohvati_pozicije_za_naloge(conn, nalog_ids)
    
    nalozi = []
    for r in nalozi_rows:
        n = dict(r)
        n['kreirano_u_prikaz'] = formatiraj_datum(n['kreirano_u'])
        n['zavrseno_u_prikaz'] = formatiraj_datum(n['zavrseno_u'])
        n['pozicije'] = pozicije_map.get(n['id'], [])
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        n['ukupno'] = izracunaj_ukupno_za_nalog(n['pozicije'])
        nalozi.append(n)
    return render_template('arhiva.html', nalozi=nalozi)

# --- SPREMANJE U STVARNOM VREMENU (REAL-TIME DB AUTO-SAVE) ---
@app.route('/api/autosave_nalog', methods=['POST'])
def api_autosave_nalog():
    id_naloga = request.form.get('id_naloga') or request.form.get('nalog_id')
    if not id_naloga:
        return {"ok": False, "error": "Nedostaje ID naloga"}, 400
    
    conn = get_db_connection()
    radnik = request.form.get('radnik', '')
    radnik_napomena = request.form.get('radnik_napomena') or request.form.get('laser_napomena') or request.form.get('bravarija_napomena') or ''
    izvor = request.form.get('izvor', '')
    sender_id = request.headers.get('X-Client-ID') or request.form.get('client_id')
    
    dim_p = request.form.get('dimenzije_ploce') or request.form.get('dimenzije_ploce_laser')
    mat_p = request.form.get('materijal_ploce') or request.form.get('materijal_ploce_laser')
    
    is_laser = (izvor == 'laser') or (dim_p is not None) or (mat_p is not None)
    
    if is_laser:
        conn.execute("UPDATE radni_nalozi SET laser_napomena=?, dimenzije_ploce_laser=COALESCE(?, dimenzije_ploce_laser), materijal_ploce_laser=COALESCE(?, materijal_ploce_laser), laser_radnik=? WHERE id=?",
                     (radnik_napomena, dim_p, mat_p, radnik, id_naloga))
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=COALESCE(NULLIF(?, ""), naziv_pozicije), laser_komada=?, laser_skart=?, laser_priprema_sati=?, laser_priprema_minute=?, laser_rezanje_sati=?, laser_rezanje_minute=?, laser_radnik=? WHERE id=?', 
                         (naziv_poz, to_int(request.form.get(f'komada_{pid}')), to_int(request.form.get(f'skart_{pid}')), to_int(request.form.get(f'priprema_sati_{pid}')), to_int(request.form.get(f'priprema_minute_{pid}')), to_int(request.form.get(f'rezanje_sati_{pid}')), to_int(request.form.get(f'rezanje_minute_{pid}')), radnik, pid))
    else:
        conn.execute("UPDATE radni_nalozi SET bravarija_napomena=?, bravarija_radnik=? WHERE id=?",
                     (radnik_napomena, radnik, id_naloga))
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=COALESCE(NULLIF(?, ""), naziv_pozicije), bravarija_komada=?, bravarija_skart=?, bravarija_priprema_sati=?, bravarija_priprema_minute=?, bravarija_piganje_sati=?, bravarija_piganje_minute=?, bravarija_radnik=? WHERE id=?', 
                         (naziv_poz, to_int(request.form.get(f'komada_{pid}')), to_int(request.form.get(f'skart_{pid}')), to_int(request.form.get(f'priprema_sati_{pid}')), to_int(request.form.get(f'priprema_minute_{pid}')), to_int(request.form.get(f'piganje_sati_{pid}')), to_int(request.form.get(f'piganje_minute_{pid}')), radnik, pid))
    
    conn.commit()
    objavi_promjenu("autosave", sender_id=sender_id)
    return {"ok": True}

# --- SPREMI ZIP (Glavni proizvodni izvještaj sa HTML-om) ---
@app.route('/preuzmi_izvjestaj/<int:id>')
def preuzmi_izvjestaj(id):
    conn = get_db_connection()
    n = conn.execute("SELECT * FROM radni_nalozi WHERE id=?", (id,)).fetchone()
    if not n:
        return redirect(url_for('index_master'))
        
    pozicije_rows = conn.execute("SELECT * FROM nalog_pozicije WHERE nalog_id=?", (id,)).fetchall()
    pozicije = [dict(p) for p in pozicije_rows]
    uk = izracunaj_ukupno_za_nalog(pozicije)
    
    firma = sigurno_ime(n['naziv_naloga']) if n['naziv_naloga'] else "Firma"
    projekt = sigurno_ime(n['naziv_projekta']) if n['naziv_projekta'] else ""
    
    dijelovi = [firma]
    if projekt:
        dijelovi.append(projekt)
    dijelovi.append(f"Izvjestaj_Nalog_{id}")
    
    glavni_folder = "_".join(dijelovi).replace("__", "_")
    predlozeno_ime_zipa = f"{glavni_folder}.zip"
    
    seen_zip_paths = set()
    def dodaj_u_zip(zf, filepath, folder, original_filename):
        clean_name = original_filename.split('_', 1)[-1] if '_' in original_filename else original_filename
        target_path = f"{glavni_folder}/{folder}/{clean_name}"
        idx = 1
        while target_path in seen_zip_paths:
            base, ext = os.path.splitext(clean_name)
            target_path = f"{glavni_folder}/{folder}/{base}_{idx}{ext}"
            idx += 1
        seen_zip_paths.add(target_path)
        zf.write(filepath, target_path)

    memory_file = io.BytesIO()
    try:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            pdf_datoteke = parsiraj_listu_datoteka(n['pdf_datoteka'])
            for pdf in pdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], pdf['filename'])
                if os.path.exists(fpath): 
                    dodaj_u_zip(zf, fpath, "PDF_Datoteke", pdf['filename'])
            
            lxdf_datoteke = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            for lx in lxdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], lx['filename'])
                if os.path.exists(fpath): 
                    dodaj_u_zip(zf, fpath, "DXF_Datoteke", lx['filename'])
            
            logo_base64, logo_ext = get_cached_logo()
                    
            report_html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Proizvodni Izvještaj - Nalog #{id}</title><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"><style>body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; color: #1a202c; margin: 0; padding: 30px; }} .wrapper {{ max-width: 900px; background: #ffffff; margin: 0 auto; padding: 40px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.05); border-top: 8px solid #dc3545; }} .logo-container {{ background-color: #ffffff; padding: 10px; border-radius: 8px; display: inline-flex; align-items: center; }} .logo-img {{ filter: brightness(1.35) saturate(1.25) contrast(1.1); max-height: 55px; width: auto; }} .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; margin-bottom: 25px; }} .meta-title {{ font-size: 14px; text-transform: uppercase; color: #64748b; text-align: right; font-weight: bold; letter-spacing: 1px; line-height: 1.4; }} .info-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 30px; }} .info-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #0dcaf0; padding: 14px 18px; border-radius: 8px; }} .info-card h3 {{ margin: 0 0 4px 0; font-size: 11px; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px; }} .info-card p {{ margin: 0; font-size: 15px; font-weight: 600; color: #0f172a; }} .napomena-kontejner {{ margin-bottom: 30px; }} .napomena-card {{ padding: 15px 18px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid #dc3545; background: #fffafb; border: 1px solid #fecdd3; border-right: 1px solid #fecdd3; border-bottom: 1px solid #fecdd3; }} .napomena-card.bravarija {{ border-left-color: #ea580c; background: #fffdfa; border-color: #ffedd5; }} .napomena-card.poslovođa {{ border-left-color: #0dcaf0; background: #f0fdfa; border-color: #ccfbf1; }} .napomena-card h4 {{ margin: 0 0 6px 0; font-size: 12px; text-transform: uppercase; color: #475569; }} .napomena-card p {{ margin: 0; font-size: 14px; font-weight: 500; color: #1e293b; white-space: pre-wrap; }} .table-section {{ margin-top: 30px; }} .table-section h2 {{ font-size: 18px; font-weight: 700; color: #0f172a; margin-bottom: 15px; padding-bottom: 6px; border-bottom: 2px solid #e2e8f0; }} table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }} th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 12px 14px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1; }} td {{ padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; vertical-align: middle; color: #334155; }} tr:nth-child(even) td {{ background: #f8fafc; }} .badge {{ display: inline-block; padding: 4px 8px; font-size: 11px; font-weight: 700; border-radius: 4px; text-transform: uppercase; margin-right: 4px; }} .badge-info {{ background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }} .badge-success {{ background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }} .badge-danger {{ background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }} .radnik-info {{ font-size: 12px; color: #64748b; margin-top: 4px; line-height: 1.4; }} .footer {{ text-align: center; margin-top: 40px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 15px; }} @media print {{ @page {{ margin: 1cm; size: A4 portrait; }} body {{ background-color: #ffffff !important; padding: 0; color: #000; }} .wrapper {{ box-shadow: none; padding: 0; border-top: none; max-width: 100%; }} .btn, .navbar {{ display: none !important; }} * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }} }}</style></head><body><div class="wrapper"><div class="header"><div class="logo-container">{"<img src='data:image/" + logo_ext + ";base64," + logo_base64 + "' class='logo-img' alt='FirstCutLaser'>" if logo_base64 else "<h1 style='color:#dc3545; margin:0;'>FIRST CUT LASER</h1>"}</div><div class="meta-title">Arhivski Izvještaj Pogona<br><span style="color:#0f172a; font-size:16px;">Nalog #{id}</span></div></div><div class="info-grid"><div class="info-card"><h3>Kupac / Partner</h3><p>{html.escape(str(n['naziv_naloga'] or '-'))}</p></div><div class="info-card"><h3>Naziv Projekta</h3><p>{html.escape(str(n['naziv_projekta'] or '-'))}</p></div><div class="info-card"><h3>Kreator Naloga</h3><p>{html.escape(str(n['kreirao'] or '-'))}</p></div><div class="info-card"><h3>Proizvodna Ruta</h3><p>{html.escape(str(n['rutiranje'] or '-'))}</p></div><div class="info-card"><h3>Debljina Materijala</h3><p>{html.escape(str(n['debljina_ploce'] or '-'))}</p></div><div class="info-card"><h3>Dimenzije uložene ploče</h3><p>{html.escape(str(n['dimenzije_ploce_laser'] or '-'))}</p></div><div class="info-card"><h3>Materijal ploče</h3><p>{html.escape(str(n['materijal_ploce_laser'] or '-'))}</p></div></div><div class="napomena-kontejner">{"<div class='napomena-card poslovođa'><h4><i class='fa-solid fa-user-tie'></i> Upute Poslovođe</h4><p>" + html.escape(str(n['opis'])) + "</p></div>" if n['opis'] else ""}{"<div class='napomena-card'><h4><i class='fa-solid fa-fire'></i> Napomena s Lasera</h4><p>" + html.escape(str(n['laser_napomena'])) + "</p></div>" if n['laser_napomena'] else ""}{"<div class='napomena-card bravarija'><h4><i class='fa-solid fa-hammer'></i> Napomena iz Bravarije</h4><p>" + html.escape(str(n['bravarija_napomena'])) + "</p></div>" if n['bravarija_napomena'] else ""}</div><div class="table-section"><h2>Specifikacija Izvršenih Pozicija</h2><table><thead><tr><th>Naziv Pozicije s Nacrta</th><th>Faza 1: Rezanje (Laser)</th><th>Faza 2: Bravarska Obrada</th></tr></thead><tbody>"""
            for p in pozicije:
                p_cilj = p['ciljana_kolicina'] if p['ciljana_kolicina'] is not None else 0
                l_kom = p['laser_komada'] if p['laser_komada'] is not None else 0
                l_skart = p['laser_skart'] if p['laser_skart'] is not None else 0
                l_psati = p['laser_priprema_sati'] if p['laser_priprema_sati'] is not None else 0
                l_pmin = p['laser_priprema_minute'] if p['laser_priprema_minute'] is not None else 0
                l_rsati = p['laser_rezanje_sati'] if p['laser_rezanje_sati'] is not None else 0
                l_rmin = p['laser_rezanje_minute'] if p['laser_rezanje_minute'] is not None else 0
                
                b_kom = p['bravarija_komada'] if p['bravarija_komada'] is not None else 0
                b_skart = p['bravarija_skart'] if p['bravarija_skart'] is not None else 0
                b_psati = p['bravarija_priprema_sati'] if p['bravarija_priprema_sati'] is not None else 0
                b_pmin = p['bravarija_priprema_minute'] if p['bravarija_priprema_minute'] is not None else 0
                b_rsati = p['bravarija_piganje_sati'] if p['bravarija_piganje_sati'] is not None else 0
                b_rmin = p['bravarija_piganje_minute'] if p['bravarija_piganje_minute'] is not None else 0
                
                l_radnik = html.escape(str(p['laser_radnik'] or n['laser_radnik'] or '-'))
                b_radnik = html.escape(str(p['bravarija_radnik'] or n['bravarija_radnik'] or '-'))
                
                vrijeme_laser = f"Priprema: {l_psati}h {l_pmin}m<br>Rezanje: {l_rsati}h {l_rmin}m"
                vrijeme_bravarija = f"Priprema: {b_psati}h {b_pmin}m<br>Piganje: {b_rsati}h {b_rmin}m"
                
                report_html += f"""<tr><td style="font-weight: 700; color: #0f172a; font-size: 15px;">{html.escape(str(p['naziv_pozicije']))}</td><td><div><span class="badge badge-info">POTREBNO: {p_cilj} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {l_kom} KOM</span><span class="badge badge-danger">{l_skart} ŠKART</span></div><div class="radnik-info">{vrijeme_laser}<br>Radnik: {l_radnik}</div></td><td><div><span class="badge badge-info">POTREBNO: {p_cilj} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {b_kom} KOM</span><span class="badge badge-danger">{b_skart} ŠKART</span></div><div class="radnik-info">{vrijeme_bravarija}<br>Radnik: {b_radnik}</div></td></tr>"""
            report_html += f"""</tbody><tfoot style="background: #f1f5f9; font-weight: bold; border-top: 2px solid #cbd5e1;"><tr><td style="padding: 12px 14px; font-weight: 700;">UKUPNO ZBROJ:</td><td style="padding: 12px 14px;"><div><span class="badge badge-info">CILJ: {uk['cilj_ukupno']} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {uk['laser_komada_ukupno']} KOM</span><span class="badge badge-danger">{uk['laser_skart_ukupno']} ŠKART</span></div><div class="radnik-info">Ukupno laser: {uk['laser_ukupno_tekst']}</div></td><td style="padding: 12px 14px;"><div><span class="badge badge-info">CILJ: {uk['cilj_ukupno']} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {uk['bravarija_komada_ukupno']} KOM</span><span class="badge badge-danger">{uk['bravarija_skart_ukupno']} ŠKART</span></div><div class="radnik-info">Ukupno bravarija: {uk['bravarija_ukupno_tekst']}</div></td></tr><tr style="background: #e2e8f0; font-size: 13px;"><td colspan="3" style="padding: 10px 14px; text-align: right; color: #0f172a;"><strong>SVEUKUPNO UTROŠENO VRIJEME NA NALOGU: {uk['sveukupno_vrijeme_tekst']}</strong></td></tr></tfoot></table></div><div class="footer">Sustav First Cut Laser d.o.o. &bull; Izvještaj generiran: {dobij_zg_vrijeme().strftime('%d.%m.%Y. u %H:%M')}</div></div></body></html>"""
            zf.writestr(f"{glavni_folder}/Pregled_Naloga_{id}.html", report_html.encode('utf-8'))
    except Exception as e: pass
    
    memory_file.seek(0)
    return send_file(memory_file, download_name=predlozeno_ime_zipa, as_attachment=True)

# --- PREUZMI ZIP DATOTEKA (Samo PDF i DXF datoteke) ---
@app.route('/preuzmi_zip_datoteka/<int:id>')
def preuzmi_zip_datoteka(id):
    conn = get_db_connection()
    n = conn.execute("SELECT * FROM radni_nalozi WHERE id=?", (id,)).fetchone()
    if not n:
        return redirect(request.referrer or url_for('index_master'))
        
    firma = sigurno_ime(n['naziv_naloga']) if n['naziv_naloga'] else "Firma"
    projekt = sigurno_ime(n['naziv_projekta']) if n['naziv_projekta'] else ""
    
    dijelovi = [firma]
    if projekt:
        dijelovi.append(projekt)
    dijelovi.append("PDF_DXF_Datoteke")
    
    glavni_folder = "_".join(dijelovi).replace("__", "_")
    predlozeno_ime_zipa = f"{glavni_folder}.zip"
    
    seen_zip_paths = set()
    def dodaj_u_zip(zf, filepath, folder, original_filename):
        clean_name = original_filename.split('_', 1)[-1] if '_' in original_filename else original_filename
        target_path = f"{glavni_folder}/{folder}/{clean_name}"
        idx = 1
        while target_path in seen_zip_paths:
            base, ext = os.path.splitext(clean_name)
            target_path = f"{glavni_folder}/{folder}/{base}_{idx}{ext}"
            idx += 1
        seen_zip_paths.add(target_path)
        zf.write(filepath, target_path)

    memory_file = io.BytesIO()
    try:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            pdf_datoteke = parsiraj_listu_datoteka(n['pdf_datoteka'])
            for pdf in pdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], pdf['filename'])
                if os.path.exists(fpath): 
                    dodaj_u_zip(zf, fpath, "PDF_Datoteke", pdf['filename'])
                    
            lxdf_datoteke = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            for lx in lxdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], lx['filename'])
                if os.path.exists(fpath): 
                    dodaj_u_zip(zf, fpath, "DXF_Datoteke", lx['filename'])
    except Exception as e:
        pass
    
    memory_file.seek(0)
    return send_file(memory_file, download_name=predlozeno_ime_zipa, as_attachment=True)

@app.route('/arhiviraj_nalog/<int:id>')
def arhiviraj_nalog(id):
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    trenutno_vrijeme = dobij_zg_vrijeme().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("UPDATE radni_nalozi SET status='Arhivirano', zavrseno_u=COALESCE(zavrseno_u, ?) WHERE id=?", (trenutno_vrijeme, id))
    conn.commit()
    objavi_promjenu("arhivirano")
    return redirect(url_for('index_master'))

@app.route('/obrisi/<int:id>')
def obrisi_nalog(id):
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    n = conn.execute('SELECT pdf_datoteka, lxdf_datoteka FROM radni_nalozi WHERE id=?', (id,)).fetchone()
    if n:
        for pdf in parsiraj_listu_datoteka(n['pdf_datoteka']):
            fpath = os.path.join(UPLOAD_FOLDER, pdf['filename'])
            if os.path.exists(fpath): 
                try: os.remove(fpath)
                except: pass
        for lx in parsiraj_listu_datoteka(n['lxdf_datoteka']):
            fpath = os.path.join(UPLOAD_FOLDER, lx['filename'])
            if os.path.exists(fpath): 
                try: os.remove(fpath)
                except: pass
    
    conn.execute('DELETE FROM radni_nalozi WHERE id=?', (id,))
    conn.execute('DELETE FROM nalog_pozicije WHERE nalog_id=?', (id,))
    conn.commit()
    objavi_promjenu("obrisan_nalog")
    return redirect(request.referrer or url_for('index_master'))

@app.route('/masovno_obrisi', methods=['POST'])
def masovno_obrisi():
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    id_list = request.form.getlist('nalog_ids')
    conn = get_db_connection()
    for n_id in id_list:
        n = conn.execute('SELECT pdf_datoteka, lxdf_datoteka FROM radni_nalozi WHERE id=?', (n_id,)).fetchone()
        if n:
            for pdf in parsiraj_listu_datoteka(n['pdf_datoteka']):
                fpath = os.path.join(UPLOAD_FOLDER, pdf['filename'])
                if os.path.exists(fpath): 
                    try: os.remove(fpath)
                    except: pass
            for lx in parsiraj_listu_datoteka(n['lxdf_datoteka']):
                fpath = os.path.join(UPLOAD_FOLDER, lx['filename'])
                if os.path.exists(fpath): 
                    try: os.remove(fpath)
                    except: pass
        conn.execute('DELETE FROM radni_nalozi WHERE id=?', (n_id,))
        conn.execute('DELETE FROM nalog_pozicije WHERE nalog_id=?', (n_id,))
    conn.commit()
    objavi_promjenu("masovno_obrisano")
    return redirect(request.referrer or url_for('arhiva'))

@app.route('/preuzmi/<filename>')
def download_file(filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if request.args.get('dl') == '1' or ext in ['dxf', 'lxds']:
        clean_name = filename.split('_', 1)[-1] if '_' in filename else filename
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True, download_name=clean_name)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=False)

@app.route('/zapocni_fazu/<int:id>/<string:faza>')
def zapocni_fazu(id, faza):
    faza = (faza or '').lower()
    trenutno_vrijeme = dobij_zg_vrijeme().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    
    if faza == 'laser': 
        stanica = request.cookies.get('stanica_lokalno', 'dashboard')
        if stanica == 'laser1':
            ime_lasera = 'Laser 1'
        elif stanica == 'laser2':
            ime_lasera = 'Laser 2'
        else:
            l1_busy = conn.execute("SELECT id FROM radni_nalozi WHERE status='Laser' AND odabrani_laser='Laser 1' AND laser_zapoceto_u IS NOT NULL").fetchone()
            l2_busy = conn.execute("SELECT id FROM radni_nalozi WHERE status='Laser' AND odabrani_laser='Laser 2' AND laser_zapoceto_u IS NOT NULL").fetchone()
            if not l1_busy:
                ime_lasera = 'Laser 1'
            elif not l2_busy:
                ime_lasera = 'Laser 2'
            else:
                ime_lasera = 'Laser 1'
        
        aktivni = conn.execute("SELECT id FROM radni_nalozi WHERE status='Laser' AND odabrani_laser=? AND laser_zapoceto_u IS NOT NULL", (ime_lasera,)).fetchone()
        if aktivni:
            return redirect(url_for('sekcija_laser', err='zauzeto'))
            
        conn.execute("UPDATE radni_nalozi SET status='Laser', laser_zapoceto_u=?, odabrani_laser=? WHERE id=?", (trenutno_vrijeme, ime_lasera, id))
        
    elif faza == 'bravarija': 
        aktivni = conn.execute("SELECT id FROM radni_nalozi WHERE status='Piganje' AND bravarija_zapoceto_u IS NOT NULL").fetchone()
        if aktivni:
            return redirect(url_for('sekcija_bravarija', err='zauzeto'))
            
        conn.execute("UPDATE radni_nalozi SET status='Piganje', bravarija_zapoceto_u=? WHERE id=?", (trenutno_vrijeme, id))
        
    conn.commit()
    objavi_promjenu("zapoceta_faza")
    return redirect(url_for(f'sekcija_{faza}'))

@app.route('/laser', methods=['GET', 'POST'])
def sekcija_laser():
    conn = get_db_connection()
    if request.method == 'POST':
        req_uuid = request.form.get('request_uuid')
        if req_uuid and je_duplikat_zahtjev(req_uuid):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': True, 'duplikat': True})
            return redirect(url_for('sekcija_laser'))
            
        id_naloga = request.form['id_naloga']
        radnik = request.form.get('radnik', '')
        radnik_napomena = request.form.get('radnik_napomena', '')
        dimenzije_ploce = request.form.get('dimenzije_ploce', '')
        materijal_ploce = request.form.get('materijal_ploce', '')
        akcija = request.form.get('akcija', 'zavrsi_odmah')
        
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=COALESCE(NULLIF(?, ""), naziv_pozicije), laser_komada=?, laser_skart=?, laser_priprema_sati=?, laser_priprema_minute=?, laser_rezanje_sati=?, laser_rezanje_minute=?, laser_radnik=? WHERE id=?', 
                         (naziv_poz, to_int(request.form.get(f'komada_{pid}')), to_int(request.form.get(f'skart_{pid}')), to_int(request.form.get(f'priprema_sati_{pid}')), to_int(request.form.get(f'priprema_minute_{pid}')), to_int(request.form.get(f'rezanje_sati_{pid}')), to_int(request.form.get(f'rezanje_minute_{pid}')), radnik, pid))
        
        if akcija == 'pauziraj':
            conn.execute("UPDATE radni_nalozi SET status='Pauzirano - Laser', laser_zapoceto_u=NULL, laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=?, laser_radnik=? WHERE id=?", (radnik_napomena, dimenzije_ploce, materijal_ploce, radnik, id_naloga))
        else:
            novi_status = 'Piganje' if akcija == 'bravarija' else 'Na pregledu'
            vrijeme_zav = dobij_zg_vrijeme().strftime('%Y-%m-%d %H:%M:%S') if novi_status == 'Na pregledu' else None
            if vrijeme_zav:
                conn.execute("UPDATE radni_nalozi SET status=?, laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=?, laser_radnik=?, zavrseno_u=? WHERE id=?", (novi_status, radnik_napomena, dimenzije_ploce, materijal_ploce, radnik, vrijeme_zav, id_naloga))
            else:
                conn.execute("UPDATE radni_nalozi SET status=?, laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=?, laser_radnik=? WHERE id=?", (novi_status, radnik_napomena, dimenzije_ploce, materijal_ploce, radnik, id_naloga))
        conn.commit()
        objavi_promjenu("laser_zavrseno")
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True})
        return redirect(url_for('sekcija_laser'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status IN ('Laser', 'Pauzirano - Laser')").fetchall()
    nalog_ids = [r['id'] for r in nalozi_rows]
    pozicije_map = dohvati_pozicije_za_naloge(conn, nalog_ids)
    
    nalozi = []
    z_stanica = request.cookies.get('zakljucana_stanica', 'uprava')
    
    for r in nalozi_rows:
        n = dict(r)
        prikazi = True
        if n['laser_zapoceto_u']:
            if z_stanica == 'laser1' and n['odabrani_laser'] != 'Laser 1': prikazi = False
            elif z_stanica == 'laser2' and n['odabrani_laser'] != 'Laser 2': prikazi = False
        if prikazi:
            n['pozicije'] = pozicije_map.get(n['id'], [])
            n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
            n['ukupno'] = izracunaj_ukupno_za_nalog(n['pozicije'])
            nalozi.append(n)
            
    return render_template('laser.html', nalozi=nalozi)

@app.route('/bravarija', methods=['GET', 'POST'])
def sekcija_bravarija():
    conn = get_db_connection()
    if request.method == 'POST':
        req_uuid = request.form.get('request_uuid')
        if req_uuid and je_duplikat_zahtjev(req_uuid):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': True, 'duplikat': True})
            return redirect(url_for('sekcija_bravarija'))
            
        id_naloga = request.form['id_naloga']
        radnik = request.form.get('radnik', '')
        radnik_napomena = request.form.get('radnik_napomena', '')
        akcija = request.form.get('akcija', 'zavrsi_odmah')
        
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=COALESCE(NULLIF(?, ""), naziv_pozicije), bravarija_komada=?, bravarija_skart=?, bravarija_priprema_sati=?, bravarija_priprema_minute=?, bravarija_piganje_sati=?, bravarija_piganje_minute=?, bravarija_radnik=? WHERE id=?', 
                         (naziv_poz, to_int(request.form.get(f'komada_{pid}')), to_int(request.form.get(f'skart_{pid}')), to_int(request.form.get(f'priprema_sati_{pid}')), to_int(request.form.get(f'priprema_minute_{pid}')), to_int(request.form.get(f'piganje_sati_{pid}')), to_int(request.form.get(f'piganje_minute_{pid}')), radnik, pid))
        
        if akcija == 'pauziraj':
            conn.execute("UPDATE radni_nalozi SET status='Pauzirano - Bravarija', bravarija_zapoceto_u=NULL, bravarija_napomena=?, bravarija_radnik=? WHERE id=?", (radnik_napomena, radnik, id_naloga))
        else:
            vrijeme_zav = dobij_zg_vrijeme().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("UPDATE radni_nalozi SET status='Na pregledu', bravarija_napomena=?, bravarija_radnik=?, zavrseno_u=? WHERE id=?", (radnik_napomena, radnik, vrijeme_zav, id_naloga))
                         
        conn.commit()
        objavi_promjenu("bravarija_zavrseno")
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True})
        return redirect(url_for('sekcija_bravarija'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status IN ('Piganje', 'Pauzirano - Bravarija')").fetchall()
    nalog_ids = [r['id'] for r in nalozi_rows]
    pozicije_map = dohvati_pozicije_za_naloge(conn, nalog_ids)
    
    nalozi = []
    for r in nalozi_rows:
        n = dict(r)
        n['pozicije'] = pozicije_map.get(n['id'], [])
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        n['ukupno'] = izracunaj_ukupno_za_nalog(n['pozicije'])
        nalozi.append(n)
        
    return render_template('bravarija.html', nalozi=nalozi)

@app.route('/dodaj_poziciju/<int:nalog_id>/<string:izvor>', methods=['POST'])
def dodaj_poziciju(nalog_id, izvor):
    naziv = request.form.get('novi_naziv_pozicije', '').strip()
    if not naziv: naziv = f"Pozicija-{dobij_zg_vrijeme().strftime('%H%M%S')}"
    conn = get_db_connection()
    nalog = conn.execute("SELECT laser_radnik, bravarija_radnik FROM radni_nalozi WHERE id=?", (nalog_id,)).fetchone()
    l_radnik = nalog['laser_radnik'] if nalog and nalog['laser_radnik'] else ''
    b_radnik = nalog['bravarija_radnik'] if nalog and nalog['bravarija_radnik'] else ''
    conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, laser_skart, bravarija_komada, bravarija_skart, laser_priprema_sati, laser_priprema_minute, laser_rezanje_sati, laser_rezanje_minute, bravarija_priprema_sati, bravarija_priprema_minute, bravarija_piganje_sati, bravarija_piganje_minute, laser_radnik, bravarija_radnik) VALUES (?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)', (nalog_id, naziv, l_radnik, b_radnik))
    conn.commit()
    objavi_promjenu("dodana_pozicija")
    return redirect(url_for(f'sekcija_{izvor}'))

@app.route('/obrisi_poziciju/<int:poz_id>/<string:izvor>')
def obrisi_poziciju(poz_id, izvor):
    conn = get_db_connection()
    conn.execute('DELETE FROM nalog_pozicije WHERE id=?', (poz_id,))
    conn.commit()
    objavi_promjenu("obrisana_pozicija")
    return redirect(url_for(f'sekcija_{izvor}'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
