"""!!! VAROVANI: UKAZKOVY SOUBOR S UMYSLNE CHYBOVYM KODEM !!!

Tento soubor existuje VYHRADNE jako fixture pro demonstraci multi-agent
code reviewera. Vsechny zranitelnosti uvnitr jsou ZAMERNE:

- Bezpecnostni dury: hardcoded heslo, SQL injection, slaby hash MD5,
  command injection pres shell=True
- Vykonnostni problemy: N+1 dotaz, opakovany I/O ve smycce
- Stylove prohresky: kratke nazvy, magic numbers, dlouha funkce, hluboke
  vnoreni

PRAVIDLA POUZITI:
1. NIKDY tento kod nespoustej.
2. NIKDY ho neimportuj do realne aplikace.
3. NIKDY z nej neopisuj vzory do produkce.
4. Soubor neni importovany z reviewer/ baliku - pouziva se POUZE jako vstup
   pro `python -m reviewer examples/vulnerable_login.py`.

Pokud jsi natrefil/-a na tento soubor v jinem kontextu nez jako vstupni
fixture pro reviewera, nahlas to.
"""
import hashlib
import sqlite3
import subprocess
import os


# Bad: hardcoded credential v repo
ADMIN_PASSWORD = "admin123"
DB_PATH = "/tmp/users.db"


def login(u, p):
    # Bad: SQL injection - parametry se vlepuji do SQL stringu
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    q = "SELECT id, password_hash FROM users WHERE username = '" + u + "'"
    cur.execute(q)
    row = cur.fetchone()
    if row is None:
        return None

    # Bad: MD5 pro hashovani hesla (slaby algoritmus)
    h = hashlib.md5(p.encode()).hexdigest()
    if h == row[1]:
        return row[0]
    return None


def get_orders_for_users(user_ids):
    """Vrati vsechny objednavky pro seznam uzivatelu."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    result = []
    # Bad: N+1 - pro kazdeho uzivatele samostatny SELECT
    for uid in user_ids:
        cur.execute("SELECT id, total FROM orders WHERE user_id = ?", (uid,))
        for row in cur.fetchall():
            # Bad: I/O ve smycce - open/close pro kazdy radek
            with open("/tmp/audit.log", "a") as f:
                f.write("order " + str(row[0]) + " for user " + str(uid) + "\n")
            result.append(row)
    return result


def run_backup(name):
    # Bad: command injection - `name` jde rovnou do shellu
    cmd = "tar czf /backups/" + name + ".tar.gz /var/data"
    subprocess.call(cmd, shell=True)


def process_data(items):
    # Bad: dlouha funkce delajici moc veci, magic numbers, hluboke vnoreni
    out = []
    for i in range(len(items)):
        x = items[i]
        if x is not None:
            if x.get("status") == 1:
                if x.get("amount", 0) > 1000:
                    if x.get("country") in ["CZ", "SK", "PL", "DE", "AT"]:
                        # 5 urovni vnoreni
                        d = x.get("amount") * 0.21
                        t = x.get("amount") + d
                        out.append({
                            "id": x.get("id"),
                            "tax": d,
                            "total": t,
                        })
                    elif x.get("country") == "US":
                        d = x.get("amount") * 0.07
                        t = x.get("amount") + d
                        out.append({
                            "id": x.get("id"),
                            "tax": d,
                            "total": t,
                        })
    return out


def render_user_profile(user_id):
    # Bad: SQL injection (znovu) + pravdepodobny XSS pokud vystup poleti do HTML
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name, bio FROM users WHERE id = " + str(user_id))
    name, bio = cur.fetchone()
    return "<h1>" + name + "</h1><p>" + bio + "</p>"


def healthcheck():
    # Bad: shell=True s interpolovanym vstupem z env
    host = os.environ.get("PING_HOST", "localhost")
    subprocess.call("ping -c 1 " + host, shell=True)
