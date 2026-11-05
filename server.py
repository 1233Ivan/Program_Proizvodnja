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
import re
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

_MATERIJAL_MAPA = {
    'celik': 'Čelik',
    'čelik': 'Čelik',
    'celici': 'Čelici',
    'čelici': 'Čelici',
    'crno': 'Crno',
    'crni': 'Crni',
    'inox': 'INOX',
    'prokrom': 'Prokrom',
    'prohrom': 'Prohrom',
    'hardox': 'Hardox',
    'strenx': 'Strenx',
    'domex': 'Domex',
    'weldox': 'Weldox',
    'aluminij': 'Aluminij',
    'alu': 'Alu',
    'pocincano': 'Pocinčano',
    'pocinčano': 'Pocinčano',
    'pocincani': 'Pocinčani',
    'pocinčani': 'Pocinčani',
    'bakar': 'Bakar',
    'mesing': 'Mesing',
    'bronca': 'Bronca',
    'corten': 'Corten',
    'kortan': 'Corten',
    'titan': 'Titan',
    'plastika': 'Plastika',
    'guma': 'Guma',
}

def normaliziraj_pojedinu_stavku_ploce_ili_materijala(stavka, je_materijal=False):
    """Pretvara nazive materijala (npr. 'celik' -> 'Čelik', 'hardox' -> 'Hardox', 'inox' -> 'INOX') u uredan zapis."""
    if not stavka:
        return ''
    s = str(stavka).strip()
    if not s:
        return ''
    
    dijelovi_kosa = [d.strip() for d in s.split('/')]
    obradeni_dijelovi_kosa = []
    for dio in dijelovi_kosa:
        # Razdvoji ako je materijal zalijepljen direktno uz dimenziju (npr. '3000x1500x5hardox' -> '3000x1500x5 hardox')
        dio_razdvojen = re.sub(r'(\d+(?:[.,]\d+)?(?:[x×*]\d+(?:[.,]\d+)?)*(?:mm|cm|m)?)([a-zčćžšđ]{3,})\b', r'\1 \2', dio, flags=re.IGNORECASE)
        rijeci = dio_razdvojen.split()
        nove_rijeci = []
        for idx, r in enumerate(rijeci):
            r_clean = r.strip()
            r_low = r_clean.lower()
            
            if r_low in _MATERIJAL_MAPA:
                nove_rijeci.append(_MATERIJAL_MAPA[r_low])
                continue
            
            m_spojeno = re.match(r'^([a-zčćžšđ]+)(\d+.*)$', r_low)
            if m_spojeno and m_spojeno.group(1) in _MATERIJAL_MAPA:
                pref = _MATERIJAL_MAPA[m_spojeno.group(1)]
                suf = m_spojeno.group(2).upper() if m_spojeno.group(2).isalnum() else m_spojeno.group(2)
                nove_rijeci.append(f"{pref}{suf}")
                continue
            
            if re.match(r'^(s\d{3}[a-z0-9]*|a\d{3}[a-z0-9]*|dd11|dc01|dc04)$', r_low):
                nove_rijeci.append(r_clean.upper())
                continue
            
            if re.match(r'^\d+(?:[.,]\d+)?(?:[x×*]\d+(?:[.,]\d+)?)+(?:mm|cm|m)?$', r_low) or re.match(r'^\d+(?:[.,]\d+)?(?:mm|cm|m|kom)$', r_low):
                nove_rijeci.append(re.sub(r'[X*]', 'x', r_clean).replace('MM', 'mm'))
                continue
            
            if je_materijal or idx > 0 or not any(ch.isdigit() for ch in r_clean):
                if r_low not in ('mm', 'cm', 'm', 'kom', 'x', 'i', 'ili', 'za', 'na', 'po', 'od', 'do', 'u', 's', 'sa'):
                    nove_rijeci.append(r_clean[0].upper() + r_clean[1:])
                else:
                    nove_rijeci.append(r_low)
            else:
                nove_rijeci.append(r_clean)
        obradeni_dijelovi_kosa.append(' '.join(nove_rijeci))
    return ' / '.join(obradeni_dijelovi_kosa)

