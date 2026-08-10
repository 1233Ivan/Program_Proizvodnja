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
from flask import Flask, request, redirect, url_for, render_template_string, send_from_directory, session, make_response, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'firstcut_pogon_secure_key_2026'

_B = base64.b64decode(b'MjMxMg==').decode('utf-8')
PIN_HASH = hashlib.sha256(_B.encode()).hexdigest()
del _B

def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_path()
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'pohrana_nacrta')
DB_FILE = os.path.join(BASE_DIR, 'proizvodnja.db')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {'pdf', 'lxds', 'dxf'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS radni_nalozi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            naziv_naloga TEXT NOT NULL,
            naziv_projekta TEXT DEFAULT '',
            debljina_ploce TEXT DEFAULT '',
            pdf_datoteka TEXT,
            lxdf_datoteka TEXT,
            status TEXT DEFAULT 'Laser',
            laser_zapoceto_u TEXT,
            bravarija_zapoceto_u TEXT,
            kreirano_u TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            opis TEXT DEFAULT '',
            laser_napomena TEXT DEFAULT '',
            bravarija_napomena TEXT DEFAULT '',
            rutiranje TEXT DEFAULT 'Pogon',
            odabrani_laser TEXT DEFAULT '',
            kreirao TEXT DEFAULT ''
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS nalog_pozicije (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nalog_id INTEGER,
            naziv_pozicije TEXT NOT NULL,
            laser_komada INTEGER DEFAULT 0,
            laser_skart INTEGER DEFAULT 0,
            laser_sati INTEGER DEFAULT 0,
            laser_minute INTEGER DEFAULT 0,
            laser_radnik TEXT DEFAULT '',
            bravarija_komada INTEGER DEFAULT 0,
            bravarija_skart INTEGER DEFAULT 0,
            bravarija_sati INTEGER DEFAULT 0,
            bravarija_minute INTEGER DEFAULT 0,
            bravarija_radnik TEXT DEFAULT '',
            FOREIGN KEY(nalog_id) REFERENCES radni_nalozi(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS blokade (
            ip_adresa TEXT PRIMARY KEY,
            pokusaji INTEGER DEFAULT 0,
            blokiran_do TIMESTAMP
        )
    ''')
    
    kolone_radni = [
        ("laser_zapoceto_u", "TEXT"), ("bravarija_zapoceto_u", "TEXT"), 
        ("opis", "TEXT DEFAULT ''"), ("rutiranje", "TEXT DEFAULT 'Pogon'"), 
        ("naziv_projekta", "TEXT DEFAULT ''"), ("laser_napomena", "TEXT DEFAULT ''"), 
        ("bravarija_napomena", "TEXT DEFAULT ''"), ("debljina_ploce", "TEXT DEFAULT ''"),
        ("odabrani_laser", "TEXT DEFAULT ''"), ("kreirao", "TEXT DEFAULT ''"),
        ("dimenzije_ploce_laser", "TEXT DEFAULT ''"), ("materijal_ploce_laser", "TEXT DEFAULT ''")
    ]
    for kol, tip in kolone_radni:
        try: conn.execute(f"ALTER TABLE radni_nalozi ADD COLUMN {kol} {tip}")
        except: pass

    kolone_poz = [
        ("laser_sati", "INTEGER DEFAULT 0"), ("laser_minute", "INTEGER DEFAULT 0"), 
        ("bravarija_sati", "INTEGER DEFAULT 0"), ("bravarija_minute", "INTEGER DEFAULT 0"),
        ("ciljana_kolicina", "INTEGER DEFAULT 0"),
        ("laser_priprema_sati", "INTEGER DEFAULT 0"), ("laser_priprema_minute", "INTEGER DEFAULT 0"),
        ("laser_rezanje_sati", "INTEGER DEFAULT 0"), ("laser_rezanje_minute", "INTEGER DEFAULT 0"),
        ("bravarija_priprema_sati", "INTEGER DEFAULT 0"), ("bravarija_priprema_minute", "INTEGER DEFAULT 0"),
        ("bravarija_piganje_sati", "INTEGER DEFAULT 0"), ("bravarija_piganje_minute", "INTEGER DEFAULT 0")
    ]
    for kol, tip in kolone_poz:
        try: conn.execute(f"ALTER TABLE nalog_pozicije ADD COLUMN {kol} {tip}")
        except: pass
    
    conn.commit()
    conn.close()

init_db()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def to_int(val):
    try: return int(val) if val else 0
    except: return 0

def parsiraj_listu_datoteka(raw_data):
    """Pomagalo koje pretvara tekstualni zapis iz baze u čistu listu datoteka s ekstenzijama."""
    if not raw_data:
        return []
    try:
        datoteke = json.loads(raw_data)
        if isinstance(datoteke, list):
            return [{"filename": d, "ext": d.split('.')[-1].upper()} for d in datoteke]
    except:
        pass
    return [{"filename": raw_data, "ext": raw_data.split('.')[-1].upper()}]

ADMIN_USER = "admin"
ADMIN_PASS = "firstcutlaser1"

@app.before_request
def provjera_pristupa():
    dozvoljeno_svima = ['/prijava_stanice', '/logo.png', '/favicon.png', '/favicon.ico', '/postavi_stanicu', '/odjava_stanice']
    
    if any(request.path.startswith(d) for d in dozvoljeno_svima):
        return
        
    z_stanica = request.cookies.get('zakljucana_stanica')
    
    if not z_stanica:
        return redirect(url_for('prijava_stanice'))
        
    if request.path == '/':
        return
        
    if z_stanica in ['laser1', 'laser2']:
        dozvoljene_rute = ['/laser', '/zapocni_fazu', '/dodaj_poziciju', '/obrisi_poziciju', '/preuzmi']
        if not any(request.path.startswith(r) for r in dozvoljene_rute):
            return redirect('/laser')
            
    elif z_stanica == 'bravarija':
        dozvoljene_rute = ['/bravarija', '/zapocni_fazu', '/dodaj_poziciju', '/obrisi_poziciju', '/preuzmi']
        if not any(request.path.startswith(r) for r in dozvoljene_rute):
            return redirect('/bravarija')
            
    elif z_stanica == 'monitor':
        if not request.path.startswith('/preuzmi'):
            return redirect('/')

@app.context_processor
def inject_globalne_varijable():
    lokalna_stanica = request.cookies.get('stanica_lokalno', 'dashboard')
    z_stanica = request.cookies.get('zakljucana_stanica', 'uprava')
    je_klijent = request.cookies.get('is_client') == 'true'
    
    ua = request.headers.get('User-Agent', '').lower()
    is_mobile = any(w in ua for w in ['mobi', 'android', 'iphone', 'ipad', 'ipod', 'windows phone'])
    
    return dict(stanica=lokalna_stanica, prikazi_klijent_gumbe=je_klijent, is_server_window=False, z_stanica=z_stanica, is_mobile=is_mobile)

BODY_OPEN_TAG = """<body class="{% if prikazi_klijent_gumbe %}is-client{% else %}is-browser{% endif %} {% if is_mobile %}is-mobile-device{% else %}is-desktop-device{% endif %}">"""

STIL_I_NAVIGACIJA = """
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>FirstCutLaser Pogon</title>
    <link rel="icon" type="image/png" href="/favicon.png">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <style>
        body { background-color: #0b0c10; font-family: 'Plus Jakarta Sans', sans-serif; color: #e2e8f0; overflow-y: scroll; overflow-x: hidden; }
        .glavni-prostor { padding: 24px 30px; }
        .navbar { background-color: #12141c; border-bottom: 3px solid #ff0000; box-shadow: 0 4px 25px rgba(0,0,0,0.5); padding: 14px 24px; border-radius: 8px; margin-bottom: 15px; }
        .navbar-brand img { height: 42px; width: auto; object-fit: contain; }
        .card { background: #12141c; border-radius: 14px; border: 1px solid #222736; box-shadow: 0 6px 20px rgba(0,0,0,0.4); margin-bottom: 24px; transition: all 0.3s ease; }
        .card-header-custom { padding: 18px 24px; border-bottom: 1px solid #222736; background: #171b26; color: #ffffff; border-top-left-radius: 14px; border-top-right-radius: 14px; }
        .table { color: #ffffff; border-color: #222736; margin-bottom: 0; }
        
        .table th { background-color: #171b26; color: #f1f5f9 !important; font-weight: 700; text-transform: uppercase; font-size: 0.8rem; padding: 14px; border-bottom: 2px solid #222736; letter-spacing: 0.03em; }
        .table td { padding: 14px; vertical-align: middle; background-color: #12141c; border-bottom: 1px solid #222736; }
        .text-muted { color: #cbd5e1 !important; }
        .text-info { color: #0dcaf0 !important; } 
        .text-white { color: #ffffff !important; }
        .form-label { color: #f1f5f9 !important; font-weight: 600; font-size: 0.85rem; letter-spacing: 0.02em; }
        .form-control::placeholder { color: #94a3b8 !important; opacity: 1; }
        
        .form-control { background-color: #1a1e2b; border: 1px solid #2d3446; color: #ffffff; border-radius: 8px; padding: 10px 14px; font-size: 0.9rem; transition: all 0.2s; }
        .form-control:focus { background-color: #23293b; border-color: #0dcaf0; color: #fff; box-shadow: 0 0 0 3px rgba(13,202,240,0.15); outline: none; }
        
        textarea.auto-expand { resize: none; overflow-y: hidden; min-height: 45px; max-height: 150px; }
        textarea.auto-expand:focus { overflow-y: auto; }

        input[type="file"].form-control { padding: 10px 14px; color: #ffffff !important; line-height: 1.5; background-color: #1a1e2b !important; border: 1px solid #2d3446 !important; font-size: 0.9rem; height: auto; }
        input[type="file"].form-control::file-selector-button { margin-left: -5px; margin-right: 14px; background-color: #2d3446 !important; color: #ffffff !important; border: none; border-radius: 4px; padding: 6px 12px; cursor: pointer; transition: all 0.2s ease; font-weight: 600; font-size: 0.85rem; }
        input[type="file"].form-control:hover { background-color: #1a1e2b !important; border-color: #0dcaf0 !important; }
        input[type="file"].form-control::file-selector-button:hover { background-color: #0dcaf0 !important; color: #0b0c10 !important; }

        .table .form-control { background-color: transparent; border: 1px solid transparent; padding: 8px 10px; border-radius: 6px; color: #ffffff !important; }
        .table .form-control:focus { background-color: rgba(255,255,255,0.03); border: 1px solid #31384f; }
        
        .btn-primary { background-color: #ff0000; border-color: #ff0000; font-weight: 600; border-radius: 8px; padding: 10px 20px; }
        .btn-success { background-color: #16a34a; border-color: #16a34a; font-weight: 600; border-radius: 8px; color: white !important;}
        .btn-warning { background-color: #ea580c; border-color: #ea580c; color: #fff !important; font-weight: 600; border-radius: 8px; }
        .btn-nav { color: #d1d5db; font-weight: 600; text-decoration: none; padding: 8px 16px; border-radius: 6px; white-space: nowrap; }
        .clock-box { background: #171b26; border: 1px solid #222736; border-radius: 8px; padding: 6px 14px; font-family: monospace; color: #ffffff; display: flex; align-items: center; gap: 8px; white-space: nowrap; }
        .vidljiv-tekst { background: rgba(255,255,255,0.05); border: 1px dashed rgba(255,255,255,0.2); border-radius: 8px; padding: 20px; color: #ffffff !important; font-size: 1.1rem; }
        .login-wrapper { min-height: 80vh; display: flex; align-items: center; justify-content: center; }
        .login-box { width: 100%; max-width: 420px; background: #12141c; border-radius: 16px; border-top: 5px solid #0dcaf0; box-shadow: 0 10px 30px rgba(0,0,0,0.6); padding: 40px; }
        
        .tamni-kontejner { background-color: #12141c; border: 1px solid #222736; border-radius: 8px; }
        .dodaj-kontejner { background-color: #171b26; border: 1px solid #2d3446; border-radius: 8px; }
        .napomena-box { background: rgba(13, 202, 240, 0.1); border-left: 3px solid #0dcaf0; padding: 12px; border-radius: 0 6px 6px 0; margin-top: 8px;}
        
        .dropdown-item:hover { background-color: #23293b; color: #fff !important; }
        
        .is-desktop-device .nav-mob-col { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 5px; }
        .is-desktop-device .nav-mob-col::-webkit-scrollbar { height: 5px; }
        .is-desktop-device .nav-mob-col::-webkit-scrollbar-thumb { background-color: #2d3446; border-radius: 4px; }
        
        .is-mobile-device .nav-mob-col { flex-wrap: wrap; }

        @media (max-width: 991px) {
            .is-mobile-device .navbar-collapse { background: #1a1e2b; padding: 18px; border-radius: 8px; margin-top: 15px; border: 1px solid #2d3446; box-shadow: 0 8px 20px rgba(0,0,0,0.5); z-index: 1000; }
            .is-mobile-device .nav-mob-col { flex-direction: column !important; align-items: stretch !important; margin: 0 !important; width: 100% !important; gap: 8px !important; }
            .is-mobile-device .nav-mob-col .btn { text-align: center !important; margin: 0 !important; width: 100% !important; padding: 12px; font-size: 1.05rem; justify-content: center; display: flex; align-items: center; }
            .is-mobile-device .nav-mob-col .clock-box { justify-content: center; margin: 0 !important; width: 100%; font-size: 1.2rem; padding: 12px; }
            .is-mobile-device .navbar-toggler { border: none; outline: none; box-shadow: none; padding: 0; }
            
            .glavni-prostor { padding: 15px 10px; }
            .card-header-custom { padding: 15px !important; }
            .card-body { padding: 15px !important; }
            
            .flex-mob-col { flex-direction: column !important; align-items: flex-start !important; gap: 12px; }
            .flex-mob-col > div { width: 100%; text-align: left !important; }
            .flex-mob-col .btn, .flex-mob-col .badge { width: 100%; display: block; text-align: center; }
            
            .mob-full-btn { display: block; width: 100%; margin-bottom: 8px; text-align: left; margin-right: 0 !important;}
            
            .mob-col-btn { flex-direction: column !important; align-items: stretch !important; padding-top: 15px !important; }
            .mob-col-btn .btn { width: 100%; margin: 5px 0 !important; }
            
            .btn-rutiranje-grupa { flex-direction: column !important; display: flex; }
            .btn-rutiranje-grupa button { width: 100%; margin-bottom: 8px; margin-right: 0 !important; }
            
            .tamni-kontejner { padding: 0 !important; border: none; }
        }
    </style>
    <script>
        if (!Element.prototype.matches) {
            Element.prototype.matches = Element.prototype.msMatchesSelector || Element.prototype.webkitMatchesSelector;
        }
        if (!Element.prototype.closest) {
            Element.prototype.closest = function(s) {
                var el = this;
                while (el && el.nodeType === 1) {
                    if (el.matches(s)) return el;
                    el = el.parentElement || el.parentNode;
                }
                return null;
            };
        }

        // ---------- KOSARICA POZICIJA ----------
        var kosaricaStavke = [];

        function inicijalizirajKosaricu() {
            var spremljeno = localStorage.getItem('fcl_kosarica');
            if (spremljeno) {
                try { kosaricaStavke = JSON.parse(spremljeno); } catch(e) { kosaricaStavke = []; }
            }
            osvjeziKosaricu();
            
            var cartNaziv = document.getElementById('cart_naziv');
            var cartKomada = document.getElementById('cart_komada');
            if(cartNaziv) {
                cartNaziv.addEventListener('keydown', function(e) {
                    if(e.key === 'Enter') { e.preventDefault(); dodajUKosaricu(); }
                });
            }
            if(cartKomada) {
                cartKomada.addEventListener('keydown', function(e) {
                    if(e.key === 'Enter') { e.preventDefault(); dodajUKosaricu(); }
                });
            }
        }

        function dodajUKosaricu() {
            var naziv = document.getElementById('cart_naziv').value.trim();
            var komada = document.getElementById('cart_komada').value;
            if(!naziv) return; 
            kosaricaStavke.push({naziv: naziv, komada: parseInt(komada) || 0});
            document.getElementById('cart_naziv').value = '';
            document.getElementById('cart_komada').value = '';
            document.getElementById('cart_naziv').focus();
            osvjeziKosaricu();
        }

        function ukloniIzKosarice(index) {
            kosaricaStavke.splice(index, 1);
            osvjeziKosaricu();
        }

        function osvjeziKosaricu() {
            var tbl = document.getElementById('cart_table');
            var body = document.getElementById('cart_body');
            var hidden = document.getElementById('kosarica_data');
            if (!tbl || !body || !hidden) return; 
            
            body.innerHTML = '';
            if(kosaricaStavke.length === 0) {
                tbl.style.display = 'none';
            } else {
                tbl.style.display = 'table';
                for(var i=0; i<kosaricaStavke.length; i++) {
                    var s = kosaricaStavke[i];
                    var kolicinaText = s.komada > 0 ? s.komada + ' kom' : '-';
                    body.innerHTML += '<tr><td class="text-info fw-bold">' + s.naziv + '</td><td class="text-white fw-bold" style="font-size: 0.95rem;">' + kolicinaText + '</td><td class="text-end"><button type="button" class="btn btn-sm btn-outline-danger py-0 px-2" onclick="ukloniIzKosarice('+i+')"><i class="fa-solid fa-trash"></i></button></td></tr>';
                }
            }
            hidden.value = JSON.stringify(kosaricaStavke);
            localStorage.setItem('fcl_kosarica', JSON.stringify(kosaricaStavke));
        }

        function osvjeziTimere() {
            var elementiVremena = document.querySelectorAll('.timer-pogona');
            for (var i = 0; i < elementiVremena.length; i++) {
                var el = elementiVremena[i];
                var ISOvrijeme = el.getAttribute('data-start');
                if(ISOvrijeme) {
                    if(ISOvrijeme.indexOf('Z') === -1 && ISOvrijeme.indexOf('+') === -1) {
                        ISOvrijeme += 'Z';
                    }
                    var pocetak = new Date(ISOvrijeme);
                    var razlikaMs = new Date() - pocetak;
                    if (razlikaMs > 0) {
                        var ukupnoSekundi = Math.floor(razlikaMs / 1000);
                        var minuti = Math.floor(ukupnoSekundi / 60);
                        var sekunde = ukupnoSekundi % 60;
                        el.innerHTML = minuti + "m " + sekunde + "s";
                    } else {
                        el.innerHTML = "0m 0s";
                    }
                }
            }
        }

        function azurirajBrojacDatoteka(input, labelId, iconClass, labelText) {
            var label = document.getElementById(labelId);
            if(!label) return;
            if(input.files && input.files.length > 0) {
                var html = '<div class="mt-2"><i class="fa-solid ' + iconClass + ' text-info me-1"></i> <span class="text-muted small">' + labelText + ':</span></div><div class="d-flex flex-wrap gap-1 mt-1">';
                for(var i=0; i<input.files.length; i++) {
                    html += '<span class="badge bg-dark border border-secondary text-info">' + input.files[i].name + '</span>';
                }
                html += '</div>';
                label.innerHTML = html;
            } else {
                label.innerHTML = '';
            }
        }

        var isSubmitting = false;

        function pokreniZiveElemente() {
            function osvjeziSat() {
                var sad = new Date();
                var el = document.getElementById('mreza-sat');
                if(el) {
                    var h = sad.getHours(), m = sad.getMinutes(), s = sad.getSeconds();
                    el.innerText = (h<10?'0'+h:h) + ":" + (m<10?'0'+m:m) + ":" + (s<10?'0'+s:s);
                }
            }
            osvjeziSat();
            setInterval(osvjeziSat, 1000);

            osvjeziTimere();
            setInterval(osvjeziTimere, 1000);
            
            postaviEnterNavigacijuTabela();
            postaviEnterNavigacijuKreiranje();
            initAutoSave();
            inicijalizirajKosaricu();

            var forms = document.querySelectorAll('form');
            for(var f=0; f<forms.length; f++) {
                forms[f].addEventListener('submit', function() {
                    isSubmitting = true;
                });
            }

            setInterval(function() {
                var activeElement = document.activeElement;
                var isTyping = activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA');

                var hasFiles = false;
                var fileInputs = document.querySelectorAll('input[type="file"]');
                for(var k=0; k<fileInputs.length; k++) {
                    if(fileInputs[k].files.length > 0) hasFiles = true;
                }
                
                var isDropdownOpen = document.querySelector('.dropdown-menu.show') !== null;

                if(!isTyping && !isSubmitting && !hasFiles && !isDropdownOpen && window.location.pathname !== '/login' && window.location.pathname !== '/prijava_stanice') {
                    var xhr = new XMLHttpRequest();
                    xhr.open('GET', window.location.href, true);
                    xhr.onreadystatechange = function() {
                        if (xhr.readyState === 4 && xhr.status === 200) {
                            try {
                                var parser = new DOMParser();
                                var newDoc = parser.parseFromString(xhr.responseText, 'text/html');

                                var currentMain = document.querySelector('.glavni-prostor');
                                var newMain = newDoc.querySelector('.glavni-prostor');

                                if (currentMain && newMain) {
                                    var scrollPos = window.scrollY || window.pageYOffset || document.documentElement.scrollTop;

                                    var scrollables = document.querySelectorAll('.table-responsive, .tamni-kontejner');
                                    var scrollLefts = [];
                                    for (var s = 0; s < scrollables.length; s++) {
                                        scrollLefts.push(scrollables[s].scrollLeft);
                                    }

                                    var otvorenihCollapse = [];
                                    var collapses = document.querySelectorAll('.collapse.show');
                                    for (var i = 0; i < collapses.length; i++) {
                                        otvorenihCollapse.push(collapses[i].id);
                                    }

                                    currentMain.innerHTML = newMain.innerHTML;
                                    
                                    window.scrollTo(0, scrollPos);
                                    for (var j = 0; j < otvorenihCollapse.length; j++) {
                                        var el = document.getElementById(otvorenihCollapse[j]);
                                        if(el) el.classList.add('show');
                                    }

                                    var newScrollables = document.querySelectorAll('.table-responsive, .tamni-kontejner');
                                    for (var s = 0; s < newScrollables.length; s++) {
                                        if(scrollLefts[s] !== undefined) {
                                            newScrollables[s].scrollLeft = scrollLefts[s];
                                        }
                                    }

                                    var newForms = document.querySelectorAll('form');
                                    for(var x=0; x<newForms.length; x++) {
                                        newForms[x].addEventListener('submit', function() {
                                            isSubmitting = true;
                                        });
                                    }

                                    if (typeof initAutoSave === 'function') initAutoSave();
                                    if (typeof postaviEnterNavigacijuTabela === 'function') postaviEnterNavigacijuTabela();
                                    if (typeof postaviEnterNavigacijuKreiranje === 'function') postaviEnterNavigacijuKreiranje();
                                    if (typeof inicijalizirajKosaricu === 'function') inicijalizirajKosaricu();
                                    osvjeziTimere();
                                }
                            } catch(err) {
                                console.log("Pozadinsko ažuriranje preskočeno: " + err);
                            }
                        }
                    };
                    xhr.send();
                }
            }, 2000);
        }
        
        function autoProsiri(el) {
            el.style.height = 'inherit';
            var computed = el.scrollHeight;
            var novaVisina = computed + 24; 
            if (novaVisina > 150) novaVisina = 150; 
            el.style.height = novaVisina + 'px';
        }

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Tab') {
                var activeEl = document.activeElement;
                if (activeEl && activeEl.tagName === 'INPUT' && activeEl.placeholder === '0' && activeEl.value === '') {
                    activeEl.value = '0';
                    if ("createEvent" in document) {
                        var evt = document.createEvent("HTMLEvents");
                        evt.initEvent("input", false, true);
                        activeEl.dispatchEvent(evt);
                    } else {
                        activeEl.fireEvent("oninput");
                    }
                }
            }
        });

        function initAutoSave() {
            var inputs = document.querySelectorAll('input[type="text"], input[type="number"], textarea');
            for (var i = 0; i < inputs.length; i++) {
                (function(input) {
                    if (!input.name || input.type === 'file' || input.type === 'hidden') return;
                    if (input.id === 'cart_naziv' || input.id === 'cart_komada') return; 
                    
                    var form = input.closest('form');
                    var nalogIdInput = form ? form.querySelector('input[name="id_naloga"]') : null;
                    var key = "fcl_" + window.location.pathname + "_";
                    if (nalogIdInput) key += "nalog_" + nalogIdInput.value + "_";
                    key += input.name;
                    
                    var saved = localStorage.getItem(key);
                    if (saved !== null) {
                        input.value = saved;
                        if(input.tagName.toLowerCase() === 'textarea') autoProsiri(input);
                    }
                    
                    input.addEventListener('input', function() {
                        localStorage.setItem(key, input.value);
                    });
                })(inputs[i]);
            }

            var forms = document.querySelectorAll('form');
            for (var j = 0; j < forms.length; j++) {
                forms[j].addEventListener('submit', function(e) {
                    var currentForm = e.target || e.srcElement;
                    var formInputs = currentForm.querySelectorAll('input, textarea');
                    for (var k = 0; k < formInputs.length; k++) {
                        var input = formInputs[k];
                        if (!input.name) continue;
                        var nalogIdInput = currentForm.querySelector('input[name="id_naloga"]');
                        var key = "fcl_" + window.location.pathname + "_";
                        if (nalogIdInput) key += "nalog_" + nalogIdInput.value + "_";
                        key += input.name;
                        localStorage.removeItem(key);
                    }
                    localStorage.removeItem('fcl_kosarica');
                    kosaricaStavke = [];
                });
            }
        }

        function postaviEnterNavigacijuTabela() {
            var forms = document.querySelectorAll('.nav-forma');
            for (var i = 0; i < forms.length; i++) {
                (function(form) {
                    var inputs = form.querySelectorAll('.navigabilno');
                    for (var j = 0; j < inputs.length; j++) {
                        (function(input, index) {
                            input.addEventListener('keydown', function(e) {
                                if (e.key === 'Enter') {
                                    e.preventDefault();
                                    if (index < inputs.length - 1) {
                                        inputs[index + 1].focus();
                                    } else {
                                        input.blur(); 
                                    }
                                }
                            });
                        })(inputs[j], j);
                    }
                })(forms[i]);
            }
        }
        
        function postaviEnterNavigacijuKreiranje() {
            var formKreiranje = document.getElementById('form-kreiranje');
            if(formKreiranje) {
                var inputNodes = formKreiranje.querySelectorAll('.kreiranje-nav');
                var inputs = [];
                for(var i=0; i<inputNodes.length; i++) inputs.push(inputNodes[i]);
                
                for (var j = 0; j < inputs.length; j++) {
                    (function(input, index) {
                        input.addEventListener('keydown', function(e) {
                            if (e.key === 'Enter') {
                                e.preventDefault();
                                e.stopPropagation(); 
                                if (index < inputs.length - 1) {
                                    inputs[index + 1].focus();
                                } else {
                                    input.blur(); 
                                }
                            }
                        });
                    })(inputs[j], j);
                }
            }
        }

        if (window.location.pathname === '/sefo_panel' || window.location.pathname === '/postavke') {
            if (sessionStorage.getItem('admin_prijavljen') !== 'da') {
                window.location.href = '/login';
            }
        }

        window.onload = pokreniZiveElemente;
    </script>
</head>
"""

NAVBAR_TEMPLATE = """
<nav class="navbar {% if is_mobile %}navbar-expand-lg{% else %}navbar-expand{% endif %} navbar-dark">
    <div class="container-fluid">
        <a class="navbar-brand d-flex align-items-center" href="/"><img src="/logo.png" alt="FirstCutLaser"></a>
        
        {% if is_mobile %}
        <button class="navbar-toggler border-0 shadow-none px-0" type="button" data-bs-toggle="collapse" data-bs-target="#fclNav">
            <i class="fa-solid fa-bars text-info fs-3"></i>
        </button>
        {% endif %}
        
        <div class="{% if is_mobile %}collapse navbar-collapse{% else %}d-flex flex-grow-1 align-items-center{% endif %}" id="fclNav">
            <div class="d-flex gap-3 ms-auto align-items-center nav-mob-col">
                
                <a href="/" class="btn btn-nav text-white-50"><i class="fa-solid fa-desktop me-2"></i> Monitor Pogona</a>
                
                {% if z_stanica in ['uprava', 'laser1', 'laser2'] %}
                    <a href="/laser" class="btn btn-nav text-danger"><i class="fa-solid fa-fire me-2"></i> Rezanje</a>
                {% endif %}
                
                {% if z_stanica in ['uprava', 'bravarija'] %}
                    <a href="/bravarija" class="btn btn-nav text-warning"><i class="fa-solid fa-hammer me-2"></i> Bravarija</a>
                {% endif %}
                
                {% if z_stanica == 'uprava' %}
                    <a href="/sefo_panel" class="btn btn-nav text-info border border-info border-opacity-25"><i class="fa-solid fa-user-gear me-2"></i> Upravljačka Ploča</a>
                {% endif %}
                
                <div class="clock-box" id="mreza-sat">00:00:00</div>
                
                {% if session.get('role') == 'Admin' %}
                    <a href="/postavke" class="btn btn-sm btn-outline-secondary text-white-50 px-3 py-2" title="Postavke Aplikacije"><i class="fa-solid fa-gear me-1"></i> Postavke</a>
                    <a href="/logout" class="btn btn-sm btn-outline-secondary text-white-50 px-3 py-2" title="Odjava"><i class="fa-solid fa-power-off me-1"></i> Odjava</a>
                {% endif %}
                
                {% if is_server_window %}
                    <button onclick="if(window.pywebview && window.pywebview.api && window.pywebview.api.izadji) { if(confirm('Želite li potpuno ugasiti server aplikaciju?')) pywebview.api.izadji(); } else { window.location.href='/ugasi_program'; }" class="btn btn-sm btn-danger fw-bold px-4 py-2"><i class="fa-solid fa-power-off me-1"></i> IZLAZ</button>
                {% elif prikazi_klijent_gumbe %}
                    <button id="klijent-postavke-btn" class="btn btn-sm btn-outline-secondary text-white-50 d-none" title="Postavke Klijenta (F12)" onclick="if(window.pywebview && window.pywebview.api && window.pywebview.api.reset_postavki) { if(confirm('Želite li resetirati IP i stanicu? Program će se ugasiti.')) pywebview.api.reset_postavki(); }"><i class="fa-solid fa-gear"></i></button>
                    <button onclick="if(window.pywebview && window.pywebview.api && window.pywebview.api.izadji) { if(confirm('Želite li potpuno ugasiti klijent aplikaciju?')) pywebview.api.izadji(); }" class="btn btn-sm btn-danger fw-bold px-4 py-2"><i class="fa-solid fa-power-off me-1"></i> IZLAZ</button>
                {% elif session.get('role') == 'Admin' %}
                    <a href="/ugasi_program" class="btn btn-sm btn-danger fw-bold px-4 py-2" title="Potpuno ugasi aplikaciju na Serveru"><i class="fa-solid fa-power-off me-1"></i> IZLAZ</a>
                {% endif %}
                
                {% if not prikazi_klijent_gumbe %}
                    <a href="/odjava_stanice" class="btn btn-sm btn-outline-danger px-3 py-2 ms-1" title="Zaključaj uređaj i izađi"><i class="fa-solid fa-lock"></i></a>
                {% endif %}
            </div>
        </div>
    </div>
</nav>
"""

@app.route('/logo.png')
def serve_logo(): return send_from_directory(BASE_DIR, 'logo.png')

@app.route('/favicon.png')
def serve_favicon(): return send_from_directory(BASE_DIR, 'favicon.png')

@app.route('/favicon.ico')
def favicon_fallback(): return redirect(url_for('serve_favicon'))

@app.route('/prijava_stanice', methods=['GET', 'POST'])
def prijava_stanice():
    conn = get_db_connection()
    
    prava_ip = 'nepoznato'
    if request.headers.get('X-Forwarded-For'):
        prava_ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        prava_ip = request.headers.get('X-Real-IP').strip()
    else:
        prava_ip = request.remote_addr or 'nepoznato'
        
    preglednik = request.headers.get('User-Agent', 'nepoznat_preglednik')
    otisak_uređaja = hashlib.md5(f"{prava_ip}_{preglednik}".encode('utf-8')).hexdigest()
    
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
        uneseni_pin = request.form.get('pin', '')
        odabir = request.form.get('vrsta_stanice')
        uneseni_hash = hashlib.sha256(uneseni_pin.encode()).hexdigest()
        
        if uneseni_hash == PIN_HASH:
            conn.execute("INSERT OR REPLACE INTO blokade (ip_adresa, pokusaji, blokiran_do) VALUES (?, 0, NULL)", (otisak_uređaja,))
            conn.commit()
            conn.close()
            
            cilj = '/'
            if odabir in ['laser1', 'laser2']: cilj = '/laser'
            elif odabir == 'bravarija': cilj = '/bravarija'
            
            resp = make_response(redirect(cilj))
            resp.set_cookie('zakljucana_stanica', odabir, max_age=60*60*24*365)
            
            lok_stanica = 'dashboard'
            if odabir != 'uprava' and odabir != 'monitor':
                lok_stanica = odabir
            resp.set_cookie('stanica_lokalno', lok_stanica, max_age=60*60*24*365)
            return resp
        else:
            pokusaji = (blokada['pokusaji'] if blokada else 0) + 1
            if pokusaji >= 5:
                blokiran_do = (sada + timedelta(hours=24)).isoformat()
                conn.execute("INSERT OR REPLACE INTO blokade (ip_adresa, pokusaji, blokiran_do) VALUES (?, ?, ?)", (otisak_uređaja, pokusaji, blokiran_do))
                poruka = "ZAKLJUČANO! Previše pogrešnih unosa PIN-a. Vaš uređaj je zaključan na 24 sata."
                blokada_aktivna = True
            else:
                conn.execute("INSERT OR REPLACE INTO blokade (ip_adresa, pokusaji, blokiran_do) VALUES (?, ?, NULL)", (otisak_uređaja, pokusaji))
                poruka = f"Neispravan sigurnosni PIN. Preostalo pokušaja: {5 - pokusaji}"
            conn.commit()
            
    conn.close()
            
    html = "<!DOCTYPE html><html>" + STIL_I_NAVIGACIJA + BODY_OPEN_TAG + """
        <div class="login-wrapper">
            <div class="login-box text-center">
                <img src="/logo.png" class="mb-4 mt-2" style="max-height: 65px;">
                <h5 class="fw-bold mb-1 text-danger text-uppercase"><i class="fa-solid fa-shield-halved me-2"></i>Pristup Pogonu</h5>
                <p class="text-muted small mb-4">Uređaj nije prepoznat. Prijavite stanicu.</p>
                
                {% if blokada_aktivna %}
                    <div class="alert alert-danger p-3 fw-bold fs-5 shadow-sm border border-danger">
                        <i class="fa-solid fa-lock me-2 text-danger"></i> ZABRANJEN PRISTUP<br>
                        <small class="fs-6 fw-normal d-block mt-2 text-white">{{ poruka }}</small>
                    </div>
                {% else %}
                    {% if poruka %}<div class="alert alert-warning py-2 small fw-bold text-dark border border-warning">{{ poruka }}</div>{% endif %}
                    <form method="POST">
                        <div class="mb-3 text-start">
                            <label class="form-label">Vrsta uređaja / Lokacija</label>
                            <select name="vrsta_stanice" class="form-control" style="cursor: pointer;">
                                <option value="uprava">Slobodan Pristup (Uprava)</option>
                                <option value="monitor">Monitor Pogona (Samo pregled)</option>
                                <option value="laser1">Laser 1 (Glavni)</option>
                                <option value="laser2">Laser 2</option>
                                <option value="bravarija">Bravarija</option>
                            </select>
                        </div>
                        <div class="mb-4 text-start">
                            <label class="form-label">Glavni PIN Pogona</label>
                            <input type="password" class="form-control text-center fs-5 tracking-widest" name="pin" placeholder="••••" required autocomplete="off">
                        </div>
                        <button type="submit" class="btn btn-danger w-100 py-2 fw-bold mb-2 text-uppercase"><i class="fa-solid fa-unlock-keyhole me-2"></i>Otključaj Sustav</button>
                    </form>
                {% endif %}
            </div>
        </div>
    </body></html>"""
    return render_template_string(html, poruka=poruka, blokada_aktivna=blokada_aktivna)

@app.route('/odjava_stanice')
def odjava_stanice():
    resp = make_response(redirect(url_for('prijava_stanice')))
    resp.set_cookie('zakljucana_stanica', '', expires=0)
    resp.set_cookie('stanica_lokalno', '', expires=0)
    session.clear()
    return resp

@app.route('/ugasi_program')
def ugasi_program():
    return redirect(url_for('index_pogon_hub'))

@app.route('/postavi_stanicu/<ime_stanice>')
def postavi_stanicu(ime_stanice):
    mapa_autorizacije = {
        'dashboard': 'uprava',
        'laser1': 'laser1',
        'laser2': 'laser2',
        'bravarija': 'bravarija'
    }
    z_ime = mapa_autorizacije.get(ime_stanice, 'uprava')
    cilj = url_for('index_pogon_hub') if z_ime in ['uprava', 'monitor'] else url_for('sekcija_laser') if 'laser' in z_ime else url_for('sekcija_bravarija')
    
    resp = make_response(redirect(cilj))
    resp.set_cookie('stanica_lokalno', ime_stanice, max_age=60*60*24*365)
    resp.set_cookie('zakljucana_stanica', z_ime, max_age=60*60*24*365)
    
    if request.args.get('client') == 'true':
        resp.set_cookie('is_client', 'true', max_age=60*60*24*365)
        
    return resp

@app.route('/')
def index_pogon_hub():
    conn = get_db_connection()
    svi_nalozi = conn.execute('SELECT * FROM radni_nalozi ORDER BY id DESC').fetchall()
    trenutno_na_laseru = [dict(n) for n in svi_nalozi if n['status'] == 'Laser' and n['laser_zapoceto_u']]
    trenutno_u_bravariji = [dict(n) for n in svi_nalozi if n['status'] == 'Piganje' and n['bravarija_zapoceto_u']]
    conn.close()
    
    sadrzaj = """
    <div class="container-fluid glavni-prostor">
        <div class="p-4 rounded-3 mb-4 text-center" style="background: linear-gradient(135deg, #12141c 0%, #171c28 100%); border: 1px solid #222736;">
            <img src="/logo.png" style="max-height: 55px;" class="mb-2"><br>
            <h2 class="fw-bold text-white mb-1">CENTRALNI MONITOR POGONA</h2>
        </div>
        <div class="row mb-4 align-items-stretch">
            <div class="col-md-6 mb-3">
                <div class="card h-100 mb-0" style="border-left: 4px solid #ff0000;">
                    <div class="card-header-custom d-flex justify-content-between align-items-center flex-mob-col">
                        <h6 class="mb-0 fw-bold text-danger text-uppercase"><i class="fa-solid fa-fire pulse-live me-2"></i>Laser &bull; Trenutno u rezanju</h6>
                    </div>
                    <div class="card-body p-4">
                        {% if not trenutno_na_laseru %}<p class="text-center my-4 vidljiv-tekst">Trenutno nema aktivnih naloga u procesu rezanja.</p>
                        {% else %}
                            {% for n in trenutno_na_laseru %}
                            <div class="p-3 rounded bg-dark bg-opacity-25 mb-2 d-flex justify-content-between align-items-center flex-mob-col">
                                <div>
                                    <b class="text-white fs-5">{{ n.naziv_naloga }}</b>
                                    {% if n.odabrani_laser %} <span class="badge bg-danger ms-2 border border-danger"><i class="fa-solid fa-crosshairs me-1"></i>{{ n.odabrani_laser|upper }}</span>{% endif %}<br>
                                    <small class="text-muted">Projekt: {{ n.naziv_projekta }} {% if n.debljina_ploce %}[D: {{ n.debljina_ploce }}]{% endif %} | Nalog #{{ n.id }}</small>
                                </div>
                                <span class="badge bg-dark border border-danger text-danger p-2 fs-6 mt-2"><i class="fa-solid fa-stopwatch me-1"></i> <span class="timer-pogona" data-start="{{ n.laser_zapoceto_u }}">0m 0s</span></span>
                            </div>
                            {% endfor %}
                        {% endif %}
                    </div>
                </div>
            </div>
            <div class="col-md-6 mb-3">
                <div class="card h-100 mb-0" style="border-left: 4px solid #facc15;">
                    <div class="card-header-custom d-flex justify-content-between align-items-center flex-mob-col">
                        <h6 class="mb-0 fw-bold text-warning text-uppercase"><i class="fa-solid fa-hammer pulse-live me-2"></i>Bravarija &bull; Trenutno u obradi</h6>
                    </div>
                    <div class="card-body p-4">
                        {% if not trenutno_u_bravariji %}<p class="text-center my-4 vidljiv-tekst">Trenutno nema aktivnih naloga u bravarskoj obradi.</p>
                        {% else %}
                            {% for n in trenutno_u_bravariji %}
                            <div class="p-3 rounded bg-dark bg-opacity-25 mb-2 d-flex justify-content-between align-items-center flex-mob-col">
                                <div>
                                    <b class="text-white fs-5">{{ n.naziv_naloga }}</b><br>
                                    <small class="text-muted">Projekt: {{ n.naziv_projekta }} {% if n.debljina_ploce %}[D: {{ n.debljina_ploce }}]{% endif %} | Nalog #{{ n.id }}</small>
                                </div>
                                <span class="badge bg-dark border border-warning text-warning p-2 fs-6 mt-2"><i class="fa-solid fa-stopwatch me-1"></i> <span class="timer-pogona" data-start="{{ n.bravarija_zapoceto_u }}">0m 0s</span></span>
                            </div>
                            {% endfor %}
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </div>
    """
    return render_template_string(f"<!DOCTYPE html><html>{STIL_I_NAVIGACIJA}{BODY_OPEN_TAG}{NAVBAR_TEMPLATE}{sadrzaj}</body></html>", trenutno_na_laseru=trenutno_na_laseru, trenutno_u_bravariji=trenutno_u_bravariji)

@app.route('/login', methods=['GET', 'POST'])
def login():
    poruka = None
    if request.method == 'POST':
        if request.form['username'] == ADMIN_USER and request.form['password'] == ADMIN_PASS:
            session['role'] = 'Admin'
            return render_template_string("<script>sessionStorage.setItem('admin_prijavljen', 'da'); window.location.href='/sefo_panel';</script>")
        poruka = "Neispravni podaci za administratora!"
    
    return render_template_string("<!DOCTYPE html><html>" + STIL_I_NAVIGACIJA + BODY_OPEN_TAG + """
        <div class="login-wrapper">
            <div class="login-box text-center position-relative">
                <a href="/" class="btn btn-sm btn-outline-secondary position-absolute top-0 start-0 m-3 border-0 text-muted" title="Nazad na početak">
                    <i class="fa-solid fa-arrow-left fs-5"></i>
                </a>
                <img src="/logo.png" class="mb-4 mt-2" style="max-height: 70px;">
                <h4 class="fw-bold mb-4 text-info">Upravljačka Ploča</h4>
                {% if poruka %}<div class="alert alert-danger py-2 small">{{ poruka }}</div>{% endif %}
                <form method="POST">
                    <div class="mb-3 text-start"><label class="form-label">Korisničko ime</label><input type="text" class="form-control" name="username" placeholder="admin" required></div>
                    <div class="mb-4 text-start"><label class="form-label">Lozinka</label><input type="password" class="form-control" name="password" placeholder="•••••••••" required></div>
                    <button type="submit" class="btn btn-info text-white w-100 py-2 fw-bold mb-2">Prijavi se u Upravljanje</button>
                </form>
            </div>
        </div>
    </body></html>""", poruka=poruka)

@app.route('/logout')
def logout():
    session.clear()
    return render_template_string("<script>sessionStorage.removeItem('admin_prijavljen'); window.location.href='/';</script>")

@app.route('/sefo_panel', methods=['GET', 'POST'])
def index_master():
    if 'role' not in session or session['role'] != 'Admin': return redirect(url_for('login'))
    conn = get_db_connection()
    if request.method == 'POST':
        naziv = request.form['naziv_naloga']
        projekt = request.form.get('naziv_projekta', '')
        kreirao = request.form.get('kreirao', '')
        debljina_ploce = request.form.get('debljina_ploce', '')
        opis = request.form.get('opis', '')
        rutiranje = request.form.get('rutiranje', 'Pogon')
        
        f_pdf_list = request.files.getlist('pdf_file')
        f_lxd_list = request.files.getlist('lxdf_file')
        
        p_names = []
        for f in f_pdf_list:
            if f and allowed_file(f.filename):
                fname = secure_filename(f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                p_names.append(fname)
        p_name_db = json.dumps(p_names) if p_names else None
            
        l_names = []
        for f in f_lxd_list:
            if f and allowed_file(f.filename):
                fname = secure_filename(f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                l_names.append(fname)
        l_name_db = json.dumps(l_names) if l_names else None
        
        pocetni_status = 'Piganje' if rutiranje == 'Samo Bravarija' else 'Laser'
        cursor = conn.execute('INSERT INTO radni_nalozi (naziv_naloga, naziv_projekta, debljina_ploce, pdf_datoteka, lxdf_datoteka, opis, rutiranje, status, kreirao) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                     (naziv, projekt, debljina_ploce, p_name_db, l_name_db, opis, rutiranje, pocetni_status, kreirao))
        nalog_id = cursor.lastrowid
        
        kosarica_data = request.form.get('kosarica_data', '[]')
        try:
            stavke = json.loads(kosarica_data)
            for s in stavke:
                komada = int(s.get('komada', 0))
                conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina, laser_komada, bravarija_komada) VALUES (?, ?, ?, 0, 0)', 
                             (nalog_id, s['naziv'], komada))
        except Exception as e:
            print("Greška pri unosu stavki iz košarice:", e)
            pass
            
        conn.commit()
        return redirect(url_for('index_master'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status != 'Arhivirano' ORDER BY id DESC").fetchall()
    nalozi = []
    for r in nalozi_rows:
        n = dict(r)
        n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        nalozi.append(n)
    conn.close()
    
    glavni_sadrzaj = """
    <div class="container-fluid glavni-prostor">
        <div class="row mb-4">
            <div class="col-12">
                <div class="card border-info">
                    <div class="card-header-custom bg-info bg-opacity-10 text-info border-info border-opacity-25">
                        <h5 class="mb-0 fw-bold"><i class="fa-solid fa-circle-plus me-2"></i>Lansiranje Novog Radnog Naloga</h5>
                    </div>
                    <div class="card-body p-4">
                        <form method="POST" enctype="multipart/form-data" class="row g-3" id="form-kreiranje">
                            
                            <div class="col-lg-3 col-md-6">
                                <label class="form-label">Naziv Firme / Kupca</label>
                                <input type="text" class="form-control kreiranje-nav" name="naziv_naloga" placeholder="npr. IBO Metal..." required>
                            </div>
                            <div class="col-lg-3 col-md-6">
                                <label class="form-label">Naziv Projekta</label>
                                <input type="text" class="form-control kreiranje-nav" name="naziv_projekta" placeholder="npr. Ograda 2. faza...">
                            </div>
                            <div class="col-lg-3 col-md-6">
                                <label class="form-label text-warning">Kreator Naloga</label>
                                <input type="text" class="form-control kreiranje-nav border-warning text-warning fw-bold" name="kreirao" placeholder="Vaše ime..." required oninput="this.value=this.value.replace(/[0-9]/g,'');">
                            </div>
                            <div class="col-lg-3 col-md-6">
                                <label class="form-label">Debljina ploče</label>
                                <input type="text" class="form-control kreiranje-nav" name="debljina_ploce" placeholder="npr. 5 mm, 12 mm...">
                            </div>
                            
                            <div class="col-12 mt-3">
                                <label class="form-label">Dodatni opis i upute za pogon</label>
                                <textarea class="form-control auto-expand kreiranje-nav" name="opis" placeholder="npr. Paziti na ogrebotine, hitno..." oninput="autoProsiri(this)"></textarea>
                            </div>
                            
                            <div class="col-12 mt-4 pt-3 border-top border-secondary border-opacity-25">
                                <h6 class="text-info fw-bold mb-3"><i class="fa-solid fa-cart-flatbed me-2"></i>Dodavanje stavki (Pozicija) u nalog (Opcionalno)</h6>
                                <div class="row g-2 align-items-end mb-3">
                                    <div class="col-md-5">
                                        <label class="form-label small text-muted mb-1">Naziv pozicije s nacrta</label>
                                        <input type="text" id="cart_naziv" class="form-control form-control-sm border-info" placeholder="npr. Nosač A">
                                    </div>
                                    <div class="col-md-3">
                                        <label class="form-label small text-muted mb-1">Potrebno napraviti (kom)</label>
                                        <input type="number" id="cart_komada" class="form-control form-control-sm border-info" placeholder="0" min="0">
                                    </div>
                                    <div class="col-md-4">
                                        <button type="button" class="btn btn-sm btn-info text-white w-100 fw-bold py-2" onclick="dodajUKosaricu()"><i class="fa-solid fa-plus me-1"></i> Dodaj stavku</button>
                                    </div>
                                </div>
                                
                                <div class="table-responsive tamni-kontejner p-0">
                                    <table class="table table-sm text-white mb-0" id="cart_table" style="display: none;">
                                        <thead class="bg-dark text-secondary" style="font-size: 0.8rem;">
                                            <tr><th class="ps-3 py-2">Pozicija</th><th class="py-2">Potrebno Komada</th><th class="text-end pe-3 py-2">Ukloni</th></tr>
                                        </thead>
                                        <tbody id="cart_body"></tbody>
                                    </table>
                                </div>
                                <input type="hidden" name="kosarica_data" id="kosarica_data" value="[]">
                            </div>
                            
                            <div class="col-lg-4 col-md-6 mt-4">
                                <label class="form-label">PDF Nacrti - <small class="text-danger">Možete odabrati više datoteka</small></label>
                                <input type="file" class="form-control kreiranje-nav" name="pdf_file" accept=".pdf" multiple onchange="azurirajBrojacDatoteka(this, 'pdf_brojac_label', 'fa-file-pdf', 'Priloženi PDF nacrti')">
                                <div id="pdf_brojac_label" class="mt-1 small"></div>
                            </div>
                            <div class="col-lg-4 col-md-6 mt-4">
                                <label class="form-label">Strojne datoteke (DXF/LXDS) - <small class="text-info">Možete odabrati više datoteka</small></label>
                                <input type="file" class="form-control kreiranje-nav" name="lxdf_file" accept=".lxds,.dxf" multiple onchange="azurirajBrojacDatoteka(this, 'dxf_brojac_label', 'fa-file-code', 'Priložene strojne datoteke')">
                                <div id="dxf_brojac_label" class="mt-1 small"></div>
                            </div>
                            
                            <div class="col-12 mt-4 d-flex gap-2 btn-rutiranje-grupa">
                                <button type="submit" name="rutiranje" value="Samo Rezanje" class="btn btn-danger flex-fill fw-bold py-2"><i class="fa-solid fa-fire me-2"></i>Šalji na REZANJE</button>
                                <button type="submit" name="rutiranje" value="Samo Bravarija" class="btn btn-warning text-dark flex-fill fw-bold py-2"><i class="fa-solid fa-hammer me-2"></i>Šalji u BRAVARIJU</button>
                                <button type="submit" name="rutiranje" value="Pogon" class="btn btn-info text-white flex-fill fw-bold py-2"><i class="fa-solid fa-industry me-2"></i>Šalji u POGON</button>
                            </div>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="card-header-custom d-flex justify-content-between align-items-center">
                <h5 class="mb-0 fw-bold"><i class="fa-solid fa-list-check text-muted me-2"></i>Glavno Upravljanje Pogonom</h5>
            </div>
            <div class="card-body p-4">
                <div class="table-responsive">
                    <table class="table align-middle">
                        <thead><tr><th>ID</th><th>Kupac / Projekt</th><th>Status / Ruta</th><th style="min-width: 320px;">Upute i Napomene</th><th>Nacrti</th><th>Opcije</th></tr></thead>
                        <tbody>
                            {% for n in nalozi %}
                            <tr>
                                <td class="fs-5 text-muted">#{{ n.id }}</td>
                                <td>
                                    <div class="fs-5 fw-bold text-white">{{ n.naziv_naloga }}</div>
                                    <div class="text-info small">
                                        {{ n.naziv_projekta }}
                                        {% if n.debljina_ploce %} &bull; <span class="badge bg-dark text-info border border-info border-opacity-25">D: {{ n.debljina_ploce }}</span>{% endif %}
                                        {% if n.kreirao %} &bull; <i class="fa-solid fa-user-pen text-warning me-1"></i><span class="text-warning fw-bold">{{ n.kreirao }}</span>{% endif %}
                                        {% if n.dimenzije_ploce_laser %} &bull; <i class="fa-solid fa-ruler-combined text-info me-1"></i><span class="text-info">Ploča: {{ n.dimenzije_ploce_laser }}</span>{% endif %}
                                        {% if n.materijal_ploce_laser %} &bull; <i class="fa-solid fa-layer-group text-info me-1"></i><span class="text-info">Materijal: {{ n.materijal_ploce_laser }}</span>{% endif %}
                                    </div>
                                    {% if n.pozicije %}
                                        <button class="btn btn-sm btn-outline-info mt-2 py-0 px-2" style="font-size:0.75rem;" type="button" data-bs-toggle="collapse" data-bs-target="#detalji-{{ n.id }}">
                                            <i class="fa-solid fa-chevron-down me-1"></i> Detalji i Vrijeme
                                        </button>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if n.status == 'Na pregledu' %}
                                        <span class="badge bg-success py-2 px-3 shadow"><i class="fa-solid fa-check-double me-1"></i> SPREMNO ZA PREGLED</span>
                                    {% else %}
                                        {% set prikaz_rute = 'Samo Rezanje' if n.rutiranje == 'Samo Laser' else n.rutiranje %}
                                        <span class="badge bg-secondary">{{ prikaz_rute }} ({{ n.status }})</span>
                                        {% if n.odabrani_laser and n.status == 'Laser' %}
                                            <span class="badge bg-danger mt-1 d-block w-75">{{ n.odabrani_laser|upper }}</span>
                                        {% endif %}
                                    {% endif %}
                                </td>
                                <td style="max-width: 350px;">
                                    {% if n.opis or n.laser_napomena or n.bravarija_napomena %}
                                        {% set sve_ukupno = (n.opis|length if n.opis else 0) + (n.laser_napomena|length if n.laser_napomena else 0) + (n.bravarija_napomena|length if n.bravarija_napomena else 0) %}
                                        
                                        {% if sve_ukupno > 100 %}
                                            <button class="btn btn-sm btn-outline-info w-100 text-start" type="button" data-bs-toggle="collapse" data-bs-target="#opis-{{ n.id }}">
                                                <i class="fa-solid fa-book-open me-2"></i> Čitaj opise i napomene
                                            </button>
                                            <div class="collapse mt-2" id="opis-{{ n.id }}">
                                                {% if n.opis %}<div class="napomena-box border-info"><b><i class="fa-solid fa-user-tie text-info me-1"></i> <span class="text-info">Upute Poslovođe:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.opis }}</span></div>{% endif %}
                                                {% if n.laser_napomena %}<div class="napomena-box border-danger mt-2"><b><i class="fa-solid fa-fire text-danger me-1"></i> <span class="text-info">Napomena iz Lasera:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.laser_napomena }}</span></div>{% endif %}
                                                {% if n.bravarija_napomena %}<div class="napomena-box border-warning mt-2"><b><i class="fa-solid fa-hammer text-warning me-1"></i> <span class="text-info">Napomena iz Bravarije:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.bravarija_napomena }}</span></div>{% endif %}
                                            </div>
                                        {% else %}
                                            <div>
                                                {% if n.opis %}<div class="napomena-box border-info"><b><i class="fa-solid fa-user-tie text-info me-1"></i> <span class="text-info">Upute Poslovođe:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.opis }}</span></div>{% endif %}
                                                {% if n.laser_napomena %}<div class="napomena-box border-danger mt-2"><b><i class="fa-solid fa-fire text-danger me-1"></i> <span class="text-info">Napomena iz Lasera:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.laser_napomena }}</span></div>{% endif %}
                                                {% if n.bravarija_napomena %}<div class="napomena-box border-warning mt-2"><b><i class="fa-solid fa-hammer text-warning me-1"></i> <span class="text-info">Napomena iz Bravarije:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.bravarija_napomena }}</span></div>{% endif %}
                                            </div>
                                        {% endif %}
                                    {% else %}
                                        <span class="text-muted small">Nema napomena</span>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if n.pdf_datoteke|length == 1 %}
                                        <a href="/preuzmi/{{ n.pdf_datoteke[0].filename }}" class="btn btn-sm btn-outline-danger mob-full-btn mb-1" target="_blank"><i class="fa-solid fa-file-pdf"></i> Otvori PDF</a>
                                    {% elif n.pdf_datoteke|length > 1 %}
                                        <div class="dropdown d-inline-block mob-full-btn mb-1" style="vertical-align: top;">
                                            <button class="btn btn-sm btn-outline-danger dropdown-toggle w-100 text-start text-md-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">
                                                <i class="fa-solid fa-file-pdf"></i> Otvori PDF datoteke ({{ n.pdf_datoteke|length }})
                                            </button>
                                            <ul class="dropdown-menu dropdown-menu-dark shadow border border-danger border-opacity-25" style="background-color: #1a1e2b;">
                                                {% for pdf in n.pdf_datoteke %}
                                                    <li><a class="dropdown-item text-danger py-2" href="/preuzmi/{{ pdf.filename }}" target="_blank"><i class="fa-solid fa-download me-2"></i>{{ pdf.filename.split('_', 1)[-1] if '_' in pdf.filename else pdf.filename }}</a></li>
                                                {% endfor %}
                                            </ul>
                                        </div>
                                    {% endif %}
                                    
                                    {% if n.lxdf_datoteke|length == 1 %}
                                        <a href="/preuzmi/{{ n.lxdf_datoteke[0].filename }}" class="btn btn-sm btn-outline-info mob-full-btn mb-1" target="_blank"><i class="fa-solid fa-file-code"></i> Otvori {{ n.lxdf_datoteke[0].ext }}</a>
                                    {% elif n.lxdf_datoteke|length > 1 %}
                                        <div class="dropdown d-inline-block mob-full-btn mb-1" style="vertical-align: top;">
                                            <button class="btn btn-sm btn-outline-info dropdown-toggle w-100 text-start text-md-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">
                                                <i class="fa-solid fa-layer-group"></i> Otvori Strojne datoteke ({{ n.lxdf_datoteke|length }})
                                            </button>
                                            <ul class="dropdown-menu dropdown-menu-dark shadow border border-info border-opacity-25" style="background-color: #1a1e2b;">
                                                {% for lx in n.lxdf_datoteke %}
                                                    <li><a class="dropdown-item text-info py-2" href="/preuzmi/{{ lx.filename }}" target="_blank"><i class="fa-solid fa-download me-2"></i>{{ lx.filename.split('_', 1)[-1] if '_' in lx.filename else lx.filename }}</a></li>
                                                {% endfor %}
                                            </ul>
                                        </div>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if n.status == 'Na pregledu' %}
                                        <a href="/arhiviraj/{{ n.id }}" target="_blank" class="btn btn-success btn-sm w-100 mb-1 fw-bold mob-full-btn"><i class="fa-solid fa-folder-open me-1"></i> Spremi i Arhiviraj</a>
                                    {% endif %}
                                    <a href="/obrisi/{{ n.id }}" class="btn btn-outline-danger btn-sm w-100 mob-full-btn" title="Trajno obriši"><i class="fa-solid fa-trash"></i> Obriši</a>
                                </td>
                            </tr>
                            
                            {% if n.pozicije %}
                            <tr class="collapse" id="detalji-{{ n.id }}">
                                <td colspan="6" class="p-3" style="background-color: #171b26;">
                                    <div class="tamni-kontejner p-0 border-0 shadow-lg rounded-3 mt-1 overflow-hidden">
                                        <h6 class="fw-bold text-info bg-dark bg-opacity-50 p-3 mb-0 border-bottom border-secondary border-opacity-25">
                                            <i class="fa-solid fa-chart-simple me-2"></i>Specifikacija odrađenih pozicija
                                        </h6>
                                        <table class="table table-hover table-borderless text-white mb-0" style="background-color: #1a1e2b;">
                                            <thead class="bg-dark bg-opacity-75 text-secondary" style="font-size: 0.8rem;">
                                                <tr>
                                                    <th class="ps-4 py-3">NAZIV POZICIJE</th>
                                                    <th class="text-center py-3 border-start border-secondary border-opacity-25"><i class="fa-solid fa-fire text-danger me-1"></i> REZANJE</th>
                                                    <th class="text-center py-3 border-start border-secondary border-opacity-25"><i class="fa-solid fa-hammer text-warning me-1"></i> BRAVARIJA</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {% for p in n.pozicije %}
                                                <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                                                    <td class="ps-4 py-3 align-middle fw-bold text-light fs-6">{{ p.naziv_pozicije }}</td>
                                                    <td class="text-center py-3 align-middle border-start border-secondary border-opacity-25">
                                                        <span class="badge bg-info text-white shadow-sm me-1 px-2 py-1" style="font-size: 0.85rem;">Potrebno: {{ p.ciljana_kolicina }} kom</span><br>
                                                        <span class="badge bg-success text-white shadow-sm mt-2 me-1 px-2 py-1" style="font-size: 0.85rem;">Odrađeno: {{ p.laser_komada }} kom</span>
                                                        <span class="badge bg-danger text-white shadow-sm mt-2 me-1 px-2 py-1" style="font-size: 0.85rem;">{{ p.laser_skart }} škart</span><br>
                                                        <small class="text-muted d-block mt-2">
                                                            {% if p.laser_priprema_sati or p.laser_priprema_minute or p.laser_rezanje_sati or p.laser_rezanje_minute %}
                                                                <i class="fa-regular fa-clock text-info me-1"></i> Prip: {{ p.laser_priprema_sati }}h {{ p.laser_priprema_minute }}m &nbsp;|&nbsp; Rez: {{ p.laser_rezanje_sati }}h {{ p.laser_rezanje_minute }}m 
                                                            {% else %}
                                                                <i class="fa-regular fa-clock text-info me-1"></i> {{ p.laser_sati }}h {{ p.laser_minute }}m 
                                                            {% endif %}
                                                            &nbsp;|&nbsp; <i class="fa-solid fa-user text-info me-1"></i> {{ p.laser_radnik or '-' }}
                                                        </small>
                                                    </td>
                                                    <td class="text-center py-3 align-middle border-start border-secondary border-opacity-25">
                                                        <span class="badge bg-info text-white shadow-sm me-1 px-2 py-1" style="font-size: 0.85rem;">Potrebno: {{ p.ciljana_kolicina }} kom</span><br>
                                                        <span class="badge bg-success text-white shadow-sm mt-2 me-1 px-2 py-1" style="font-size: 0.85rem;">Odrađeno: {{ p.bravarija_komada }} kom</span>
                                                        <span class="badge bg-danger text-white shadow-sm mt-2 me-1 px-2 py-1" style="font-size: 0.85rem;">{{ p.bravarija_skart }} škart</span><br>
                                                        <small class="text-muted d-block mt-2">
                                                            {% if p.bravarija_priprema_sati or p.bravarija_priprema_minute or p.bravarija_piganje_sati or p.bravarija_piganje_minute %}
                                                                <i class="fa-regular fa-clock text-warning me-1"></i> Prip: {{ p.bravarija_priprema_sati }}h {{ p.bravarija_priprema_minute }}m &nbsp;|&nbsp; Pig: {{ p.bravarija_piganje_sati }}h {{ p.bravarija_piganje_minute }}m 
                                                            {% else %}
                                                                <i class="fa-regular fa-clock text-warning me-1"></i> {{ p.bravarija_sati }}h {{ p.bravarija_minute }}m 
                                                            {% endif %}
                                                            &nbsp;|&nbsp; <i class="fa-solid fa-user text-warning me-1"></i> {{ p.bravarija_radnik or '-' }}
                                                        </small>
                                                    </td>
                                                </tr>
                                                {% endfor %}
                                            </tbody>
                                        </table>
                                    </div>
                                </td>
                            </tr>
                            {% endif %}
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    """
    return render_template_string(f"<!DOCTYPE html><html>{STIL_I_NAVIGACIJA}{BODY_OPEN_TAG}{NAVBAR_TEMPLATE}{glavni_sadrzaj}</body></html>", nalozi=nalozi)


@app.route('/arhiviraj/<int:id>')
def arhiviraj_nalog(id):
    if 'role' not in session or session['role'] != 'Admin': return redirect(url_for('login'))
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
                if os.path.exists(fpath):
                    zf.write(fpath, pdf['filename'])
                
            lxdf_datoteke = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            for lx in lxdf_datoteke:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], lx['filename'])
                if os.path.exists(fpath):
                    zf.write(fpath, lx['filename'])
                
            logo_base64 = ""
            logo_ext = ""
            for ext in ['png', 'jpg', 'jpeg']:
                path = os.path.join(BASE_DIR, f'html_logo.{ext}')
                if os.path.exists(path):
                    with open(path, "rb") as lf:
                        logo_base64 = base64.b64encode(lf.read()).decode('utf-8')
                        logo_ext = "jpeg" if ext == "jpg" else ext
                    break
                    
            report_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Proizvodni Izvještaj - Nalog #{id}</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; color: #1a202c; margin: 0; padding: 30px; }}
        .wrapper {{ max-width: 900px; background: #ffffff; margin: 0 auto; padding: 40px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.05); border-top: 8px solid #dc3545; }}
        .logo-container {{ background-color: #ffffff; padding: 10px; border-radius: 8px; display: inline-flex; align-items: center; }}
        .logo-img {{ filter: brightness(1.35) saturate(1.25) contrast(1.1); max-height: 55px; width: auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; margin-bottom: 25px; }}
        .meta-title {{ font-size: 14px; text-transform: uppercase; color: #64748b; text-align: right; font-weight: bold; letter-spacing: 1px; line-height: 1.4; }}
        .info-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 30px; }}
        .info-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-left: 4px solid #0dcaf0; padding: 14px 18px; border-radius: 8px; }}
        .info-card h3 {{ margin: 0 0 4px 0; font-size: 11px; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px; }}
        .info-card p {{ margin: 0; font-size: 15px; font-weight: 600; color: #0f172a; }}
        .napomena-kontejner {{ margin-bottom: 30px; }}
        .napomena-card {{ padding: 15px 18px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid #dc3545; background: #fffafb; border: 1px solid #fecdd3; border-right: 1px solid #fecdd3; border-bottom: 1px solid #fecdd3; }}
        .napomena-card.bravarija {{ border-left-color: #ea580c; background: #fffdfa; border-color: #ffedd5; }}
        .napomena-card.poslovođa {{ border-left-color: #0dcaf0; background: #f0fdfa; border-color: #ccfbf1; }}
        .napomena-card h4 {{ margin: 0 0 6px 0; font-size: 12px; text-transform: uppercase; color: #475569; }}
        .napomena-card p {{ margin: 0; font-size: 14px; font-weight: 500; color: #1e293b; white-space: pre-wrap; }}
        .table-section {{ margin-top: 30px; }}
        .table-section h2 {{ font-size: 18px; font-weight: 700; color: #0f172a; margin-bottom: 15px; padding-bottom: 6px; border-bottom: 2px solid #e2e8f0; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
        th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 12px 14px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1; }}
        td {{ padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; vertical-align: middle; color: #334155; }}
        tr:nth-child(even) td {{ background: #f8fafc; }}
        .badge {{ display: inline-block; padding: 4px 8px; font-size: 11px; font-weight: 700; border-radius: 4px; text-transform: uppercase; margin-right: 4px; }}
        .badge-info {{ background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }}
        .badge-success {{ background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }}
        .badge-danger {{ background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }}
        .radnik-info {{ font-size: 12px; color: #64748b; margin-top: 4px; line-height: 1.4; }}
        .footer {{ text-align: center; margin-top: 40px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 15px; }}
        @media print {{
            @page {{ margin: 1cm; size: A4 portrait; }}
            body {{ background-color: #ffffff !important; padding: 0; color: #000; }}
            .wrapper {{ box-shadow: none; padding: 0; border-top: none; max-width: 100%; }}
            .btn, .navbar {{ display: none !important; }}
            * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
        }}
    </style>
</head>
<body>
    <div class="wrapper">
        <div class="header">
            <div class="logo-container">
                {"<img src='data:image/" + logo_ext + ";base64," + logo_base64 + "' class='logo-img' alt='FirstCutLaser'>" if logo_base64 else "<h1 style='color:#dc3545; margin:0;'>FIRST CUT LASER</h1>"}
            </div>
            <div class="meta-title">Arhivski Izvještaj Pogona<br><span style="color:#0f172a; font-size:16px;">Nalog #{id}</span></div>
        </div>
        <div class="info-grid">
            <div class="info-card"><h3>Kupac / Partner</h3><p>{n['naziv_naloga'] if n['naziv_naloga'] else '-'}</p></div>
            <div class="info-card"><h3>Naziv Projekta</h3><p>{n['naziv_projekta'] if n['naziv_projekta'] else '-'}</p></div>
            <div class="info-card"><h3>Kreator Naloga</h3><p>{n['kreirao'] if n['kreirao'] else '-'}</p></div>
            <div class="info-card"><h3>Proizvodna Ruta</h3><p>{n['rutiranje'] if n['rutiranje'] else '-'}</p></div>
            <div class="info-card"><h3>Debljina Materijala</h3><p>{n['debljina_ploce'] if n['debljina_ploce'] else '-'}</p></div>
            <div class="info-card"><h3>Dimenzije uložene ploče</h3><p>{n['dimenzije_ploce_laser'] if n['dimenzije_ploce_laser'] else '-'}</p></div>
            <div class="info-card"><h3>Materijal ploče</h3><p>{n['materijal_ploce_laser'] if n['materijal_ploce_laser'] else '-'}</p></div>
        </div>
        <div class="napomena-kontejner">
            {"<div class='napomena-card poslovođa'><h4><i class='fa-solid fa-user-tie'></i> Upute Poslovođe</h4><p>" + n['opis'] + "</p></div>" if n['opis'] else ""}
            {"<div class='napomena-card'><h4><i class='fa-solid fa-fire'></i> Napomena s Lasera</h4><p>" + n['laser_napomena'] + "</p></div>" if n['laser_napomena'] else ""}
            {"<div class='napomena-card bravarija'><h4><i class='fa-solid fa-hammer'></i> Napomena iz Bravarije</h4><p>" + n['bravarija_napomena'] + "</p></div>" if n['bravarija_napomena'] else ""}
        </div>
        <div class="table-section">
            <h2>Specifikacija Izvršenih Pozicija</h2>
            <table>
                <thead>
                    <tr><th>Naziv Pozicije s Nacrta</th><th>Faza 1: Rezanje (Laser)</th><th>Faza 2: Bravarska Obrada</th></tr>
                </thead>
                <tbody>
"""
            for p in pozicije:
                if p['laser_priprema_sati'] or p['laser_priprema_minute'] or p['laser_rezanje_sati'] or p['laser_rezanje_minute']:
                    vrijeme_laser = f"Priprema: {p['laser_priprema_sati']}h {p['laser_priprema_minute']}m<br>Rezanje: {p['laser_rezanje_sati']}h {p['laser_rezanje_minute']}m"
                else:
                    vrijeme_laser = f"Vrijeme: {p['laser_sati']}h {p['laser_minute']}m"
                
                if p['bravarija_priprema_sati'] or p['bravarija_priprema_minute'] or p['bravarija_piganje_sati'] or p['bravarija_piganje_minute']:
                    vrijeme_bravarija = f"Priprema: {p['bravarija_priprema_sati']}h {p['bravarija_priprema_minute']}m<br>Piganje: {p['bravarija_piganje_sati']}h {p['bravarija_piganje_minute']}m"
                else:
                    vrijeme_bravarija = f"Vrijeme: {p['bravarija_sati']}h {p['bravarija_minute']}m"
                    
                report_html += f"""
                    <tr>
                        <td style="font-weight: 700; color: #0f172a; font-size: 15px;">{p['naziv_pozicije']}</td>
                        <td>
                            <div><span class="badge badge-info">POTREBNO: {p['ciljana_kolicina']} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {p['laser_komada']} KOM</span><span class="badge badge-danger">{p['laser_skart']} ŠKART</span></div>
                            <div class="radnik-info">{vrijeme_laser}<br>Radnik: {p['laser_radnik'] if p['laser_radnik'] else '-'}</div>
                        </td>
                        <td>
                            <div><span class="badge badge-info">POTREBNO: {p['ciljana_kolicina']} KOM</span><br><span class="badge badge-success" style="margin-top:4px;">ODRAĐENO: {p['bravarija_komada']} KOM</span><span class="badge badge-danger">{p['bravarija_skart']} ŠKART</span></div>
                            <div class="radnik-info">{vrijeme_bravarija}<br>Radnik: {p['bravarija_radnik'] if p['bravarija_radnik'] else '-'}</div>
                        </td>
                    </tr>
                """
            report_html += f"""
                </tbody>
            </table>
        </div>
        <div class="footer">Sustav First Cut Laser d.o.o. &bull; Izvještaj generiran: {datetime.now().strftime('%d.%m.%Y. u %H:%M')}</div>
    </div>
</body>
</html>
"""
            zf.writestr(f"Pregled_Naloga_{id}.html", report_html.encode('utf-8'))
        
        conn.execute("UPDATE radni_nalozi SET status='Arhivirano' WHERE id=?", (id,))
        
        preostalo = conn.execute("SELECT COUNT(*) FROM radni_nalozi WHERE status != 'Arhivirano'").fetchone()[0]
        if preostalo == 0:
            ostaci = conn.execute("SELECT pdf_datoteka, lxdf_datoteka FROM radni_nalozi").fetchall()
            for o in ostaci:
                pdf_ostaci = parsiraj_listu_datoteka(o['pdf_datoteka'])
                for pdf in pdf_ostaci:
                    fpath = os.path.join(app.config['UPLOAD_FOLDER'], pdf['filename'])
                    if os.path.exists(fpath):
                        os.remove(fpath)
                lx_ostaci = parsiraj_listu_datoteka(o['lxdf_datoteka'])
                for lx in lx_ostaci:
                    fpath = os.path.join(app.config['UPLOAD_FOLDER'], lx['filename'])
                    if os.path.exists(fpath):
                        os.remove(fpath)
            
            conn.execute("DELETE FROM radni_nalozi")
            conn.execute("DELETE FROM nalog_pozicije")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='radni_nalozi'")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='nalog_pozicije'")
        
        conn.commit()
    except Exception as e:
        print("Greška pri kreiranju ZIP-a:", e)
    
    conn.close()
    
    memory_file.seek(0)
    return send_file(memory_file, download_name=predlozeno_ime_zipa, as_attachment=True)


@app.route('/postavke', methods=['GET', 'POST'])
def postavke():
    if 'role' not in session or session['role'] != 'Admin': return redirect(url_for('login'))
    autostart_on = False

    sadrzaj = """
    <div class="container-fluid glavni-prostor d-flex justify-content-center">
        <div class="card p-5" style="max-width: 600px; width:100%;">
            <h4 class="text-white fw-bold mb-4"><i class="fa-solid fa-gear text-info me-2"></i> Postavke Servera</h4>
            <hr class="border-secondary mb-4">
            <div class="d-flex justify-content-between align-items-center mb-4 flex-mob-col">
                <div>
                    <h6 class="text-white mb-1">Server Operacije</h6>
                    <small class="text-muted">Glavne postavke aplikacije su upravljane kroz konzolu servera.</small>
                </div>
            </div>
            <a href="/sefo_panel" class="btn btn-outline-info w-100">&larr; Nazad na Upravljačku Ploču</a>
        </div>
    </div>
    """
    return render_template_string(f"<!DOCTYPE html><html>{STIL_I_NAVIGACIJA}{BODY_OPEN_TAG}{NAVBAR_TEMPLATE}{sadrzaj}</body></html>", autostart_on=autostart_on)

@app.route('/preuzmi/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=False)

@app.route('/obrisi/<int:id>')
def obrisi_nalog(id):
    if 'role' not in session or session['role'] != 'Admin': return redirect(url_for('login'))
    conn = get_db_connection()
    n = conn.execute('SELECT pdf_datoteka, lxdf_datoteka FROM radni_nalozi WHERE id=?', (id,)).fetchone()
    if n:
        pdf_ostaci = parsiraj_listu_datoteka(n['pdf_datoteka'])
        for pdf in pdf_ostaci:
            fpath = os.path.join(UPLOAD_FOLDER, pdf['filename'])
            if os.path.exists(fpath):
                os.remove(fpath)
        lx_ostaci = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        for lx in lx_ostaci:
            fpath = os.path.join(UPLOAD_FOLDER, lx['filename'])
            if os.path.exists(fpath):
                os.remove(fpath)
    
    conn.execute('DELETE FROM radni_nalozi WHERE id=?', (id,))
    conn.execute('DELETE FROM nalog_pozicije WHERE nalog_id=?', (id,))
    
    preostalo = conn.execute("SELECT COUNT(*) FROM radni_nalozi WHERE status != 'Arhivirano'").fetchone()[0]
    if preostalo == 0:
        ostaci = conn.execute("SELECT pdf_datoteka, lxdf_datoteka FROM radni_nalozi").fetchall()
        for o in ostaci:
            pdf_ostaci = parsiraj_listu_datoteka(o['pdf_datoteka'])
            for pdf in pdf_ostaci:
                fpath = os.path.join(UPLOAD_FOLDER, pdf['filename'])
                if os.path.exists(fpath):
                    os.remove(fpath)
            lx_ostaci = parsiraj_listu_datoteka(o['lxdf_datoteka'])
            for lx in lx_ostaci:
                fpath = os.path.join(UPLOAD_FOLDER, lx['filename'])
                if os.path.exists(fpath):
                    os.remove(fpath)
        
        conn.execute("DELETE FROM radni_nalozi")
        conn.execute("DELETE FROM nalog_pozicije")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='radni_nalozi'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='nalog_pozicije'")
        
    conn.commit()
    conn.close()
    return redirect(url_for('index_master'))

@app.route('/zapocni_fazu/<int:id>/<string:faza>')
def zapocni_fazu(id, faza):
    trenutni_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = get_db_connection()
    if faza == 'laser': 
        stanica = request.cookies.get('stanica_lokalno', 'Laser')
        ime_lasera = 'Laser 1' if stanica == 'laser1' else 'Laser 2' if stanica == 'laser2' else 'Nepoznat Laser'
        conn.execute('''
            UPDATE radni_nalozi 
            SET laser_zapoceto_u=?, odabrani_laser=? 
            WHERE id=? AND (laser_zapoceto_u IS NULL OR laser_zapoceto_u = '')
        ''', (trenutni_iso, ime_lasera, id))
    elif faza == 'bravarija': 
        conn.execute('''
            UPDATE radni_nalozi 
            SET bravarija_zapoceto_u=? 
            WHERE id=? AND (bravarija_zapoceto_u IS NULL OR bravarija_zapoceto_u = '')
        ''', (trenutni_iso, id))
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
        
        pozicije_ids = request.form.getlist('pozicija_id')
        for pid in pozicije_ids:
            naziv_poz = request.form.get(f'naziv_{pid}')
            komada = to_int(request.form.get(f'komada_{pid}'))
            skart = to_int(request.form.get(f'skart_{pid}'))
            p_sati = to_int(request.form.get(f'priprema_sati_{pid}'))
            p_min = to_int(request.form.get(f'priprema_minute_{pid}'))
            r_sati = to_int(request.form.get(f'rezanje_sati_{pid}'))
            r_min = to_int(request.form.get(f'rezanje_minute_{pid}'))
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=?, laser_komada=?, laser_skart=?, laser_priprema_sati=?, laser_priprema_minute=?, laser_rezanje_sati=?, laser_rezanje_minute=?, laser_radnik=? WHERE id=?', 
                         (naziv_poz, komada, skart, p_sati, p_min, r_sati, r_min, radnik, pid))
        
        novi_status = 'Piganje' if akcija == 'bravarija' else 'Na pregledu'
        conn.execute("UPDATE radni_nalozi SET status=?, laser_napomena=?, dimenzije_ploce_laser=?, materijal_ploce_laser=? WHERE id=?", (novi_status, radnik_napomena, dimenzije_ploce, materijal_ploce, id_naloga))
        conn.commit()
        return redirect(url_for('sekcija_laser'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status='Laser'").fetchall()
    nalozi = []
    z_stanica = request.cookies.get('zakljucana_stanica', 'uprava')
    
    for r in nalozi_rows:
        n = dict(r)
        
        prikazi = True
        if n['laser_zapoceto_u']:
            if z_stanica == 'laser1' and n['odabrani_laser'] != 'Laser 1':
                prikazi = False
            elif z_stanica == 'laser2' and n['odabrani_laser'] != 'Laser 2':
                prikazi = False
                
        if prikazi:
            n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
            n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
            n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
            nalozi.append(n)
            
    conn.close()
    
    glavni_sadrzaj = """
    <div class="container-fluid glavni-prostor">
        <h3 class="mb-4 fw-bold text-white"><i class="fa-solid fa-fire text-danger me-2"></i>STANICA 1: REZANJE (LASER)</h3>
        {% if not nalozi %}<div class="alert alert-dark text-center my-5 py-5 border-0" style="background: #12141c; color: #94a3b8;">Nema otvorenih naloga na čekanju za rezanje.</div>{% endif %}
        
        {% for n in nalozi %}
        <div class="card p-4" style="border-top: 4px solid #ff0000 !important;">
            <div class="d-flex justify-content-between align-items-center mb-2 flex-mob-col">
                <div>
                    <h4 class="fw-bold text-white mb-0">{{ n.naziv_naloga }}</h4>
                    <small class="text-muted">
                        Projekt: {{ n.naziv_projekta }} 
                        {% if n.debljina_ploce %} | Debljina: <b class="text-info">D: {{ n.debljina_ploce }}</b>{% endif %} 
                        | Nalog #{{ n.id }} | Ruta: {{ 'Samo Rezanje' if n.rutiranje == 'Samo Laser' else n.rutiranje }}
                    </small>
                </div>
                <div class="text-end">
                    {% if not n.laser_zapoceto_u %}
                        <a href="/zapocni_fazu/{{ n.id }}/laser" class="btn btn-success fw-bold px-4">ZAPOČNI REZANJE</a>
                    {% else %}
                        {% if n.odabrani_laser %}
                            <span class="badge bg-danger text-white border border-danger mb-1"><i class="fa-solid fa-crosshairs me-1"></i>{{ n.odabrani_laser|upper }}</span><br>
                        {% endif %}
                        <span class="badge bg-dark border border-danger text-danger p-2 fs-6"><i class="fa-solid fa-stopwatch pulse-live me-2"></i><span class="timer-pogona" data-start="{{ n.laser_zapoceto_u }}">0m 0s</span></span>
                    {% endif %}
                </div>
            </div>
            
            {% if n.opis %}
                {% set show_collapse = n.opis|length > 100 %}
                {% if show_collapse %}
                    <button class="btn btn-sm btn-outline-info text-start w-100 my-2" type="button" data-bs-toggle="collapse" data-bs-target="#upute-{{ n.id }}">
                        <i class="fa-solid fa-circle-info me-2"></i> Prikaži upute poslovođe
                    </button>
                    <div class="collapse" id="upute-{{ n.id }}">
                        <div class="napomena-box border-info"><b><i class="fa-solid fa-user-tie text-info me-1"></i> <span class="text-info">Upute poslovođe:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.opis }}</span></div>
                    </div>
                {% else %}
                    <div class="napomena-box border-info my-2"><b><i class="fa-solid fa-user-tie text-info me-1"></i> <span class="text-info">Upute poslovođe:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.opis }}</span></div>
                {% endif %}
            {% endif %}
            
            <div class="mt-2 mb-4">
                {% if n.pdf_datoteke|length == 1 %}
                    <a href="/preuzmi/{{ n.pdf_datoteke[0].filename }}" class="btn btn-sm btn-outline-danger mob-full-btn mb-1" target="_blank"><i class="fa-solid fa-file-pdf"></i> Otvori PDF</a>
                {% elif n.pdf_datoteke|length > 1 %}
                    <div class="dropdown d-inline-block mob-full-btn mb-1" style="vertical-align: top;">
                        <button class="btn btn-sm btn-outline-danger dropdown-toggle w-100 text-start text-md-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">
                            <i class="fa-solid fa-file-pdf"></i> Otvori PDF datoteke ({{ n.pdf_datoteke|length }})
                        </button>
                        <ul class="dropdown-menu dropdown-menu-dark shadow border border-danger border-opacity-25" style="background-color: #1a1e2b;">
                            {% for pdf in n.pdf_datoteke %}
                                <li><a class="dropdown-item text-danger py-2" href="/preuzmi/{{ pdf.filename }}" target="_blank"><i class="fa-solid fa-download me-2"></i>{{ pdf.filename.split('_', 1)[-1] if '_' in pdf.filename else pdf.filename }}</a></li>
                            {% endfor %}
                        </ul>
                    </div>
                {% endif %}
                
                {% if n.lxdf_datoteke|length == 1 %}
                    <a href="/preuzmi/{{ n.lxdf_datoteke[0].filename }}" class="btn btn-sm btn-outline-info mob-full-btn mb-1" target="_blank"><i class="fa-solid fa-file-code"></i> Otvori {{ n.lxdf_datoteke[0].ext }}</a>
                {% elif n.lxdf_datoteke|length > 1 %}
                    <div class="dropdown d-inline-block mob-full-btn mb-1" style="vertical-align: top;">
                        <button class="btn btn-sm btn-outline-info dropdown-toggle w-100 text-start text-md-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">
                            <i class="fa-solid fa-layer-group"></i> Otvori Strojne datoteke ({{ n.lxdf_datoteke|length }})
                        </button>
                        <ul class="dropdown-menu dropdown-menu-dark shadow border border-info border-opacity-25" style="background-color: #1a1e2b;">
                            {% for lx in n.lxdf_datoteke %}
                                <li><a class="dropdown-item text-info py-2" href="/preuzmi/{{ lx.filename }}" target="_blank"><i class="fa-solid fa-download me-2"></i>{{ lx.filename.split('_', 1)[-1] if '_' in lx.filename else lx.filename }}</a></li>
                            {% endfor %}
                        </ul>
                    </div>
                {% endif %}
            </div>

            {% if n.laser_zapoceto_u %}
            <div class="p-3 mb-3 dodaj-kontejner">
                <form action="/dodaj_poziciju/{{ n.id }}/laser" method="POST" class="row g-2 align-items-center">
                    <div class="col-md-6"><input type="text" class="form-control form-control-sm" name="novi_naziv_pozicije" placeholder="Upišite šifru pozicije s nacrta..." required onkeydown="if(event.key === 'Enter') event.preventDefault();"></div>
                    <div class="col-md-3"><button type="submit" class="btn btn-sm btn-outline-danger w-100"><i class="fa-solid fa-plus"></i> Dodaj poziciju</button></div>
                </form>
            </div>
            
            <form method="POST" class="nav-forma">
                <input type="hidden" name="id_naloga" value="{{ n.id }}">
                
                <div class="p-3 mb-4 rounded-3 border border-info border-opacity-25" style="background-color: rgba(13, 202, 240, 0.03);">
                    <div class="row align-items-center g-3">
                        <div class="col-md-6">
                            <label class="form-label text-info fw-bold mb-1"><i class="fa-solid fa-ruler-combined me-1"></i> Dimenzije uložene ploče</label>
                            <input type="text" class="form-control text-white border-info bg-dark navigabilno" name="dimenzije_ploce" placeholder="npr. 2000x1000x5" value="{{ n.dimenzije_ploce_laser }}">
                        </div>
                        <div class="col-md-6">
                            <label class="form-label text-info fw-bold mb-1"><i class="fa-solid fa-layer-group me-1"></i> Materijal ploče</label>
                            <input type="text" class="form-control text-white border-info bg-dark navigabilno" name="materijal_ploce" placeholder="npr. Inox, Alumunij, Čelik..." value="{{ n.materijal_ploce_laser }}">
                        </div>
                    </div>
                </div>
                
                <div class="table-responsive mb-3 p-2 tamni-kontejner">
                    <table class="table align-middle">
                        <thead><tr><th>Naziv Pozicije</th><th style="width:100px;">Potrebno</th><th style="width:120px;">Odrađeno</th><th style="width:120px;">Škart</th><th style="width:140px;">Priprema (h:m)</th><th style="width:140px;">Rezanje (h:m)</th><th class="text-end">X</th></tr></thead>
                        <tbody>
                            {% for p in n.pozicije %}
                            <tr>
                                <td><input type="hidden" name="pozicija_id" value="{{ p.id }}"><input type="text" class="form-control text-danger fw-bold navigabilno" name="naziv_{{ p.id }}" value="{{ p.naziv_pozicije }}" required></td>
                                <td><div class="fs-6 fw-bold text-info">{{ p.ciljana_kolicina }} kom</div></td>
                                <td><input type="text" class="form-control text-white navigabilno" name="komada_{{ p.id }}" value="{{ p.laser_komada if p.laser_komada else '' }}" placeholder="0" required oninput="this.value=this.value.replace(/[^0-9]/g,'');"></td>
                                <td><input type="text" class="form-control text-danger navigabilno" name="skart_{{ p.id }}" value="{{ p.laser_skart if p.laser_skart else '' }}" placeholder="0" required oninput="this.value=this.value.replace(/[^0-9]/g,'');"></td>
                                
                                <td>
                                    <div class="d-flex align-items-center">
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="priprema_sati_{{ p.id }}" value="{{ p.laser_priprema_sati if p.laser_priprema_sati else '' }}" placeholder="h" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                        <span class="mx-1 text-muted">:</span>
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="priprema_minute_{{ p.id }}" value="{{ p.laser_priprema_minute if p.laser_priprema_minute else '' }}" placeholder="m" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                    </div>
                                </td>
                                <td>
                                    <div class="d-flex align-items-center">
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="rezanje_sati_{{ p.id }}" value="{{ p.laser_rezanje_sati if p.laser_rezanje_sati else '' }}" placeholder="h" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                        <span class="mx-1 text-muted">:</span>
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="rezanje_minute_{{ p.id }}" value="{{ p.laser_rezanje_minute if p.laser_rezanje_minute else '' }}" placeholder="m" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                    </div>
                                </td>
                                <td class="text-end"><a href="/obrisi_poziciju/{{ p.id }}/laser" class="btn btn-sm btn-outline-danger"><i class="fa-solid fa-xmark"></i></a></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                
                <div class="row align-items-start g-2 mt-4 pt-3 border-top border-secondary border-opacity-25">
                    <div class="col-md-5">
                        <label class="form-label text-white">Ime Operatera Lasera</label>
                        <input type="text" class="form-control text-white navigabilno" name="radnik" placeholder="Unesite ime..." required oninput="this.value=this.value.replace(/[0-9]/g,'');">
                        <label class="form-label mt-3 text-white">Napomena Radnika (Opcionalno)</label>
                        <textarea class="form-control text-white auto-expand navigabilno" name="radnik_napomena" placeholder="Npr. Ostavio sam u kutu kod vrata..." oninput="autoProsiri(this)"></textarea>
                    </div>
                    <div class="col-md-7 ms-auto text-end d-flex gap-2 justify-content-end align-items-end h-100 mob-col-btn" style="padding-top: 30px;">
                        <button type="submit" name="akcija" value="zavrsi_odmah" class="btn btn-success fw-bold px-4">&check; Završi i pošalji na Pregled</button>
                        {% if n.rutiranje != 'Samo Laser' and n.rutiranje != 'Samo Rezanje' %}
                            <button type="submit" name="akcija" value="bravarija" class="btn btn-danger fw-bold px-4">Pošalji u Bravariju &rarr;</button>
                        {% endif %}
                    </div>
                </div>
            </form>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    """
    return render_template_string(f"<!DOCTYPE html><html>{STIL_I_NAVIGACIJA}{BODY_OPEN_TAG}{NAVBAR_TEMPLATE}{glavni_sadrzaj}</body></html>", nalozi=nalozi)

@app.route('/bravarija', methods=['GET', 'POST'])
def sekcija_bravarija():
    conn = get_db_connection()
    if request.method == 'POST':
        id_naloga = request.form['id_naloga']
        radnik = request.form.get('radnik', '')
        radnik_napomena = request.form.get('radnik_napomena', '')
        pozicije_ids = request.form.getlist('pozicija_id')
        for pid in pozicije_ids:
            naziv_poz = request.form.get(f'naziv_{pid}')
            komada = to_int(request.form.get(f'komada_{pid}'))
            skart = to_int(request.form.get(f'skart_{pid}'))
            p_sati = to_int(request.form.get(f'priprema_sati_{pid}'))
            p_min = to_int(request.form.get(f'priprema_minute_{pid}'))
            pig_sati = to_int(request.form.get(f'piganje_sati_{pid}'))
            pig_min = to_int(request.form.get(f'piganje_minute_{pid}'))
            conn.execute('UPDATE nalog_pozicije SET naziv_pozicije=?, bravarija_komada=?, bravarija_skart=?, bravarija_priprema_sati=?, bravarija_priprema_minute=?, bravarija_piganje_sati=?, bravarija_piganje_minute=?, bravarija_radnik=? WHERE id=?', 
                         (naziv_poz, komada, skart, p_sati, p_min, pig_sati, pig_min, radnik, pid))
        
        conn.execute("UPDATE radni_nalozi SET status='Na pregledu', bravarija_napomena=? WHERE id=?", (radnik_napomena, id_naloga))
        conn.commit()
        return redirect(url_for('sekcija_bravarija'))
        
    nalozi_rows = conn.execute("SELECT * FROM radni_nalozi WHERE status='Piganje'").fetchall()
    nalozi = []
    for r in nalozi_rows:
        n = dict(r)
        n['pozicije'] = [dict(p) for p in conn.execute('SELECT * FROM nalog_pozicije WHERE nalog_id=?', (n['id'],)).fetchall()]
        n['lxdf_datoteke'] = parsiraj_listu_datoteka(n['lxdf_datoteka'])
        n['pdf_datoteke'] = parsiraj_listu_datoteka(n['pdf_datoteka'])
        nalozi.append(n)
    conn.close()
    
    glavni_sadrzaj = """
    <div class="container-fluid glavni-prostor">
        <h3 class="mb-4 fw-bold text-white"><i class="fa-solid fa-hammer text-warning me-2"></i>STANICA 2: BRAVARIJA</h3>
        {% if not nalozi %}<div class="alert alert-dark text-center my-5 py-5 border-0" style="background: #12141c; color: #94a3b8;">Nema naloga na čekanju za bravariju.</div>{% endif %}
        
        {% for n in nalozi %}
        <div class="card p-4" style="border-top: 4px solid #facc15 !important;">
            <div class="d-flex justify-content-between align-items-center mb-2 flex-mob-col">
                <div>
                    <h4 class="fw-bold text-white mb-0">{{ n.naziv_naloga }}</h4>
                    <small class="text-muted">
                        Projekt: {{ n.naziv_projekta }} 
                        {% if n.debljina_ploce %} | Debljina: <b class="text-info">D: {{ n.debljina_ploce }}</b>{% endif %} 
                        | Nalog #{{ n.id }}
                    </small>
                </div>
                <div>
                    {% if not n.bravarija_zapoceto_u %}
                        <a href="/zapocni_fazu/{{ n.id }}/bravarija" class="btn btn-warning fw-bold px-4">ZAPOČNI BRAVARIJU</a>
                    {% else %}
                        <span class="badge bg-dark border border-warning text-warning p-2 fs-6"><i class="fa-solid fa-stopwatch pulse-live me-2"></i><span class="timer-pogona" data-start="{{ n.bravarija_zapoceto_u }}">0m 0s</span></span>
                    {% endif %}
                </div>
            </div>
            
            {% if n.opis or n.laser_napomena %}
                {% set ukupno_slova = (n.opis|length if n.opis else 0) + (n.laser_napomena|length if n.laser_napomena else 0) %}
                {% if ukupno_slova > 100 %}
                    <button class="btn btn-sm btn-outline-warning text-start w-100 my-2" type="button" data-bs-toggle="collapse" data-bs-target="#upute-{{ n.id }}">
                        <i class="fa-solid fa-book-open me-2"></i> Prikaži prijašnje upute i napomene
                    </button>
                    <div class="collapse mt-2" id="upute-{{ n.id }}">
                        {% if n.opis %}<div class="napomena-box border-info"><b><i class="fa-solid fa-user-tie text-info me-1"></i> <span class="text-info">Upute poslovođe:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.opis }}</span></div>{% endif %}
                        {% if n.laser_napomena %}<div class="napomena-box border-danger mt-2"><b><i class="fa-solid fa-fire text-danger me-1"></i> <span class="text-info">Napomena iz Lasera:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.laser_napomena }}</span></div>{% endif %}
                    </div>
                {% else %}
                    <div class="mt-2">
                        {% if n.opis %}<div class="napomena-box border-info"><b><i class="fa-solid fa-user-tie text-info me-1"></i> <span class="text-info">Upute poslovođe:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.opis }}</span></div>{% endif %}
                        {% if n.laser_napomena %}<div class="napomena-box border-danger mt-2"><b><i class="fa-solid fa-fire text-danger me-1"></i> <span class="text-info">Napomena iz Lasera:</span></b><br><span class="text-white fw-bold" style="font-size: 0.95rem;">{{ n.laser_napomena }}</span></div>{% endif %}
                    </div>
                {% endif %}
            {% endif %}
            
            <div class="mt-2 mb-4">
                {% if n.pdf_datoteke|length == 1 %}
                    <a href="/preuzmi/{{ n.pdf_datoteke[0].filename }}" class="btn btn-sm btn-outline-danger mob-full-btn mb-1" target="_blank"><i class="fa-solid fa-file-pdf"></i> Otvori PDF</a>
                {% elif n.pdf_datoteke|length > 1 %}
                    <div class="dropdown d-inline-block mob-full-btn mb-1" style="vertical-align: top;">
                        <button class="btn btn-sm btn-outline-danger dropdown-toggle w-100 text-start text-md-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">
                            <i class="fa-solid fa-file-pdf"></i> Otvori PDF datoteke ({{ n.pdf_datoteke|length }})
                        </button>
                        <ul class="dropdown-menu dropdown-menu-dark shadow border border-danger border-opacity-25" style="background-color: #1a1e2b;">
                            {% for pdf in n.pdf_datoteke %}
                                <li><a class="dropdown-item text-danger py-2" href="/preuzmi/{{ pdf.filename }}" target="_blank"><i class="fa-solid fa-download me-2"></i>{{ pdf.filename.split('_', 1)[-1] if '_' in pdf.filename else pdf.filename }}</a></li>
                            {% endfor %}
                        </ul>
                    </div>
                {% endif %}
                
                {% if n.lxdf_datoteke|length == 1 %}
                    <a href="/preuzmi/{{ n.lxdf_datoteke[0].filename }}" class="btn btn-sm btn-outline-info mob-full-btn mb-1" target="_blank"><i class="fa-solid fa-file-code"></i> Otvori {{ n.lxdf_datoteke[0].ext }}</a>
                {% elif n.lxdf_datoteke|length > 1 %}
                    <div class="dropdown d-inline-block mob-full-btn mb-1" style="vertical-align: top;">
                        <button class="btn btn-sm btn-outline-info dropdown-toggle w-100 text-start text-md-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">
                            <i class="fa-solid fa-layer-group"></i> Otvori Strojne datoteke ({{ n.lxdf_datoteke|length }})
                        </button>
                        <ul class="dropdown-menu dropdown-menu-dark shadow border border-info border-opacity-25" style="background-color: #1a1e2b;">
                            {% for lx in n.lxdf_datoteke %}
                                <li><a class="dropdown-item text-info py-2" href="/preuzmi/{{ lx.filename }}" target="_blank"><i class="fa-solid fa-download me-2"></i>{{ lx.filename.split('_', 1)[-1] if '_' in lx.filename else lx.filename }}</a></li>
                            {% endfor %}
                        </ul>
                    </div>
                {% endif %}
            </div>

            {% if n.bravarija_zapoceto_u %}
            <div class="p-3 mb-3 dodaj-kontejner">
                <form action="/dodaj_poziciju/{{ n.id }}/bravarija" method="POST" class="row g-2 align-items-center">
                    <div class="col-md-6"><input type="text" class="form-control form-control-sm" name="novi_naziv_pozicije" placeholder="Upišite šifru pozicije s nacrta..." required onkeydown="if(event.key === 'Enter') event.preventDefault();"></div>
                    <div class="col-md-3"><button type="submit" class="btn btn-sm btn-outline-warning text-white w-100"><i class="fa-solid fa-plus"></i> Dodaj poziciju</button></div>
                </form>
            </div>
            
            <form method="POST" class="nav-forma">
                <input type="hidden" name="id_naloga" value="{{ n.id }}">
                <div class="table-responsive mb-3 p-2 tamni-kontejner">
                    <table class="table align-middle">
                        <thead><tr><th>Naziv Pozicije</th><th style="width:100px;">Potrebno</th><th style="width:120px;">Odrađeno</th><th style="width:120px;">Škart</th><th style="width:140px;">Priprema (h:m)</th><th style="width:140px;">Piganje (h:m)</th><th class="text-end">X</th></tr></thead>
                        <tbody>
                            {% for p in n.pozicije %}
                            <tr>
                                <td><input type="hidden" name="pozicija_id" value="{{ p.id }}"><input type="text" class="form-control text-warning fw-bold navigabilno" name="naziv_{{ p.id }}" value="{{ p.naziv_pozicije }}" required></td>
                                <td><div class="fs-6 fw-bold text-info">{{ p.ciljana_kolicina }} kom</div></td>
                                <td><input type="text" class="form-control text-white navigabilno" name="komada_{{ p.id }}" value="{{ p.bravarija_komada if p.bravarija_komada else '' }}" placeholder="0" required oninput="this.value=this.value.replace(/[^0-9]/g,'');"></td>
                                <td><input type="text" class="form-control text-danger navigabilno" name="skart_{{ p.id }}" value="{{ p.bravarija_skart if p.bravarija_skart else '' }}" placeholder="0" required oninput="this.value=this.value.replace(/[^0-9]/g,'');"></td>
                                
                                <td>
                                    <div class="d-flex align-items-center">
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="priprema_sati_{{ p.id }}" value="{{ p.bravarija_priprema_sati if p.bravarija_priprema_sati else '' }}" placeholder="h" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                        <span class="mx-1 text-muted">:</span>
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="priprema_minute_{{ p.id }}" value="{{ p.bravarija_priprema_minute if p.bravarija_priprema_minute else '' }}" placeholder="m" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                    </div>
                                </td>
                                <td>
                                    <div class="d-flex align-items-center">
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="piganje_sati_{{ p.id }}" value="{{ p.bravarija_piganje_sati if p.bravarija_piganje_sati else '' }}" placeholder="h" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                        <span class="mx-1 text-muted">:</span>
                                        <input type="text" class="form-control text-white px-1 text-center navigabilno" name="piganje_minute_{{ p.id }}" value="{{ p.bravarija_piganje_minute if p.bravarija_piganje_minute else '' }}" placeholder="m" required oninput="this.value=this.value.replace(/[^0-9]/g,'');">
                                    </div>
                                </td>
                                
                                <td class="text-end"><a href="/obrisi_poziciju/{{ p.id }}/bravarija" class="btn btn-sm btn-outline-danger"><i class="fa-solid fa-xmark"></i></a></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                
                <div class="row align-items-start g-2 mt-4 pt-3 border-top border-secondary border-opacity-25">
                    <div class="col-md-5">
                        <label class="form-label text-white">Ime Bravara</label>
                        <input type="text" class="form-control text-white navigabilno" name="radnik" placeholder="Unesite ime..." required oninput="this.value=this.value.replace(/[0-9]/g,'');">
                        <label class="form-label mt-3 text-white">Napomena Bravara (Opcionalno)</label>
                        <textarea class="form-control text-white auto-expand navigabilno" name="radnik_napomena" placeholder="Npr. Obrađeno i stavljeno na paletu..." oninput="autoProsiri(this)"></textarea>
                    </div>
                    <div class="col-md-7 ms-auto text-end align-items-end d-flex justify-content-end h-100 mob-col-btn" style="padding-top: 30px;">
                        <button type="submit" class="btn btn-success fw-bold px-4">&check; Završi i pošalji na Pregled</button>
                    </div>
                </div>
            </form>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    """
    return render_template_string(f"<!DOCTYPE html><html>{STIL_I_NAVIGACIJA}{BODY_OPEN_TAG}{NAVBAR_TEMPLATE}{glavni_sadrzaj}</body></html>", nalozi=nalozi)

@app.route('/dodaj_poziciju/<int:nalog_id>/<string:izvor>', methods=['POST'])
def dodaj_poziciju(nalog_id, izvor):
    naziv = request.form.get('novi_naziv_pozicije', '').strip()
    if not naziv: naziv = f"Pozicija-{datetime.now().strftime('%H%M%S')}"
    conn = get_db_connection()
    conn.execute('INSERT INTO nalog_pozicije (nalog_id, naziv_pozicije, ciljana_kolicina) VALUES (?, ?, 0)', (nalog_id, naziv))
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
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=True)