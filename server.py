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
from datetime import datetime, timedelta
from flask import Flask, request, redirect, url_for, render_template, send_from_directory, session, make_response, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'firstcut_pogon_secure_key_2026'
app.permanent_session_lifetime = timedelta(days=365)

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
DB_FILE = os.path.join(BASE_DIR, 'proizvodnja.db')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {'pdf', 'lxds', 'dxf'}

if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS radni_nalozi (
        id INTEGER PRIMARY KEY AUTOINCREMENT, naziv_naloga TEXT NOT NULL, naziv_projekta TEXT DEFAULT '',
        debljina_ploce TEXT DEFAULT '', pdf_datoteka TEXT, lxdf_datoteka TEXT, status TEXT DEFAULT 'Laser',
        laser_zapoceto_u TEXT, bravarija_zapoceto_u TEXT, kreirano_u TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        opis TEXT DEFAULT '', laser_napomena TEXT DEFAULT '', bravarija_napomena TEXT DEFAULT '',
        rutiranje TEXT DEFAULT 'Pogon', odabrani_laser TEXT DEFAULT '', kreirao TEXT DEFAULT '',
        dimenzije_ploce_laser TEXT DEFAULT '', materijal_ploce_laser TEXT DEFAULT '', zavrseno_u TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS nalog_pozicije (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nalog_id INTEGER, naziv_pozicije TEXT NOT NULL,
        laser_komada INTEGER DEFAULT 0, laser_skart INTEGER DEFAULT 0, laser_sati INTEGER DEFAULT 0, laser_minute INTEGER DEFAULT 0, laser_radnik TEXT DEFAULT '',
        bravarija_komada INTEGER DEFAULT 0, bravarija_skart INTEGER DEFAULT 0, bravarija_sati INTEGER DEFAULT 0, bravarija_minute INTEGER DEFAULT 0, bravarija_radnik TEXT DEFAULT '',
        ciljana_kolicina INTEGER DEFAULT 0, laser_priprema_sati INTEGER DEFAULT 0, laser_priprema_minute INTEGER DEFAULT 0,
        laser_rezanje_sati INTEGER DEFAULT 0, laser_rezanje_minute INTEGER DEFAULT 0, bravarija_priprema_sati INTEGER DEFAULT 0,
        bravarija_priprema_minute INTEGER DEFAULT 0, bravarija_piganje_sati INTEGER DEFAULT 0, bravarija_piganje_minute INTEGER DEFAULT 0,
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
        ("zavrseno_u", "TIMESTAMP")
    ]
    for kol, tip in kolone_radni:
        try: conn.execute(f"ALTER TABLE radni_nalozi ADD COLUMN {kol} {tip}")
        except: pass

    kolone_poz = [
        ("laser_sati", "INTEGER DEFAULT 0"), ("laser_minute", "INTEGER DEFAULT 0"), 
        ("bravarija_sati", "INTEGER DEFAULT 0"), ("bravarija_minute", "INTEGER DEFAULT 0"),
        ("ciljana_kolicina", "INTEGER DEFAULT 0"), ("laser_priprema_sati", "INTEGER DEFAULT 0"), 
        ("laser_priprema_minute", "INTEGER DEFAULT 0"), ("laser_rezanje_sati", "INTEGER DEFAULT 0"), 
        ("laser_rezanje_minute", "INTEGER DEFAULT 0"), ("bravarija_priprema_sati", "INTEGER DEFAULT 0"), 
        ("bravarija_priprema_minute", "INTEGER DEFAULT 0"), ("bravarija_piganje_sati", "INTEGER DEFAULT 0"), 
        ("bravarija_piganje_minute", "INTEGER DEFAULT 0")
    ]
    for kol, tip in kolone_poz:
        try: conn.execute(f"ALTER TABLE nalog_pozicije ADD COLUMN {kol} {tip}")
        except: pass
        
    try:
        kolone_za_null = ["laser_komada", "laser_skart", "laser_priprema_sati", "laser_priprema_minute", "laser_rezanje_sati", "laser_rezanje_minute", "bravarija_komada", "bravarija_skart", "bravarija_priprema_sati", "bravarija_priprema_minute", "bravarija_piganje_sati", "bravarija_piganje_minute"]
        for kol in kolone_za_null: conn.execute(f"UPDATE nalog_pozicije SET {kol} = NULL WHERE {kol} = 0")
    except: pass
    
    conn.commit()
    conn.close()

init_db()

def formatiraj_datum(datum_str):
    if not datum_str: return ''
    try:
        d = str(datum_str).replace('T', ' ').split('.')[0]
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

@app.before_request
def provjera_pristupa():
    dozvoljeno_svima = ['/prijava_stanice', '/logo.png', '/favicon.png', '/favicon.ico', '/postavi_stanicu', '/odjava_stanice', '/login', '/logout', '/maintenance', '/skriveni_dev_login']
    if any(request.path.startswith(d) for d in dozvoljeno_svima): return
    
    conn = get_db_connection()
    maint = conn.execute("SELECT vrijednost FROM postavke WHERE kljuc='maintenance'").fetchone()
    conn.close()
    
    if maint and maint['vrijednost'] == '1':
        if request.cookies.get('pogon_role') != 'Developer':
            return redirect(url_for('maintenance'))

    z_stanica = request.cookies.get('zakljucana_stanica')
    if not z_stanica: return redirect(url_for('prijava_stanice'))
    if request.path == '/': return
    if z_stanica in ['laser1', 'laser2'] and not any(request.path.startswith(r) for r in ['/laser', '/zapocni_fazu', '/dodaj_poziciju', '/obrisi_poziciju', '/preuzmi']): return redirect('/laser')
    elif z_stanica == 'bravarija' and not any(request.path.startswith(r) for r in ['/bravarija', '/zapocni_fazu', '/dodaj_poziciju', '/obrisi_poziciju', '/preuzmi']): return redirect('/bravarija')
    elif z_stanica == 'monitor' and not request.path.startswith('/preuzmi'): return redirect('/')

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
        ob = conn.execute("SELECT tekst, vrijedi_do FROM obavijesti WHERE vrijedi_do > ? ORDER BY id DESC LIMIT 1", (datetime.now().isoformat(),)).fetchone()
        if ob: aktivna_obavijest, aktivna_obavijest_do = ob['tekst'], ob['vrijedi_do']
        conn.close()
    except: pass
    
    pogon_role = request.cookies.get('pogon_role')
    return dict(stanica=lokalna_stanica, prikazi_klijent_gumbe=je_klijent, is_server_window=False, z_stanica=z_stanica, is_mobile=is_mobile, aktivna_obavijest=aktivna_obavijest, aktivna_obavijest_do=aktivna_obavijest_do, pogon_role=pogon_role)

@app.route('/logo.png')
def serve_logo(): return send_from_directory(BASE_DIR, 'logo.png')
@app.route('/favicon.png')
def serve_favicon(): return send_from_directory(BASE_DIR, 'favicon.png')
@app.route('/favicon.ico')
def favicon_fallback(): return redirect(url_for('serve_favicon'))

@app.route('/maintenance')
def maintenance():
    conn = get_db_connection()
    maint = conn.execute("SELECT vrijednost FROM postavke WHERE kljuc='maintenance'").fetchone()
    conn.close()
    if not maint or maint['vrijednost'] == '0': return redirect('/')
    if request.cookies.get('pogon_role') == 'Developer': return redirect('/dev_panel')
    return render_template('maintenance.html')

@app.route('/skriveni_dev_login', methods=['POST'])
def skriveni_dev_login():
    password = request.form.get('password', '')
    if hashlib.sha256(password.encode('utf-8')).hexdigest() == DEV_HASH:
        resp = make_response(redirect(url_for('dev_panel')))
        resp.set_cookie('pogon_role', 'Developer')
        return resp
    return redirect(url_for('maintenance'))

@app.route('/prijava_stanice', methods=['GET', 'POST'])
def prijava_stanice():
    conn = get_db_connection()
    prava_ip = request.headers.get('X-Forwarded-For', request.headers.get('X-Real-IP', request.remote_addr or 'nepoznato')).split(',')[0].strip()
    otisak_uređaja = hashlib.md5(f"{prava_ip}_{request.headers.get('User-Agent', 'nepoznat')}".encode('utf-8')).hexdigest()
    blokada = conn.execute("SELECT * FROM blokade WHERE ip_adresa=?", (otisak_uređaja,)).fetchone()
    
    sada = datetime.now()
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
            conn.close()
            cilj = '/laser' if odabir in ['laser1', 'laser2'] else '/bravarija' if odabir == 'bravarija' else '/'
            resp = make_response(redirect(cilj))
            resp.set_cookie('zakljucana_stanica', odabir, max_age=60*60*24*365)
            resp.set_cookie('stanica_lokalno', odabir if odabir not in ['uprava', 'monitor'] else 'dashboard', max_age=60*60*24*365)
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
    conn.close()
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
    resp.set_cookie('stanica_lokalno', ime_stanice, max_age=60*60*24*365)
    resp.set_cookie('zakljucana_stanica', z_ime, max_age=60*60*24*365)
    if request.args.get('client') == 'true': resp.set_cookie('is_client', 'true', max_age=60*60*24*365)
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
    conn.close()
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
            resp.set_cookie('pogon_role', 'Admin') 
            return resp
            
        elif username == "developer" and uneseni_hash == DEV_HASH:
            resp = make_response(redirect(url_for('dev_panel')))
            resp.set_cookie('pogon_role', 'Developer')
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
            vrijedi_do = (datetime.now() + timedelta(minutes=trajanje_minuta)).isoformat()
            conn.execute("DELETE FROM obavijesti")
            conn.execute("INSERT INTO obavijesti (tekst, vrijedi_do) VALUES (?, ?)", (tekst, vrijedi_do))
            conn.commit()
        elif akcija == 'ugasi_ticker':
            conn.execute("DELETE FROM obavijesti")
            conn.commit()
        elif akcija == 'odblokiraj':
            ip_od = request.form.get('ip_adresa')
            conn.execute("DELETE FROM blokade WHERE ip_adresa=?", (ip_od,))
            conn.commit()
        elif akcija == 'toggle_maint':
            status = request.form.get('maint_status', '0')
            conn.execute("UPDATE postavke SET vrijednost=? WHERE kljuc='maintenance'", (status,))
            conn.commit()
            
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

    conn.close()
    return render_template('dev_panel.html', blokade=blokade, uk_naloga=uk_naloga, uk_pozicija=uk_pozicija, db_velicina_mb=db_velicina_mb, maint_status=maint_status)

@app.route('/sefo_panel', methods=['GET', 'POST'])
def index_master():
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    
    # 14 DANA AUTO-ARHIVIRANJE: Svi aktivni nalozi stariji od 14 dana automatski se arhiviraju.
    granica_za_arhivu = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("UPDATE radni_nalozi SET status = 'Arhivirano', zavrseno_u = COALESCE(zavrseno_u, CURRENT_TIMESTAMP) WHERE status != 'Arhivirano' AND kreirano_u <= ?", (granica_za_arhivu,))
    conn.commit()
    
    if request.method == 'POST':
        naziv = request.form['naziv_naloga']
        projekt = request.form.get('naziv_projekta', '')
        kreirao = request.form.get('kreirao', '')
        debljina_ploce = request.form.get('debljina_ploce', '')
        opis = request.form.get('opis', '')
        rutiranje = request.form.get('rutiranje', 'Pogon')
        
        p_names = []
        for f in request.files.getlist('pdf_file'):
            if f and allowed_file(f.filename):
                fname = secure_filename(f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                p_names.append(fname)
        p_name_db = json.dumps(p_names) if p_names else None
            
        l_names = []
        for f in request.files.getlist('lxdf_file'):
            if f and allowed_file(f.filename):
                fname = secure_filename(f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                l_names.append(fname)
        l_name_db = json.dumps(l_names) if l_names else None
        
        pocetni_status = 'Piganje' if rutiranje == 'Samo Bravarija' else 'Laser'
        cursor = conn.execute('INSERT INTO radni_nalozi (naziv_naloga, naziv_projekta, debljina_ploce, pdf_datoteka, lxdf_datoteka, opis, rutiranje, status, kreirao) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                     (naziv, projekt, debljina_ploce, p_name_db, l_name_db, opis, rutiranje, pocetni_status, kreirao))
        nalog_id = cursor.lastrowid
        
        try:
            stavke = json.loads(request.form.get('kosarica_data', '[]'))
            for s in stavke:
                conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, laser_skart, bravarija_komada, bravarija_skart, laser_priprema_sati, laser_priprema_minute, laser_rezanje_sati, laser_rezanje_minute, bravarija_priprema_sati, bravarija_priprema_minute, bravarija_piganje_sati, bravarija_piganje_minute) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)', (nalog_id, s['naziv'], int(s.get('komada', 0))))
        except Exception: pass
        conn.commit()
        return redirect(url_for('index_master'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status != 'Arhivirano' ORDER BY id DESC").fetchall()
    aktivni_nalozi = []
    zavrseni_nalozi = []
    
    for r in nalozi_rows:
        n = dict(r)
        n['kreirano_u_prikaz'] = formatiraj_datum(n['kreirano_u'])
        n['zavrseno_u_prikaz'] = formatiraj_datum(n['zavrseno_u'])
        n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        
        if n['status'] == 'Na pregledu': zavrseni_nalozi.append(n)
        else: aktivni_nalozi.append(n)
            
    conn.close()
    return render_template('sefo_panel.html', aktivni_nalozi=aktivni_nalozi, zavrseni_nalozi=zavrseni_nalozi)

@app.route('/arhiva')
def arhiva():
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status = 'Arhivirano' ORDER BY id DESC").fetchall()
    nalozi = []
    for r in nalozi_rows:
        n = dict(r)
        n['kreirano_u_prikaz'] = formatiraj_datum(n['kreirano_u'])
        n['zavrseno_u_prikaz'] = formatiraj_datum(n['zavrseno_u'])
        n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        nalozi.append(n)
    conn.close()
    return render_template('arhiva.html', nalozi=nalozi)

@app.route('/preuzmi_izvjestaj/<int:id>')
def preuzmi_izvjestaj(id):
    conn = get_db_connection()
    n = conn.execute("SELECT * FROM radni_nalozi WHERE id=?", (id,)).fetchone()
    if not n:
        conn.close()
        return redirect(url_for('index_master'))
        
    pozicije = conn.execute("SELECT * FROM nalog_pozicije WHERE nalog_id=?", (id,)).fetchall()
    firma = secure_filename(n['naziv_naloga']) if n['naziv_naloga'] else "Firma"
    projekt = secure_filename(n['naziv_projekta']) if n['naziv_projekta'] else "Projekt"
    debljina = secure_filename(n['debljina_ploce']) if n['debljina_ploce'] else "Debljina"
    predlozeno_ime_zipa = f"{firma}_{projekt}_{debljina}.zip".replace("__", "_")
    memory_file = io.BytesIO()
    
    try:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            pdf_datoteke = parsiraj_listu_datoteka(n['pdf_datoteka'])
            for pdf in pdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], pdf['filename'])
                if os.path.exists(fpath): zf.write(fpath, pdf['filename'])
            lxdf_datoteke = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            for lx in lxdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], lx['filename'])
                if os.path.exists(fpath): zf.write(fpath, lx['filename'])
            logo_base64 = ""
            logo_ext = ""
            for ext in ['png', 'jpg', 'jpeg']:
                path = os.path.join(BASE_DIR, f'html_logo.{ext}')
                if os.path.exists(path):
                    with open(path, "rb") as lf:
                        logo_base64 = base64.b64encode(lf.read()).decode('utf-8')
                        logo_ext = "jpeg" if ext == "jpg" else ext
                    break
                    
            report_html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Proizvodni Izvještaj - Nalog #{id}</title><style>body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; color: #1a202c; margin: 0; padding: 30px; }} .wrapper {{ max-width: 900px; background: #ffffff; margin: 0 auto; padding: 40px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.05); border-top: 8px solid #dc3545; }} .logo-container {{ background-color: #ffffff; padding: 10px; border-radius: 8px; display: inline-flex; align-items: center; }} .logo-img {{ filter: brightness(1.35) saturate(1.25) contrast(1.1); max-height: 55px; width: auto; }} .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; margin-bottom: 25px; }} .meta-title {{ font-size: 14px; text-transform: uppercase; color: #64748b; text-align: right; font-weight: bold; letter-spacing: 1px; line-height: 1.4; }} .info-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 30px; }} .info-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #0dcaf0; padding: 14px 18px; border-radius: 8px; }} .info-card h3 {{ margin: 0 0 4px 0; font-size: 11px; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px; }} .info-card p {{ margin: 0; font-size: 15px; font-weight: 600; color: #0f172a; }} .napomena-kontejner {{ margin-bottom: 30px; }} .napomena-card {{ padding: 15px 18px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid #dc3545; background: #fffafb; border: 1px solid #fecdd3; border-right: 1px solid #fecdd3; border-bottom: 1px solid #fecdd3; }} .napomena-card.bravarija {{ border-left-color: #ea580c; background: #fffdfa; border-color: #ffedd5; }} .napomena-card.poslovođa {{ border-left-color: #0dcaf0; background: #f0fdfa; border-color: #ccfbf1; }} .napomena-card h4 {{ margin: 0 0 6px 0; font-size: 12px; text-transform: uppercase; color: #475569; }} .napomena-card p {{ margin: 0; font-size: 14px; font-weight: 500; color: #1e293b; white-space: pre-wrap; }} .table-section {{ margin-top: 30px; }} .table-section h2 {{ font-size: 18px; font-weight: 700; color: #0f172a; margin-bottom: 15px; padding-bottom: 6px; border-bottom: 2px solid #e2e8f0; }} table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }} th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 12px 14px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1; }} td {{ padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; vertical-align: middle; color: #334155; }} tr:nth-child(even) td {{ background: #f8fafc; }} .badge {{ display: inline-block; padding: 4px 8px; font-size: 11px; font-weight: 700; border-radius: 4px; text-transform: uppercase; margin-right: 4px; }} .badge-info {{ background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }} .badge-success {{ background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }} .badge-danger {{ background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }} .radnik-info {{ font-size: 12px; color: #64748b; margin-top: 4px; line-height: 1.4; }} .footer {{ text-align: center; margin-top: 40px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 15px; }} @media print {{ @page {{ margin: 1cm; size: A4 portrait; }} body {{ background-color: #ffffff !important; padding: 0; color: #000; }} .wrapper {{ box-shadow: none; padding: 0; border-top: none; max-width: 100%; }} .btn, .navbar {{ display: none !important; }} * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }} }}</style></head><body><div class="wrapper"><div class="header"><div class="logo-container">{"<img src='data:image/" + logo_ext + ";base64," + logo_base64 + "' class='logo-img' alt='FirstCutLaser'>" if logo_base64 else "<h1 style='color:#dc3545; margin:0;'>FIRST CUT LASER</h1>"}</div><div class="meta-title">Arhivski Izvještaj Pogona<br><span style="color:#0f172a; font-size:16px;">Nalog #{id}</span></div></div><div class="info-grid"><div class="info-card"><h3>Kupac / Partner</h3><p>{n['naziv_naloga'] if n['naziv_naloga'] else '-'}</p></div><div class="info-card"><h3>Naziv Projekta</h3><p>{n['naziv_projekta'] if n['naziv_projekta'] else '-'}</p></div><div class="info-card"><h3>Kreator Naloga</h3><p>{n['kreirao'] if n['kreirao'] else '-'}</p></div><div class="info-card"><h3>Proizvodna Ruta</h3><p>{n['rutiranje'] if n['rutiranje'] else '-'}</p></div><div class="info-card"><h3>Debljina Materijala</h3><p>{n['debljina_ploce'] if n['debljina_ploce'] else '-'}</p></div><div class="info-card"><h3>Dimenzije uložene ploče</h3><p>{n['dimenzije_ploce_laser'] if n['dimenzije_ploce_laser'] else '-'}</p></div><div class="info-card"><h3>Materijal ploče</h3><p>{n['materijal_ploce_laser'] if n['materijal_ploce_laser'] else '-'}</p></div></div><div class="napomena-kontejner">{"<div class='napomena-card poslovođa'><h4><i class='fa-solid fa-user-tie'></i> Upute Poslovođe</h4><p>" + n['opis'] + "</p></div>" if n['opis'] else ""}{"<div class='napomena-card'><h4><i class='fa-solid fa-fire'></i> Napomena s Lasera</h4><p>" + n['laser_napomena'] + "</p></div>" if n['laser_napomena'] else ""}{"<div class='napomena-card bravarija'><h4><i class='fa-solid fa-hammer'></i> Napomena iz Bravarije</h4><p>" + n['bravarija_napomena'] + "</p></div>" if n['bravarija_napomena'] else ""}</div><div class="table-section"><h2>Specifikacija Izvršenih Pozicija</h2><table><thead><tr><th>Naziv Pozicije s Nacrta</th><th>Faza 1: Rezanje (Laser)</th><th>Faza 2: Bravarska Obrada</th></tr></thead><tbody>"""
            for p in pozicije:
                vrijeme_laser = f"Priprema: {p['laser_priprema_sati']}h {p['laser_priprema_minute']}m<br>Rezanje: {p['laser_rezanje_sati']}h {p['laser_rezanje_minute']}m" if p['laser_priprema_sati'] or p['laser_priprema_minute'] or p['laser_rezanje_sati'] or p['laser_rezanje_minute'] else f"Vrijeme: {p['laser_sati']}h {p['laser_minute']}m"
                vrijeme_bravarija = f"Priprema: {p['bravarija_priprema_sati']}h {p['bravarija_priprema_minute']}m<br>Piganje: {p['bravarija_piganje_sati']}h {p['bravarija_piganje_minute']}m" if p['bravarija_priprema_sati'] or p['bravarija_priprema_minute'] or p['bravarija_piganje_sati'] or p['bravarija_piganje_minute'] else f"Vrijeme: {p['bravarija_sati']}h {p['bravarija_minute']}m"
                report_html += f"""<tr><td style="font-weight: 700; color: #0f172a; font-size: 15px;">{p['naziv_pozicije']}</td><td><div><span class="badge badge-info">POTREBNO: {p['ciljana_kolicina']} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {p['laser_komada']} KOM</span><span class="badge badge-danger">{p['laser_skart']} ŠKART</span></div><div class="radnik-info">{vrijeme_laser}<br>Radnik: {p['laser_radnik'] if p['laser_radnik'] else '-'}</div></td><td><div><span class="badge badge-info">POTREBNO: {p['ciljana_kolicina']} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {p['bravarija_komada']} KOM</span><span class="badge badge-danger">{p['bravarija_skart']} ŠKART</span></div><div class="radnik-info">{vrijeme_bravarija}<br>Radnik: {p['bravarija_radnik'] if p['bravarija_radnik'] else '-'}</div></td></tr>"""
            report_html += f"""</tbody></table></div><div class="footer">Sustav First Cut Laser d.o.o. &bull; Izvještaj generiran: {datetime.now().strftime('%d.%m.%Y. u %H:%M')}</div></div></body></html>"""
            zf.writestr(f"Pregled_Naloga_{id}.html", report_html.encode('utf-8'))
    except Exception as e: pass
    
    conn.close()
    memory_file.seek(0)
    return send_file(memory_file, download_name=predlozeno_ime_zipa, as_attachment=True)

