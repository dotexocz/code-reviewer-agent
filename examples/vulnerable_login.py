"""Ukazkovy soubor s umyslnymi chybami pro demonstraci reviewera.

Tento kod obsahuje SCHVALNE:
- Bezpecnostni dury: hardcoded heslo, SQL injection, slaby hash, command injection
- Vykonnostni problemy: N+1 dotaz, opakovany I/O ve smycce
- Stylove prohresky: kratke nazvy, magic numbers, prilis dlouha funkce

NIKDY tento kod nepouzivej v produkci - je to fixture pro demo.
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