def normaliziraj_popis_ploca_ili_materijala(tekst, je_materijal=False):
    if tekst is None:
        return None
    stavke = [normaliziraj_pojedinu_stavku_ploce_ili_materijala(x, je_materijal) for x in str(tekst).split(',')]
    stavke = [x for x in stavke if x]
    return ', '.join(stavke)

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

    # Automatska normalizacija postojećih naziva ploča i materijala u bazi (npr. 'celik' -> 'Čelik', 'hardox' -> 'Hardox')
    try:
        rows_norm = conn.execute("SELECT id, debljina_ploce, dimenzije_ploce_laser, materijal_ploce_laser FROM radni_nalozi").fetchall()
        for rn in rows_norm:
            stari_deb = rn['debljina_ploce'] or ''
            stari_dim = rn['dimenzije_ploce_laser'] or ''
            stari_mat = rn['materijal_ploce_laser'] or ''
            novi_deb = normaliziraj_popis_ploca_ili_materijala(stari_deb, False) if stari_deb else ''
            novi_dim = normaliziraj_popis_ploca_ili_materijala(stari_dim, False) if stari_dim else ''
            novi_mat = normaliziraj_popis_ploca_ili_materijala(stari_mat, True) if stari_mat else ''
            if novi_deb != stari_deb or novi_dim != stari_dim or novi_mat != stari_mat:
                conn.execute("UPDATE radni_nalozi SET debljina_ploce=?, dimenzije_ploce_laser=?, materijal_ploce_laser=? WHERE id=?", (novi_deb, novi_dim, novi_mat, rn['id']))
    except Exception:
        pass
    
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

def to_int_or_zero(val):
    res = to_int(val)
    return 0 if res is None else res

def parsiraj_listu_datoteka(raw_data):
    if not raw_data: return []
    try:
        datoteke = json.loads(raw_data)
        if isinstance(datoteke, list): return [{"filename": d, "ext": d.split('.')[-1].upper()} for d in datoteke]
    except: pass
    return [{"filename": raw_data, "ext": raw_data.split('.')[-1].upper()}]

# --- ULTRA-BRZA PROVJERA VERZIJE STANJA U RAM-U (0ms BLOKIRANJA KONEKCIJA) ---
@app.route('/api/state_version')
def api_state_version():
    close_connection(None)
    provjeri_vanjske_promjene_datoteka()
    with _SSE_CONDITION:
        v = _STATE_VERSION
        s = _STATE_SENDER
        r = _STATE_REASON
    resp = jsonify({
        'version': v,
        'sender_id': s,
        'reason': r
    })
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

@app.route('/api/stream')
def api_stream():
    close_connection(None)
    provjeri_vanjske_promjene_datoteka()
    with _SSE_CONDITION:
        v = _STATE_VERSION
        s = _STATE_SENDER
        r = _STATE_REASON
    podaci = json.dumps({
        'version': v,
        'sender_id': s,
        'reason': r
    })
    return Response(f"retry: 1500\nevent: connected\ndata: {podaci}\n\n", mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no'
    })

@app.before_request
def provjera_pristupa():
    dozvoljeno_svima = ['/prijava_stanice', '/logo.png', '/favicon.png', '/favicon.ico', '/postavi_stanicu', '/odjava_stanice', '/login', '/logout', '/maintenance', '/skriveni_dev_login', '/api/stream', '/api/state_version']
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
        debljina_ploce = normaliziraj_popis_ploca_ili_materijala(request.form.get('debljina_ploce', ''), False)
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
    debljina_ploce = normaliziraj_popis_ploca_ili_materijala(request.form.get('debljina_ploce', '').strip(), False)
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
    
    dim_p = request.form.get('dimenzije_ploce') if 'dimenzije_ploce' in request.form else request.form.get('dimenzije_ploce_laser')
    mat_p = request.form.get('materijal_ploce') if 'materijal_ploce' in request.form else request.form.get('materijal_ploce_laser')
    if dim_p is not None:
        dim_p = normaliziraj_popis_ploca_ili_materijala(dim_p, False)
    if mat_p is not None:
        mat_p = normaliziraj_popis_ploca_ili_materijala(mat_p, True)
    
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