@app.route('/arhiviraj_nalog/<int:id>')
def arhiviraj_nalog(id):
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute("UPDATE radni_nalozi SET status='Arhivirano' WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index_master'))

@app.route('/obrisi/<int:id>')
def obrisi_nalog(id):
    if request.cookies.get('pogon_role') not in ['Admin', 'Developer']: return redirect(url_for('login'))
    conn = get_db_connection()
    n = conn.execute('SELECT pdf_datoteka, lxdf_datoteka FROM radni_nalozi WHERE id=?', (id,)).fetchone()
    if n:
        for pdf in parsiraj_listu_datoteka(n['pdf_datoteka']):
            fpath = os.path.join(UPLOAD_FOLDER, pdf['filename'])
            if os.path.exists(fpath): os.remove(fpath)
        for lx in parsiraj_listu_datoteka(n['lxdf_datoteka']):
            fpath = os.path.join(UPLOAD_FOLDER, lx['filename'])
            if os.path.exists(fpath): os.remove(fpath)
    
    conn.execute('DELETE FROM radni_nalozi WHERE id=?', (id,))
    conn.execute('DELETE FROM nalog_pozicije WHERE nalog_id=?', (id,))
    conn.commit()
    conn.close()
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
                if os.path.exists(fpath): os.remove(fpath)
            for lx in parsiraj_listu_datoteka(n['lxdf_datoteka']):
                fpath = os.path.join(UPLOAD_FOLDER, lx['filename'])
                if os.path.exists(fpath): os.remove(fpath)
        conn.execute('DELETE FROM radni_nalozi WHERE id=?', (n_id,))
        conn.execute('DELETE FROM nalog_pozicije WHERE nalog_id=?', (n_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('arhiva'))

@app.route('/preuzmi/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=False)

@app.route('/zapocni_fazu/<int:id>/<string:faza>')
def zapocni_fazu(id, faza):
    trenutni_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = get_db_connection()
    
    if faza == 'laser': 
        stanica = request.cookies.get('stanica_lokalno', 'Laser')
        ime_lasera = 'Laser 1' if stanica == 'laser1' else 'Laser 2' if stanica == 'laser2' else 'Nepoznat Laser'
        
        aktivni = conn.execute("SELECT id FROM radni_nalozi WHERE status='Laser' AND odabrani_laser=? AND laser_zapoceto_u IS NOT NULL", (ime_lasera,)).fetchone()
        if aktivni:
            conn.close()
            return redirect(url_for('sekcija_laser', err='zauzeto'))
            
        conn.execute("UPDATE radni_nalozi SET status='Laser', laser_zapoceto_u=?, odabrani_laser=? WHERE id=?", (trenutni_iso, ime_lasera, id))
        
    elif faza == 'bravarija': 
        aktivni = conn.execute("SELECT id FROM radni_nalozi WHERE status='Piganje' AND bravarija_zapoceto_u IS NOT NULL").fetchone()
        if aktivni:
            conn.close()
            return redirect(url_for('sekcija_bravarija', err='zauzeto'))
            
        conn.execute("UPDATE radni_nalozi SET status='Piganje', bravarija_zapoceto_u=? WHERE id=?", (trenutni_iso, id))
        
    conn.commit()
    conn.close()
    return redirect(url_for(f'sekcija_{faza}'))

@app.route('/laser', methods=['GET', 'POST'])
def sekcija_laser():
    conn = get_db_connection()
    if request.method == 'POST':
        id_naloga = request.form['id_naloga']
        radnik = request.form.get('radnik', '')
        radnik_napomena = request.form.get('radnik_napomena', '')
        dimenzije_ploce = request.form.get('dimenzije_ploce', '')
        materijal_ploce = request.form.get('materijal_ploce', '')
        akcija = request.form.get('akcija', 'zavrsi_odmah')
        
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=?, laser_komada=?, laser_skart=?, laser_priprema_sati=?, laser_priprema_minute=?, laser_rezanje_sati=?, laser_rezanje_minute=?, laser_radnik=? WHERE id=?', 
                         (naziv_poz, to_int(request.form.get(f'komada_{pid}')), to_int(request.form.get(f'skart_{pid}')), to_int(request.form.get(f'priprema_sati_{pid}')), to_int(request.form.get(f'priprema_minute_{pid}')), to_int(request.form.get(f'rezanje_sati_{pid}')), to_int(request.form.get(f'rezanje_minute_{pid}')), radnik, pid))
        
        if akcija == 'pauziraj':
            conn.execute("UPDATE radni_nalozi SET status='Pauzirano - Laser', laser_zapoceto_u=NULL, odabrani_laser='', laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=? WHERE id=?", (radnik_napomena, dimenzije_ploce, materijal_ploce, id_naloga))
        else:
            novi_status = 'Piganje' if akcija == 'bravarija' else 'Na pregledu'
            vrijeme_zav = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if novi_status == 'Na pregledu' else None
            if vrijeme_zav:
                conn.execute("UPDATE radni_nalozi SET status=?, laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=?, zavrseno_u=? WHERE id=?", (novi_status, radnik_napomena, dimenzije_ploce, materijal_ploce, vrijeme_zav, id_naloga))
            else:
                conn.execute("UPDATE radni_nalozi SET status=?, laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=? WHERE id=?", (novi_status, radnik_napomena, dimenzije_ploce, materijal_ploce, id_naloga))
        conn.commit()
        return redirect(url_for('sekcija_laser'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status IN ('Laser', 'Pauzirano - Laser')").fetchall()
    nalozi = []
    z_stanica = request.cookies.get('zakljucana_stanica', 'uprava')
    
    for r in nalozi_rows:
        n = dict(r)
        prikazi = True
        if n['laser_zapoceto_u']:
            if z_stanica == 'laser1' and n['odabrani_laser'] != 'Laser 1': prikazi = False
            elif z_stanica == 'laser2' and n['odabrani_laser'] != 'Laser 2': prikazi = False
        if prikazi:
            n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
            n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
            nalozi.append(n)
    conn.close()
    return render_template('laser.html', nalozi=nalozi)

@app.route('/bravarija', methods=['GET', 'POST'])
def sekcija_bravarija():
    conn = get_db_connection()
    if request.method == 'POST':
        id_naloga = request.form['id_naloga']
        radnik = request.form.get('radnik', '')
        radnik_napomena = request.form.get('radnik_napomena', '')
        akcija = request.form.get('akcija', 'zavrsi_odmah')
        
        for pid in request.form.getlist('pozicija_id'):
            naziv_poz = request.form.get(f'naziv_{pid}')
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=?, bravarija_komada=?, bravarija_skart=?, bravarija_priprema_sati=?, bravarija_priprema_minute=?, bravarija_piganje_sati=?, bravarija_piganje_minute=?, bravarija_radnik=? WHERE id=?', 
                         (naziv_poz, to_int(request.form.get(f'komada_{pid}')), to_int(request.form.get(f'skart_{pid}')), to_int(request.form.get(f'priprema_sati_{pid}')), to_int(request.form.get(f'priprema_minute_{pid}')), to_int(request.form.get(f'piganje_sati_{pid}')), to_int(request.form.get(f'piganje_minute_{pid}')), radnik, pid))
        
        if akcija == 'pauziraj':
            conn.execute("UPDATE radni_nalozi SET status='Pauzirano - Bravarija', bravarija_zapoceto_u=NULL, bravarija_napomena=? WHERE id=?", (radnik_napomena, id_naloga))
        else:
            vrijeme_zav = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("UPDATE radni_nalozi SET status='Na pregledu', bravarija_napomena=?, zavrseno_u=? WHERE id=?", (radnik_napomena, vrijeme_zav, id_naloga))
                         
        conn.commit()
        return redirect(url_for('sekcija_bravarija'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status IN ('Piganje', 'Pauzirano - Bravarija')").fetchall()
    nalozi = []
    for r in nalozi_rows:
        n = dict(r)
        n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        nalozi.append(n)
    conn.close()
    return render_template('bravarija.html', nalozi=nalozi)

@app.route('/dodaj_poziciju/<int:nalog_id>/<string:izvor>', methods=['POST'])
def dodaj_poziciju(nalog_id, izvor):
    naziv = request.form.get('novi_naziv_pozicije', '').strip()
    if not naziv: naziv = f"Pozicija-{datetime.now().strftime('%H%M%S')}"
    conn = get_db_connection()
    conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, laser_skart, bravarija_komada, bravarija_skart, laser_priprema_sati, laser_priprema_minute, laser_rezanje_sati, laser_rezanje_minute, bravarija_priprema_sati, bravarija_priprema_minute, bravarija_piganje_sati, bravarija_piganje_minute) VALUES (?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)', (nalog_id, naziv))
    conn.commit()
    conn.close()
    return redirect(url_for(f'sekcija_{izvor}'))

@app.route('/obrisi_poziciju/<int:poz_id>/<string:izvor>')
def obrisi_poziciju(poz_id, izvor):
    conn = get_db_connection()
    conn.execute('DELETE FROM nalog_pozicije WHERE id=?', (poz_id,))
    conn.commit()
    conn.close()
    return redirect(url_for(f'sekcija_{izvor}'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)