def generiraj_html_izvjestaja(conn, id, online_pregled=False, auto_print=False):
    n_row = conn.execute("SELECT * FROM radni_nalozi WHERE id=?", (id,)).fetchone()
    if not n_row:
        return None, None, None, None
    n = dict(n_row)
    pozicije_rows = conn.execute("SELECT * FROM nalog_pozicije WHERE nalog_id=?", (id,)).fetchall()
    pozicije = [dict(p) for p in pozicije_rows]
    uk = izracunaj_ukupno_za_nalog(pozicije)

    logo_base64, logo_ext = get_cached_logo()
    dim_lista = [normaliziraj_pojedinu_stavku_ploce_ili_materijala(x, False) for x in (n.get('dimenzije_ploce_laser') or '').split(',') if x.strip()]
    mat_lista = [normaliziraj_pojedinu_stavku_ploce_ili_materijala(x, True) for x in (n.get('materijal_ploce_laser') or '').split(',') if x.strip()]

    glavni_laser_radnik = n.get('laser_radnik') or next((p.get('laser_radnik') for p in pozicije if p.get('laser_radnik')), None) or '-'
    glavni_bravarija_radnik = n.get('bravarija_radnik') or next((p.get('bravarija_radnik') for p in pozicije if p.get('bravarija_radnik')), None) or '-'

    html_izvjestaj = render_template(
        'izvjestaj_naloga.html',
        n=n,
        pozicije=pozicije,
        uk=uk,
        logo_base64=logo_base64,
        logo_ext=logo_ext,
        dim_lista=dim_lista,
        mat_lista=mat_lista,
        glavni_laser_radnik=glavni_laser_radnik,
        glavni_bravarija_radnik=glavni_bravarija_radnik,
        kreirano_prikaz=formatiraj_datum(n.get('kreirano_u')),
        zavrseno_prikaz=formatiraj_datum(n.get('zavrseno_u')),
        pdf_datoteke=parsiraj_listu_datoteka(n.get('pdf_datoteka')),
        lxdf_datoteke=parsiraj_listu_datoteka(n.get('lxdf_datoteka')),
        generirano_u=dobij_zg_vrijeme().strftime('%d.%m.%Y. u %H:%M'),
        online_pregled=online_pregled,
        auto_print=auto_print
    )
    return n, pozicije, uk, html_izvjestaj

@app.route('/izvjestaj_pdf/<int:id>')
def izvjestaj_pdf(id):
    conn = get_db_connection()
    auto_print = request.args.get('print') == '1'
    n, pozicije, uk, html_izvjestaj = generiraj_html_izvjestaja(conn, id, online_pregled=True, auto_print=auto_print)
    if not n:
        return redirect(url_for('index_master'))
    return html_izvjestaj

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
            
            _, _, _, report_html = generiraj_html_izvjestaja(conn, id, online_pregled=False, auto_print=False)
            if report_html:
                zf.writestr(f"{glavni_folder}/Pregled_Naloga_{id}.html", report_html.encode('utf-8'))
    except Exception as e: pass
    
    memory_file.seek(0)
    return send_file(memory_file, download_name=predlozeno_ime_zipa, as_attachment=True)
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
        dimenzije_ploce = normaliziraj_popis_ploca_ili_materijala(request.form.get('dimenzije_ploce', ''), False)
        materijal_ploce = normaliziraj_popis_ploca_ili_materijala(request.form.get('materijal_ploce', ''), True)
        akcija = request.form.get('akcija', 'zavrsi_odmah')
        
        parser_int = to_int if akcija == 'pauziraj' else to_int_or_zero
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=COALESCE(NULLIF(?, ""), naziv_pozicije), laser_komada=?, laser_skart=?, laser_priprema_sati=?, laser_priprema_minute=?, laser_rezanje_sati=?, laser_rezanje_minute=?, laser_radnik=? WHERE id=?', 
                         (naziv_poz, parser_int(request.form.get(f'komada_{pid}')), parser_int(request.form.get(f'skart_{pid}')), parser_int(request.form.get(f'priprema_sati_{pid}')), parser_int(request.form.get(f'priprema_minute_{pid}')), parser_int(request.form.get(f'rezanje_sati_{pid}')), parser_int(request.form.get(f'rezanje_minute_{pid}')), radnik, pid))
        
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
        
        parser_int = to_int if akcija == 'pauziraj' else to_int_or_zero
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=COALESCE(NULLIF(?, ""), naziv_pozicije), bravarija_komada=?, bravarija_skart=?, bravarija_priprema_sati=?, bravarija_priprema_minute=?, bravarija_piganje_sati=?, bravarija_piganje_minute=?, bravarija_radnik=? WHERE id=?', 
                         (naziv_poz, parser_int(request.form.get(f'komada_{pid}')), parser_int(request.form.get(f'skart_{pid}')), parser_int(request.form.get(f'priprema_sati_{pid}')), parser_int(request.form.get(f'priprema_minute_{pid}')), parser_int(request.form.get(f'piganje_sati_{pid}')), parser_int(request.form.get(f'piganje_minute_{pid}')), radnik, pid))
        
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
    if not naziv:
        naziv = f"Pozicija-{dobij_zg_vrijeme().strftime('%H%M%S')}"
    ciljana_kol = to_int(request.form.get('nova_ciljana_kolicina')) or 0
    sender_id = request.headers.get('X-Client-ID') or request.form.get('client_id')

    conn = get_db_connection()
    nalog = conn.execute("SELECT laser_radnik, bravarija_radnik FROM radni_nalozi WHERE id=?", (nalog_id,)).fetchone()
    l_radnik = nalog['laser_radnik'] if nalog and nalog['laser_radnik'] else ''
    b_radnik = nalog['bravarija_radnik'] if nalog and nalog['bravarija_radnik'] else ''
    cur = conn.execute(
        'INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, laser_skart, bravarija_komada, bravarija_skart, laser_priprema_sati, laser_priprema_minute, laser_rezanje_sati, laser_rezanje_minute, bravarija_priprema_sati, bravarija_priprema_minute, bravarija_piganje_sati, bravarija_piganje_minute, laser_radnik, bravarija_radnik) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)',
        (nalog_id, naziv, ciljana_kol, l_radnik, b_radnik)
    )
    novi_id = cur.lastrowid
    conn.commit()
    objavi_promjenu("dodana_pozicija", sender_id=sender_id)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'ok': True,
            'pozicija': {
                'id': novi_id,
                'nalog_id': nalog_id,
                'naziv_pozicije': naziv,
                'ciljana_kolicina': ciljana_kol
            }
        })
    return redirect(url_for(f'sekcija_{izvor}'))

@app.route('/obrisi_poziciju/<int:poz_id>/<string:izvor>', methods=['GET', 'POST'])
def obrisi_poziciju(poz_id, izvor):
    sender_id = request.headers.get('X-Client-ID') or request.form.get('client_id')
    conn = get_db_connection()
    conn.execute('DELETE FROM nalog_pozicije WHERE id=?', (poz_id,))
    conn.commit()
    objavi_promjenu("obrisana_pozicija", sender_id=sender_id)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'obrisan_id': poz_id})
    return redirect(url_for(f'sekcija_{izvor}'))

if __name__ == '__main__':
    try:
        from werkzeug.serving import WSGIRequestHandler
        WSGIRequestHandler.protocol_version = "HTTP/1.1"
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
